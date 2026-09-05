"""Memorial AWS persistence, private asset, and generation adapter tests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from botocore.exceptions import ClientError
from openai import OpenAI
from PIL import Image
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    DynamoItem,
    deserialize_affection_profile,
    serialize_affection_profile,
)
from shittim_chest.domain.affection import AffectionProfile, MemorialUnlock
from shittim_chest.domain.debate_content import ParticipantSlot
from shittim_chest.domain.identifiers import DebateId

from shittim_records.admin import PromptValues
from shittim_records.memorial import (
    GeneratedMemorialImage,
    MemorialFailure,
    MemorialGenerationJob,
    MemorialSnapshot,
    MemorialUploadReservation,
)
from shittim_records.memorial_adapters import (
    DynamoMemorialRepository,
    DynamoRecentQuestionSource,
    MemorialConfigurationRepository,
    MemorialSecurityConfigurationRepository,
    OpenAIMemorialContentGenerator,
    S3MemorialAssetStore,
    SqsMemorialJobQueue,
    _is_transaction_conflict,
    render_memorial_overlay,
)
from shittim_records.projector import project_affection_profile
from shittim_records.read_api import PARTICIPANT_AVATAR_ASSET_KEYS

NOW = datetime(2026, 9, 3, 1, 2, 3, tzinfo=UTC)
REQUESTER_KEY = "r" * 43
UPLOAD_SHA256 = "a" * 64
IDEMPOTENCY_HASH = "b" * 64
RESULT_KEY = "memorials/" + "m" * 43 + ".png"
NARRATIVE = "思" * 700


def _profile(
    *,
    unlocked: bool = True,
    cycle: int = 1,
    reset_count: int = 0,
    updated_at: datetime = NOW,
) -> DynamoItem:
    return serialize_affection_profile(
        AffectionProfile(
            requester_key=REQUESTER_KEY,
            requester_username="owner",
            requester_display_name="質問者",
            scores=(1000, 500, 500),
            version=4,
            updated_at=updated_at,
            reset_count=reset_count,
            memorial_cycle=cycle,
            memorial_unlock=(
                MemorialUnlock(
                    participant=ParticipantSlot.PARTICIPANT_A,
                    unlocked_at=NOW,
                    debate_id=DebateId.parse("019faaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa"),
                    requester_display_name="質問者",
                    memorial_cycle=cycle,
                )
                if unlocked
                else None
            ),
        )
    )


def _checkpoint(
    *,
    state: str = "queued",
    narrative: str | None = None,
    image_asset_key: str | None = None,
    generation_attempt: int = 0,
) -> DynamoItem:
    item: DynamoItem = {
        "PK": f"MEMORIAL#REQUESTER#{REQUESTER_KEY}",
        "SK": "CYCLE#00000001",
        "schema_version": 1,
        "record_type": "memorial_cycle",
        "requester_key": REQUESTER_KEY,
        "requester_display_name": "質問者",
        "cycle": 1,
        "state": state,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(),
        "upload_asset_key": "uploads/" + "u" * 43 + ".bin",
        "upload_content_type": "image/png",
        "upload_size_bytes": 123,
        "upload_sha256": UPLOAD_SHA256,
        "upload_expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "upload_idempotency_hash": IDEMPOTENCY_HASH,
        "queue_idempotency_hash": IDEMPOTENCY_HASH,
        "result_asset_key": RESULT_KEY,
        "generation_attempt": generation_attempt,
        "updated_at": NOW.isoformat(),
    }
    if state == "generating":
        item["generation_lease_expires_at"] = (NOW + timedelta(minutes=5)).isoformat()
        item["generation_claim_token"] = "c" * 22
    if narrative is not None:
        item["narrative"] = narrative
    if image_asset_key is not None:
        item["image_asset_key"] = image_asset_key
    if state == "ready":
        item["generated_at"] = (NOW + timedelta(minutes=2)).isoformat()
    return item


class DynamoRecorder:
    def __init__(
        self,
        *,
        profile: DynamoItem | None = None,
        checkpoint: DynamoItem | None = None,
        query_items: list[DynamoItem] | None = None,
    ) -> None:
        self.profile = profile
        self.checkpoint = checkpoint
        self.query_items = query_items or []
        self.get_tables: list[str] = []
        self.transactions: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_tables.append(kwargs["TableName"])
        key = unmarshal_item(kwargs["Key"])
        if key["SK"] == "PROFILE":
            return {} if self.profile is None else {"Item": marshal_item(self.profile)}
        if str(key["SK"]).startswith("RESET#"):
            return {}
        return {} if self.checkpoint is None else {"Item": marshal_item(self.checkpoint)}

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        return {"Items": [marshal_item(item) for item in self.query_items]}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        for action in kwargs["TransactItems"]:
            operation = next(iter(action.values()))
            if operation["TableName"] == "source":
                assert self.profile is not None
                values = unmarshal_item(operation["ExpressionAttributeValues"])
                assert values[":unlocked_at"] == self.profile["unlocked_at"]
        self.transactions.append(kwargs)
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}


class ConditionalUpdateDynamoRecorder(DynamoRecorder):
    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        raise ClientError(
            cast(
                Any,
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "private detail",
                    }
                },
            ),
            "UpdateItem",
        )


def _apply_update(item: DynamoItem, update: dict[str, Any]) -> None:
    """Apply the assignment-only SET/REMOVE subset used by queueing and reset."""

    values = unmarshal_item(update["ExpressionAttributeValues"])
    names = update.get("ExpressionAttributeNames", {})
    assignments, _, removals = update["UpdateExpression"].removeprefix("SET ").partition(" REMOVE ")
    for assignment in assignments.split(","):
        name, value = (part.strip() for part in assignment.split("="))
        item[names.get(name, name)] = values[value]
    for removal in removals.split(","):
        if removal:
            name = removal.strip()
            item.pop(names.get(name, name), None)


class QueueTransactionDynamoRecorder(DynamoRecorder):
    """Enforce DynamoDB token identity and persist queue transitions for replay tests."""

    def __init__(self) -> None:
        super().__init__(profile=_profile(), checkpoint=_checkpoint(state="unlocked"))
        self.accepted_tokens: dict[str, object] = {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        token = kwargs["ClientRequestToken"]
        actions = kwargs["TransactItems"]
        previous = self.accepted_tokens.get(token)
        if previous is not None:
            if previous != actions:
                raise ClientError(
                    {"Error": {"Code": "IdempotentParameterMismatchException"}},
                    "TransactWriteItems",
                )
            return {}
        assert self.checkpoint is not None
        update = actions[1]["Update"]
        values = unmarshal_item(update["ExpressionAttributeValues"])
        assert self.checkpoint["state"] == values[":prior_state"]
        super().transact_write_items(**kwargs)
        _apply_update(self.checkpoint, update)
        self.accepted_tokens[token] = actions
        return {}


def _repository(client: DynamoRecorder, *, with_source: bool = True) -> DynamoMemorialRepository:
    return DynamoMemorialRepository(
        cast(Any, client),
        source_table_name="source" if with_source else None,
        statistics_table_name="statistics",
    )


def test_snapshot_synthesizes_locked_state_without_an_affection_profile() -> None:
    client = DynamoRecorder()

    result = _repository(client).get_snapshot(requester_key=REQUESTER_KEY)

    assert result == MemorialSnapshot(
        requester_key=REQUESTER_KEY,
        state="locked",
        cycle=1,
        reset_count=0,
        unlocked_participant=None,
        unlocked_at=None,
    )
    assert client.get_tables == ["source"]


def test_snapshot_returns_ascending_content_free_memory_summaries() -> None:
    ready = _checkpoint(state="ready", narrative=NARRATIVE, image_asset_key=RESULT_KEY)
    client = DynamoRecorder(profile=_profile(), checkpoint=ready, query_items=[ready])

    result = _repository(client).get_snapshot(requester_key=REQUESTER_KEY)

    assert result.state == "ready"
    assert result.latest_ready_cycle == 1
    assert tuple(summary.cycle for summary in result.memories) == (1,)
    assert result.upload_ready is False


def test_reserve_upload_uses_an_opaque_random_key_and_exact_source_cas() -> None:
    client = DynamoRecorder(profile=_profile())

    reservation = _repository(client).reserve_upload(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=123,
        sha256=UPLOAD_SHA256,
        idempotency_hash=IDEMPOTENCY_HASH,
        now=NOW,
    )

    assert reservation.asset_key.startswith("uploads/")
    assert REQUESTER_KEY not in reservation.asset_key
    transaction = client.transactions[0]["TransactItems"]
    condition = transaction[0]["ConditionCheck"]
    assert condition["TableName"] == "source"
    assert all(
        name in condition["ConditionExpression"]
        for name in (
            "version",
            "memorial_cycle",
            "unlocked_participant",
            "unlocked_at",
            "unlock_debate_id",
            "unlock_display_name",
            "unlock_retroactive",
        )
    )
    saved = unmarshal_item(transaction[1]["Put"]["Item"])
    assert saved["upload_asset_key"] == reservation.asset_key
    assert saved["requester_display_name"] == "質問者"


def test_expired_upload_reservation_can_be_replaced_before_queueing() -> None:
    expired = _checkpoint(state="unlocked")
    expired["upload_expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    client = DynamoRecorder(profile=_profile(), checkpoint=expired)

    replacement = _repository(client).reserve_upload(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=124,
        sha256="c" * 64,
        idempotency_hash="d" * 64,
        now=NOW,
    )

    put = client.transactions[0]["TransactItems"][1]["Put"]
    assert "upload_expires_at = :old_expiry" in put["ConditionExpression"]
    assert replacement.asset_key != expired["upload_asset_key"]


@pytest.mark.parametrize("state", ("unlocked", "failed"))
def test_upload_idempotency_key_cannot_be_reused_for_different_content(
    state: str,
) -> None:
    existing = _checkpoint(state=state)
    existing["upload_expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    client = DynamoRecorder(profile=_profile(), checkpoint=existing)

    with pytest.raises(MemorialFailure) as failure:
        _repository(client).reserve_upload(
            requester_key=REQUESTER_KEY,
            expected_cycle=1,
            content_type="image/png",
            size_bytes=124,
            sha256="c" * 64,
            idempotency_hash=IDEMPOTENCY_HASH,
            now=NOW,
        )

    assert (failure.value.code, failure.value.status) == ("IDEMPOTENCY_CONFLICT", 409)
    assert client.transactions == []


def test_terminal_failed_upload_replacement_preserves_logical_attempt_budget() -> None:
    failed = _checkpoint(state="failed", generation_attempt=3)
    client = DynamoRecorder(profile=_profile(), checkpoint=failed)

    _repository(client).reserve_upload(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=124,
        sha256="c" * 64,
        idempotency_hash="d" * 64,
        now=NOW,
    )

    saved = unmarshal_item(client.transactions[0]["TransactItems"][1]["Put"]["Item"])
    assert saved["generation_attempt"] == 3


def test_queued_checkpoint_can_be_redispatched_with_a_new_idempotency_key() -> None:
    queued = _checkpoint(state="queued")
    client = DynamoRecorder(profile=_profile(), checkpoint=queued)

    result = _repository(client).queue_generation(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        idempotency_hash="d" * 64,
        now=NOW,
    )

    assert result.state == "queued"
    assert client.transactions == []


def test_failed_checkpoint_with_paid_progress_requeues_without_new_result_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _checkpoint(state="failed", narrative=NARRATIVE, generation_attempt=3)
    client = DynamoRecorder(profile=_profile(), checkpoint=failed)
    repository = _repository(client)
    queued = MemorialSnapshot(
        requester_key=REQUESTER_KEY,
        state="queued",
        cycle=1,
        reset_count=0,
        unlocked_participant="participant-a",
        unlocked_at=NOW,
    )
    monkeypatch.setattr(repository, "get_snapshot", lambda **_kwargs: queued)

    result = repository.queue_generation(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        idempotency_hash="d" * 64,
        now=NOW,
    )

    assert result is queued
    update = client.transactions[0]["TransactItems"][1]["Update"]
    values = unmarshal_item(update["ExpressionAttributeValues"])
    assert values[":prior_state"] == "failed"
    assert values[":result_asset_key"] == RESULT_KEY
    assert values[":narrative"] == NARRATIVE
    assert ":zero" not in values
    assert "narrative = :narrative" in update["ConditionExpression"]


def test_failed_recovery_reuses_http_key_without_reusing_a_different_dynamo_transaction() -> None:
    client = QueueTransactionDynamoRecorder()
    repository = _repository(client)
    request = {
        "requester_key": REQUESTER_KEY,
        "expected_cycle": 1,
        "idempotency_hash": IDEMPOTENCY_HASH,
    }
    assert repository.queue_generation(**request, now=NOW).state == "queued"
    assert client.checkpoint is not None
    result_key = client.checkpoint["result_asset_key"]
    for attempt in (1, 2):
        client.checkpoint.update(
            {"state": "failed", "generation_attempt": attempt, "narrative": NARRATIVE}
        )
        recovered = repository.queue_generation(
            **request,
            now=NOW + timedelta(minutes=attempt),
        )
        assert recovered.state == "queued"
        assert client.checkpoint["generation_attempt"] == attempt
        assert client.checkpoint["result_asset_key"] == result_key
        assert client.checkpoint["narrative"] == NARRATIVE
        replay = repository.queue_generation(
            **request,
            now=NOW + timedelta(minutes=attempt, seconds=1),
        )
        assert replay == recovered
        assert len(client.transactions) == attempt + 1
    assert len(client.accepted_tokens) == 3


def test_claim_and_generation_checkpoints_use_only_statistics_table() -> None:
    queued = _checkpoint()
    client = DynamoRecorder(checkpoint=queued)
    repository = _repository(client, with_source=False)

    job = repository.claim_generation(requester_key=REQUESTER_KEY, cycle=1, now=NOW)

    assert job is not None
    assert job.generation_attempt == 1
    assert job.result_asset_key == RESULT_KEY
    assert client.get_tables == ["statistics"]
    with_narrative = repository.checkpoint_narrative(job=job, narrative=NARRATIVE, now=NOW)
    with_image = repository.checkpoint_image(
        job=with_narrative,
        image_asset_key=RESULT_KEY,
        now=NOW,
    )
    memory = repository.complete_generation(
        job=with_image,
        generated_at=NOW + timedelta(minutes=1),
    )
    assert memory.image_asset_key == RESULT_KEY
    assert memory.narrative == NARRATIVE
    assert len(client.updates) == 4
    claim_values = unmarshal_item(client.updates[0]["ExpressionAttributeValues"])
    assert claim_values[":generation_attempt"] == 1
    assert "generation_attempt = :previous_attempt" in client.updates[0]["ConditionExpression"]
    claim_token = cast(str, claim_values[":generation_claim_token"])
    assert len(claim_token) == 22
    for update in client.updates[1:]:
        values = unmarshal_item(update["ExpressionAttributeValues"])
        assert "generation_claim_token = :generation_claim_token" in update["ConditionExpression"]
        assert values[":generation_claim_token"] == claim_token


def test_expired_third_attempt_lease_can_be_claimed_for_recovery() -> None:
    generating = _checkpoint(state="generating", generation_attempt=3)
    generating["generation_lease_expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    client = DynamoRecorder(checkpoint=generating)

    job = _repository(client, with_source=False).claim_generation(
        requester_key=REQUESTER_KEY,
        cycle=1,
        now=NOW,
    )

    assert job is not None and job.generation_attempt == 4
    update = client.updates[0]
    values = unmarshal_item(update["ExpressionAttributeValues"])
    assert values[":previous_attempt"] == 3
    assert values[":generation_attempt"] == 4
    assert values[":old_lease"] == generating["generation_lease_expires_at"]
    assert len(cast(str, values[":generation_claim_token"])) == 22


def test_deadline_release_atomically_refunds_claimed_attempt() -> None:
    generating = _checkpoint(state="generating", generation_attempt=3)
    client = DynamoRecorder(checkpoint=generating)
    repository = _repository(client, with_source=False)
    job = repository._job(generating)

    repository.release_generation_to_queue(
        job=job,
        released_at=NOW,
        refund_attempt=True,
    )

    update = client.updates[0]
    values = unmarshal_item(update["ExpressionAttributeValues"])
    assert "generation_attempt = :released_attempt" in update["UpdateExpression"]
    assert values[":generation_attempt"] == 3
    assert values[":released_attempt"] == 2
    assert values[":generation_claim_token"] == generating["generation_claim_token"]
    assert "generation_claim_token = :generation_claim_token" in update["ConditionExpression"]


def test_replayed_deadline_release_does_not_refund_the_attempt_twice() -> None:
    queued = _checkpoint(state="queued", generation_attempt=2)
    queued["generation_claim_token"] = "c" * 22
    client = DynamoRecorder(checkpoint=queued)
    repository = _repository(client, with_source=False)
    claimed = _checkpoint(state="generating", generation_attempt=3)

    repository.release_generation_to_queue(
        job=repository._job(claimed),
        released_at=NOW,
        refund_attempt=True,
    )

    assert client.updates == []


def test_stale_claim_token_cannot_replay_another_claims_release() -> None:
    queued = _checkpoint(state="queued", generation_attempt=2)
    queued["generation_claim_token"] = "n" * 22
    client = DynamoRecorder(checkpoint=queued)
    repository = _repository(client, with_source=False)
    claimed = _checkpoint(state="generating", generation_attempt=3)

    with pytest.raises(MemorialFailure) as failure:
        repository.release_generation_to_queue(
            job=repository._job(claimed),
            released_at=NOW,
            refund_attempt=True,
        )

    assert failure.value.code == "MEMORIAL_STATE_CONFLICT"
    assert client.updates == []


def test_stale_claim_token_cannot_adopt_another_claims_terminal_state() -> None:
    ready = _checkpoint(
        state="ready",
        narrative=NARRATIVE,
        image_asset_key=RESULT_KEY,
        generation_attempt=3,
    )
    ready["generation_claim_token"] = "n" * 22
    client = ConditionalUpdateDynamoRecorder(checkpoint=ready)
    repository = _repository(client, with_source=False)
    stale = _checkpoint(
        state="generating",
        narrative=NARRATIVE,
        image_asset_key=RESULT_KEY,
        generation_attempt=3,
    )
    job = repository._job(stale)

    with pytest.raises(MemorialFailure) as completion_failure:
        repository.complete_generation(job=job, generated_at=NOW + timedelta(minutes=2))
    with pytest.raises(MemorialFailure) as failure_replay:
        repository.fail_generation(job=job, failed_at=NOW, preserve_derived=True)

    assert completion_failure.value.code == "MEMORIAL_STATE_CONFLICT"
    assert failure_replay.value.code == "MEMORIAL_STATE_CONFLICT"


@pytest.mark.parametrize(
    ("preserve_derived", "expects_remove"),
    ((False, True), (True, False)),
)
def test_terminal_failure_clears_only_unrecoverable_partial_output(
    preserve_derived: bool,
    expects_remove: bool,
) -> None:
    checkpoint = _checkpoint(
        state="generating",
        narrative=NARRATIVE,
        generation_attempt=3,
    )
    client = DynamoRecorder(checkpoint=checkpoint)
    job = _repository(client, with_source=False)._job(checkpoint)

    _repository(client, with_source=False).fail_generation(
        job=job,
        failed_at=NOW,
        preserve_derived=preserve_derived,
    )

    update = client.updates[0]
    assert ("REMOVE narrative, image_asset_key" in update["UpdateExpression"]) is expects_remove
    values = unmarshal_item(update["ExpressionAttributeValues"])
    assert values[":generation_attempt"] == 3
    assert values[":generation_claim_token"] == checkpoint["generation_claim_token"]


@pytest.mark.parametrize("profile_updated_at", (NOW, NOW + timedelta(seconds=1)))
def test_reset_transaction_fences_generation_and_atomically_resets_profile(
    monkeypatch: pytest.MonkeyPatch,
    profile_updated_at: datetime,
) -> None:
    ready = _checkpoint(state="ready", narrative=NARRATIVE, image_asset_key=RESULT_KEY)
    profile = _profile(updated_at=profile_updated_at)
    client = DynamoRecorder(profile=profile, checkpoint=ready)
    repository = _repository(client)
    post_reset = MemorialSnapshot(
        requester_key=REQUESTER_KEY,
        state="locked",
        cycle=2,
        reset_count=1,
        unlocked_participant=None,
        unlocked_at=None,
        memories=(),
    )
    monkeypatch.setattr(repository, "get_snapshot", lambda **_kwargs: post_reset)

    result = repository.reset_affection(
        requester_key=REQUESTER_KEY,
        expected_cycle=1,
        reset_score=500,
        idempotency_hash=IDEMPOTENCY_HASH,
        now=NOW,
    )

    assert result is post_reset
    actions = client.transactions[0]["TransactItems"]
    source_update = actions[0]["Update"]
    values = unmarshal_item(source_update["ExpressionAttributeValues"])
    assert values[":scores"] == [500, 500, 500]
    assert values[":next_cycle"] == 2
    assert values[":reset_count"] == 1
    assert "REMOVE unlocked_participant" in source_update["UpdateExpression"]
    generation_fence = actions[1]["ConditionCheck"]["ConditionExpression"]
    assert "#state <> :queued" in generation_fence
    assert "#state <> :generating" in generation_fence
    receipt = unmarshal_item(actions[2]["Put"]["Item"])
    assert receipt["record_type"] == "memorial_reset"
    assert receipt["idempotency_hash"] == IDEMPOTENCY_HASH
    _apply_update(profile, source_update)
    reloaded = deserialize_affection_profile(profile, requester_key=REQUESTER_KEY)
    projected = project_affection_profile(profile, identity_hmac_key=b"x" * 32)
    assert reloaded.updated_at == max(NOW, profile_updated_at)
    assert reloaded.scores == (500, 500, 500)
    assert reloaded.memorial_unlock is None
    assert projected["reset_count"] == 1
    assert projected["memorial_cycle"] == 2


class SqsRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"MessageId": "message"}


def test_queue_message_contains_only_the_opaque_owner_and_cycle() -> None:
    client = SqsRecorder()

    SqsMemorialJobQueue(cast(Any, client), "https://sqs.example.invalid/jobs.fifo").send(
        requester_key=REQUESTER_KEY,
        cycle=2,
    )

    call = client.calls[0]
    assert json.loads(call["MessageBody"]) == {
        "cycle": 2,
        "requesterKey": REQUESTER_KEY,
    }
    assert set(call) == {
        "QueueUrl",
        "MessageBody",
        "MessageGroupId",
        "MessageDeduplicationId",
    }


class SsmRecorder:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[dict[str, Any]] = []

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Parameters": [{"Name": name, "Value": self.values[name]} for name in kwargs["Names"]]
        }


def test_security_configuration_hides_session_key_from_repr() -> None:
    session_name = "/private/session"
    oauth_name = "/private/oauth"
    secret = "session-secret-" * 3
    oauth = json.dumps(
        {
            "schema_version": 1,
            "client_id": "1" * 17,
            "guild_id": "2" * 17,
            "allowed_origin": "https://records.example.invalid",
            "oauth_callback_url": ("https://records.example.invalid/api/v1/auth/discord/callback"),
        }
    )
    client = SsmRecorder({session_name: secret, oauth_name: oauth})

    loaded = MemorialSecurityConfigurationRepository(
        cast(Any, client),
        session_key_parameter_name=session_name,
        oauth_parameter_name=oauth_name,
    ).load()

    assert loaded.allowed_origin == "https://records.example.invalid"
    assert secret not in repr(loaded)
    assert client.calls[0]["WithDecryption"] is True


class RevisionSource:
    def load_active_revision_id(self) -> str:
        return "r" + "0" * 26

    def load_revision(self, _revision: str) -> Any:
        return SimpleNamespace(
            prompts=PromptValues.from_mapping(
                {
                    "system": "global trusted",
                    "moderator": "moderator",
                    "participant-a": "persona a",
                    "participant-b": "persona b",
                    "participant-c": "persona c",
                }
            )
        )


def test_memorial_configuration_uses_only_the_selected_verified_persona(
    caplog: pytest.LogCaptureFixture,
) -> None:
    key_name = "/shittim-chest/production/records/openai/memorial-api-key"
    client = SsmRecorder({key_name: "private-api-key"})
    configuration = MemorialConfigurationRepository(
        cast(Any, client),
        api_key_parameter_name=key_name,
        runtime_prompt_parameter_root="/shittim-chest/production/runtime-prompts",
        legacy_persona_parameter_names={
            slot: f"/shittim-chest/production/personas/v0001/{slot}"
            for slot in PARTICIPANT_AVATAR_ASSET_KEYS
        },
    )
    cast(Any, configuration)._revisions = RevisionSource()

    with caplog.at_level(logging.DEBUG):
        persona = configuration.load_participant_prompt("participant-a")
        api_key = configuration.load_api_key()

    assert persona == "persona a"
    assert api_key == "private-api-key"
    assert "private-api-key" not in caplog.text
    assert "persona a" not in caplog.text
    assert "global trusted" not in repr(persona)


class QuestionDynamo:
    def __init__(self, questions: list[str]) -> None:
        self.questions = questions
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Items": [
                marshal_item(
                    {
                        "record_type": "archive_meta",
                        "requester_key": REQUESTER_KEY,
                        "question": question,
                    }
                )
                for question in self.questions
            ]
        }


def test_recent_questions_use_the_requester_gsi_without_logging_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_question = "非公開の質問内容"
    client = QuestionDynamo([private_question])

    with caplog.at_level(logging.DEBUG):
        result = DynamoRecentQuestionSource(cast(Any, client), "archive").latest_questions(
            requester_key=REQUESTER_KEY,
            limit=10,
        )

    assert result == (private_question,)
    call = client.calls[0]
    assert call["IndexName"] == "gsi3"
    assert call["ScanIndexForward"] is False
    assert call["Limit"] == 10
    assert private_question not in caplog.text


def _png(size: tuple[int, int], *, color: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class Body(io.BytesIO):
    pass


class S3Recorder:
    def __init__(self) -> None:
        self.presigned_posts: list[dict[str, Any]] = []
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.presigned_posts.append(kwargs)
        fields = {
            **kwargs["Fields"],
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": "credential/scope",
            "x-amz-date": "20260903T010203Z",
            "policy": "cG9saWN5",
            "x-amz-signature": "c" * 64,
        }
        return {"url": "https://upload.example.invalid", "fields": fields}

    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://media.example.invalid/private"

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        content, content_type = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "ContentLength": len(content),
            "ContentType": content_type,
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(content).digest()).decode(),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        content, content_type = self.objects[(kwargs["Bucket"], kwargs["Key"])]
        return {
            "Body": Body(content),
            "ContentLength": len(content),
            "ContentType": content_type,
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(content).digest()).decode(),
        }

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = (
            kwargs["Body"],
            kwargs["ContentType"],
        )
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.deletes.append(kwargs)
        return {}


class HeadErrorS3(S3Recorder):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            cast(
                Any,
                {
                    "Error": {"Code": self.code, "Message": "private detail"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
            ),
            "HeadObject",
        )


def _reservation() -> MemorialUploadReservation:
    return MemorialUploadReservation(
        requester_key=REQUESTER_KEY,
        cycle=1,
        asset_key="uploads/" + "u" * 43 + ".bin",
        content_type="image/png",
        size_bytes=123,
        sha256=UPLOAD_SHA256,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def _job(
    *, narrative: str | None = None, image_asset_key: str | None = None
) -> MemorialGenerationJob:
    return MemorialGenerationJob(
        requester_key=REQUESTER_KEY,
        requester_display_name="質問者",
        cycle=1,
        participant="participant-a",
        unlocked_at=NOW,
        upload_asset_key="uploads/" + "u" * 43 + ".bin",
        result_asset_key=RESULT_KEY,
        narrative=narrative,
        image_asset_key=image_asset_key,
    )


def _asset_store(client: S3Recorder) -> S3MemorialAssetStore:
    return S3MemorialAssetStore(
        cast(Any, client),
        upload_bucket_name="uploads",
        media_bucket_name="media",
        participant_asset_keys=PARTICIPANT_AVATAR_ASSET_KEYS,
    )


def test_presigned_post_fixes_key_type_checksum_and_bounded_length() -> None:
    client = S3Recorder()

    ticket = _asset_store(client).create_upload_ticket(_reservation())

    request = client.presigned_posts[0]
    assert request["Fields"]["key"].startswith("uploads/")
    assert request["Fields"]["Content-Type"] == "image/png"
    assert request["Fields"]["x-amz-checksum-sha256"] == ticket.fields["x-amz-checksum-sha256"]
    assert ["content-length-range", 1, 10 * 1024 * 1024] in request["Conditions"]


def test_asset_store_normalizes_source_and_reuses_a_valid_generated_object() -> None:
    client = S3Recorder()
    source = _png((80, 64))
    generated = _png((1920, 1080))
    client.objects[("uploads", _job().upload_asset_key)] = (source, "image/png")
    client.objects[("media", RESULT_KEY)] = (generated, "image/png")
    store = _asset_store(client)

    normalized = store.load_upload(_job())

    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (80, 64)
        assert not image.info
    assert store.existing_generated(_job()) == RESULT_KEY


def test_asset_store_treats_only_explicit_s3_not_found_as_absent() -> None:
    store = _asset_store(HeadErrorS3("NoSuchKey"))

    assert store.verify_upload(_reservation()) is False
    assert store.existing_generated(_job()) is None


def test_asset_store_does_not_hide_s3_access_denied_as_absent() -> None:
    store = _asset_store(HeadErrorS3("AccessDenied"))

    with pytest.raises(MemorialFailure) as upload_error:
        store.verify_upload(_reservation())
    with pytest.raises(MemorialFailure) as generated_error:
        store.existing_generated(_job())

    assert upload_error.value.code == "MEMORIAL_UPLOAD_UNAVAILABLE"
    assert generated_error.value.code == "MEMORIAL_STORAGE_UNAVAILABLE"


def test_high_entropy_smartphone_jpeg_is_bounded_before_provider_use() -> None:
    dimensions = (2400, 1800)
    pixels = hashlib.shake_256(b"memorial-smartphone-fixture").digest(
        dimensions[0] * dimensions[1] * 3
    )
    source = Image.frombytes("RGB", dimensions, pixels)
    encoded = io.BytesIO()
    source.save(encoded, format="JPEG", quality=90)
    jpeg = encoded.getvalue()
    assert len(jpeg) < 10 * 1024 * 1024
    client = S3Recorder()
    client.objects[("uploads", _job().upload_asset_key)] = (jpeg, "image/jpeg")

    normalized = _asset_store(client).load_upload(_job())

    assert len(normalized) <= 10 * 1024 * 1024
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert max(image.size) == 1536


def test_asset_store_persists_only_the_preallocated_random_result_key() -> None:
    client = S3Recorder()
    store = _asset_store(client)
    generated = GeneratedMemorialImage(image_bytes=_png((1920, 1080)))

    result = store.store_generated(job=_job(), generated=generated, now=NOW)

    assert result == RESULT_KEY
    assert client.puts[0]["Key"] == RESULT_KEY
    assert REQUESTER_KEY not in RESULT_KEY
    assert client.puts[0]["IfNoneMatch"] == "*"
    assert client.puts[0]["CacheControl"] == "private, no-store"


def test_asset_store_deletes_a_reset_reservation_idempotently() -> None:
    client = S3Recorder()
    reservation = _reservation()

    _asset_store(client).delete_reservation(reservation)

    assert client.deletes == [{"Bucket": "uploads", "Key": reservation.asset_key}]


class TrustedConfiguration:
    def load_api_key(self) -> str:
        return "private-api-key"

    def load_participant_prompt(self, _participant: str) -> str:
        return "trusted persona"


class ParticipantReferences:
    def load_participant_reference(self, _participant: str) -> bytes:
        return _png((64, 64))


class OpenAIRecorder:
    def __init__(self) -> None:
        self.response_calls: list[dict[str, Any]] = []
        self.image_calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._response)
        self.images = SimpleNamespace(edit=self._image)

    def _response(self, **kwargs: Any) -> Any:
        self.response_calls.append(kwargs)
        return SimpleNamespace(output_text=NARRATIVE)

    def _image(self, **kwargs: Any) -> Any:
        self.image_calls.append(kwargs)
        encoded = base64.b64encode(_png((1920, 1088))).decode()
        return SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])


def _font_paths() -> tuple[Path, Path]:
    root = Path(__file__).parents[3] / "apps" / "records-web" / "src" / "assets" / "fonts"
    return root / "Delogy-Regular.ttf", root / "LINESeedJP-ExtraBold.woff2"


def test_openai_generation_is_stateless_split_and_uses_two_high_fidelity_images(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    title_font, date_font = _font_paths()
    import shittim_records.memorial_adapters as adapters

    monkeypatch.setattr(adapters, "_TITLE_FONT_PATH", title_font)
    monkeypatch.setattr(adapters, "_DATE_FONT_PATH", date_font)
    client = OpenAIRecorder()
    generator = OpenAIMemorialContentGenerator(
        cast(Any, TrustedConfiguration()),
        participant_references=cast(Any, ParticipantReferences()),
        client=client,
    )
    questions = ("非公開の質問",)

    with caplog.at_level(logging.DEBUG):
        generator.validate_image_inputs(
            participant="participant-a",
            source_image=_png((64, 64)),
        )
        narrative = generator.generate_narrative(
            participant="participant-a",
            requester_display_name="質問者",
            questions=questions,
            achieved_on=date(2026, 9, 3),
        )
        image = generator.generate_image(
            participant="participant-a",
            requester_display_name="質問者",
            questions=questions,
            source_image=_png((64, 64)),
            narrative=narrative,
            achieved_on=date(2026, 9, 3),
        )

    text_call = client.response_calls[0]
    assert text_call["model"] == "gpt-5.6-luna"
    assert text_call["store"] is False
    assert text_call["tools"] == []
    assert "trusted persona" in text_call["instructions"]
    image_call = client.image_calls[0]
    assert image_call["model"] == "gpt-image-2"
    assert image_call["size"] == "1920x1088"
    assert "input_fidelity" not in image_call
    assert len(image_call["image"]) == 2
    assert "trusted persona" in image_call["prompt"]
    assert "store" not in image_call
    with Image.open(io.BytesIO(image.image_bytes)) as result:
        assert result.size == (1920, 1080)
        assert result.mode == "RGB"
    assert "private-api-key" not in caplog.text
    assert questions[0] not in caplog.text
    assert NARRATIVE not in caplog.text


@pytest.mark.parametrize("operation", ("narrative", "image"))
def test_openai_client_disables_sdk_retries_and_bounds_each_paid_call(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    import shittim_records.memorial_adapters as adapters

    requests: list[httpx2.Request] = []

    def rate_limit(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(429, json={"error": {"message": "rate limited"}})

    generator = OpenAIMemorialContentGenerator(
        cast(Any, TrustedConfiguration()),
        participant_references=cast(Any, ParticipantReferences()),
    )

    with httpx2.Client(transport=httpx2.MockTransport(rate_limit)) as http_client:
        monkeypatch.setattr(
            adapters,
            "OpenAI",
            lambda **kwargs: OpenAI(http_client=http_client, **kwargs),
        )
        client = generator._openai_client()
        assert client.max_retries == 0
        assert client.timeout.as_dict() == {
            "connect": 5.0,
            "read": 120.0,
            "write": 30.0,
            "pool": 5.0,
        }
        inputs: dict[str, Any] = {
            "participant": "participant-a",
            "requester_display_name": "質問者",
            "questions": ("非公開の質問",),
            "achieved_on": date(2026, 9, 3),
        }
        with pytest.raises(MemorialFailure, match="MEMORIAL_PROVIDER_RATE_LIMITED"):
            if operation == "image":
                generator.generate_image(**inputs, source_image=_png((64, 64)), narrative=NARRATIVE)
            else:
                generator.generate_narrative(**inputs)

    assert len(requests) == 1
    expected_path = "/v1/images/edits" if operation == "image" else "/v1/responses"
    assert requests[0].url.path == expected_path


@pytest.mark.parametrize(
    ("code", "reasons", "expected"),
    (
        ("IdempotentParameterMismatchException", None, True),
        (
            "TransactionCanceledException",
            [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
            True,
        ),
        ("TransactionCanceledException", None, False),
        (
            "TransactionCanceledException",
            [{"Code": "None"}, {"Code": "ThrottlingError"}],
            False,
        ),
        (
            "TransactionCanceledException",
            [{"Code": "TransactionConflict"}],
            False,
        ),
        ("ProvisionedThroughputExceededException", None, False),
    ),
)
def test_transaction_conflict_classification_only_maps_expected_cas_failures(
    code: str,
    reasons: list[dict[str, str]] | None,
    expected: bool,
) -> None:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": "private detail"}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    error = ClientError(cast(Any, response), "TransactWriteItems")

    assert _is_transaction_conflict(error) is expected


def test_overlay_uses_bundled_fonts_and_a_japanese_achievement_date() -> None:
    title_font, date_font = _font_paths()

    rendered = render_memorial_overlay(
        _png((1920, 1088)),
        achieved_on=date(2026, 8, 31),
        title_font_path=title_font,
        date_font_path=date_font,
    )

    with Image.open(io.BytesIO(rendered)) as image:
        assert image.size == (1920, 1080)
        assert image.getpixel((1321, 983)) != (12, 34, 56)
