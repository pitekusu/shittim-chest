"""DynamoDB Local integration for Records OAuth, Session, and read pagination."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import boto3
import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    deserialize_affection_profile,
    serialize_affection_profile,
)
from shittim_chest.domain.affection import AffectionProfile, MemorialUnlock
from shittim_chest.domain.debate_content import ParticipantSlot
from shittim_chest.domain.identifiers import DebateId
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.adapters import ArchiveRepository
from shittim_records.admin import AdminFailure, PromptRevisionSummary
from shittim_records.admin_adapters import DynamoPromptAuditStore
from shittim_records.archive import project_completed_debate
from shittim_records.auth import AuthFailure, OAuthState, SessionRecord
from shittim_records.auth_adapters import DynamoAuthStore
from shittim_records.cost_adapters import DynamoCostLedgerStore
from shittim_records.costs import ProviderDailyCost, ProviderDailyRate
from shittim_records.inspector_translation_adapters import DynamoInspectorTranslationStore
from shittim_records.inspector_translations import (
    InspectorJapaneseSummary,
    inspector_description,
)
from shittim_records.memorial import MemorialFailure
from shittim_records.memorial_adapters import DynamoMemorialRepository
from shittim_records.projector import project_affection_profile
from shittim_records.ranking_adapters import DynamoRankingSnapshotStore, DynamoRankingSource
from shittim_records.rankings import RankingService
from shittim_records.read_adapters import DynamoRecordsReader


@pytest.fixture
def dynamodb_client() -> DynamoDBClient:
    endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
    if endpoint is None:
        pytest.skip("DYNAMODB_ENDPOINT_URL is required for DynamoDB Local tests")
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        endpoint_url=endpoint,
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - DynamoDB Local dummy credential.
    )


@pytest.fixture
def table_names(dynamodb_client: DynamoDBClient) -> Iterator[tuple[str, str, str]]:
    suffix = uuid.uuid4().hex
    session_table = f"records-session-{suffix}"
    archive_table = f"records-archive-{suffix}"
    statistics_table = f"records-statistics-{suffix}"
    dynamodb_client.create_table(
        TableName=session_table,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.create_table(
        TableName=archive_table,
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("PK", "SK", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": index,
                "KeySchema": [
                    {"AttributeName": f"{index}pk", "KeyType": "HASH"},
                    {"AttributeName": f"{index}sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in ("gsi1", "gsi2")
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.create_table(
        TableName=statistics_table,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = dynamodb_client.get_waiter("table_exists")
    waiter.wait(TableName=session_table)
    waiter.wait(TableName=archive_table)
    waiter.wait(TableName=statistics_table)
    try:
        yield session_table, archive_table, statistics_table
    finally:
        dynamodb_client.delete_table(TableName=session_table)
        dynamodb_client.delete_table(TableName=archive_table)
        dynamodb_client.delete_table(TableName=statistics_table)


@pytest.fixture
def memorial_source_table(dynamodb_client: DynamoDBClient) -> Iterator[str]:
    table_name = f"records-memorial-source-{uuid.uuid4().hex}"
    dynamodb_client.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.get_waiter("table_exists").wait(TableName=table_name)
    try:
        yield table_name
    finally:
        dynamodb_client.delete_table(TableName=table_name)


@pytest.fixture
def memorial_source_profile(
    dynamodb_client: DynamoDBClient,
    memorial_source_table: str,
) -> AffectionProfile:
    unlocked_at = datetime(2026, 9, 3, 1, 2, 3, 123456, tzinfo=UTC)
    profile = AffectionProfile(
        requester_key=uuid.uuid4().hex.ljust(43, "r"),
        requester_username="owner",
        requester_display_name="質問者",
        scores=(1000, 830, 410),
        version=7,
        updated_at=unlocked_at,
        memorial_unlock=MemorialUnlock(
            participant=ParticipantSlot.PARTICIPANT_A,
            unlocked_at=unlocked_at,
            debate_id=DebateId.new(),
            requester_display_name="質問者",
            memorial_cycle=1,
        ),
    )
    dynamodb_client.put_item(
        TableName=memorial_source_table,
        Item=marshal_item(serialize_affection_profile(profile)),
    )
    return profile


def test_oauth_claim_session_and_archive_pagination(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
) -> None:
    session_table, archive_table, statistics_table = table_names
    store = DynamoAuthStore(dynamodb_client, session_table)
    store.create_oauth_state(
        state_hash="state-hash",
        state=OAuthState(nonce_hash="nonce-hash", return_to="/", expires_at=200),
    )
    claimed = store.claim_oauth_state(
        state_hash="state-hash",
        nonce_hash="nonce-hash",
        now_epoch=100,
        claimed_at="2026-08-17T00:00:00+00:00",
    )
    assert claimed.return_to == "/"
    with pytest.raises(AuthFailure):
        store.claim_oauth_state(
            state_hash="state-hash",
            nonce_hash="nonce-hash",
            now_epoch=100,
            claimed_at="2026-08-17T00:00:01+00:00",
        )

    session = SessionRecord(
        requester_key="requester",
        display_name="Requester",
        avatar_asset_key=None,
        csrf_hash="csrf-hash",
        guild_verified_at="2026-08-17T00:00:00+00:00",
        expires_at=200,
    )
    store.create_session(session_hash="session-hash", session=session)
    assert store.get_session(session_hash="session-hash") == session
    profile_response = dynamodb_client.get_item(
        TableName=session_table,
        Key=marshal_item({"PK": "PROFILE#REQUESTER", "SK": "requester"}),
        ConsistentRead=True,
    )
    profile = unmarshal_item(profile_response["Item"])
    assert profile["display_name"] == "Requester"
    assert "expiresAt" not in profile
    store.delete_session(session_hash="session-hash")
    assert store.get_session(session_hash="session-hash") is None

    projection = project_completed_debate(
        completed_snapshot(),
        identity_hmac_key=b"records-test-key-that-is-longer-than-32-bytes",
        presentation=presentation(),
        projected_at=NOW,
    )
    assert ArchiveRepository(dynamodb_client, archive_table).put_projection(projection) is True
    assert ArchiveRepository(dynamodb_client, archive_table).put_projection(projection) is False
    reader = DynamoRecordsReader(
        dynamodb_client,
        cast(Any, _NoopS3()),
        archive_table_name=archive_table,
        statistics_table_name=statistics_table,
        session_table_name=session_table,
        media_bucket_name="media",
    )
    for _attempt in range(20):
        page = reader.list_meta(
            limit=1,
            sort="newest",
            winner=None,
            exclusive_start_key=None,
        )
        if page.items:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("DynamoDB Local GSI did not expose the Archive record")
    assert len(page.items) == 1
    assert page.items[0]["record_id"] == projection.record_id
    assert len(reader.load_record(record_id=projection.record_id)) == 12

    ranking = RankingService(
        source=DynamoRankingSource(
            dynamodb_client,
            archive_table,
            statistics_table,
        ),
        store=DynamoRankingSnapshotStore(dynamodb_client, statistics_table),
    ).refresh(now=NOW)
    assert ranking.archive_count == 1
    assert sum(entry.count for entry in ranking.wins) == 1
    assert len(reader.load_ranking_snapshots()) == 2

    cost_start = date(2026, 7, 25)
    cost_end = cost_start + timedelta(days=30)
    components = tuple(
        (name, Decimal("1") if name == "residual" else Decimal("0"))
        for name in (
            "cloudwatch",
            "public_ipv4",
            "dynamodb",
            "s3",
            "cloudfront",
            "api_gateway",
            "ecr",
            "inspector",
            "residual",
        )
    )
    cost_store = DynamoCostLedgerStore(dynamodb_client, statistics_table)
    cost_store.save_cost_window(
        source="AWS",
        costs=tuple(
            ProviderDailyCost(
                cost_date=cost_start + timedelta(days=index),
                category=category,
                amount_usd=Decimal("1"),
                estimated=False,
                components=components if category == "OTHER_AWS" else (),
            )
            for index in range(30)
            for category in ("FARGATE", "LAMBDA", "OTHER_AWS")
        ),
        next_date=cost_end,
        initial_complete=False,
        collected_at=NOW,
    )
    cost_store.save_rate_window(
        rates=tuple(
            ProviderDailyRate(cost_start + timedelta(days=index), Decimal("150"))
            for index in range(30)
        ),
        next_date=cost_end,
        initial_complete=False,
        collected_at=NOW,
    )
    checkpoint = cost_store.load_checkpoint("AWS")
    assert checkpoint is not None
    assert checkpoint.next_date == cost_end
    stored_costs, stored_rates = reader.load_cost_ledger()
    assert len(stored_costs) == 90
    assert len(stored_rates) == 30

    description = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=(
            "A boundary validation flaw can cause the affected process to read outside "
            "its intended memory region."
        ),
    )
    translation = InspectorJapaneseSummary(
        key=description.key,
        vulnerability_id=description.vulnerability_id,
        source_sha256=description.source_sha256,
        summary_ja=(
            "入力値の境界確認が不十分なため、細工されたデータを処理すると、対象プロセスが本来の範囲外にある"
            "メモリを読み取る可能性があります。その結果、処理の異常終了や、プロセス内で扱われる情報の一部が"
            "意図せず露出するおそれがある脆弱性です。"
        ),
        translated_at=NOW,
    )
    translation_store = DynamoInspectorTranslationStore(dynamodb_client, statistics_table)
    translation_store.save((translation,))

    assert translation_store.load((description.key,)) == {description.key: translation}
    cached = dynamodb_client.get_item(
        TableName=statistics_table,
        Key=marshal_item(
            {
                "PK": "ADMIN#INSPECTOR_TRANSLATION",
                "SK": f"SUMMARY#{description.key}",
            }
        ),
        ConsistentRead=True,
    )
    assert description.description not in str(unmarshal_item(cached["Item"]))


def test_admin_prompt_audit_transactions_recovery_and_pagination(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
) -> None:
    statistics_table = table_names[2]
    store = DynamoPromptAuditStore(dynamodb_client, statistics_table)
    revisions = tuple(f"r01k3gqp6g0000000000000000{index}" for index in range(4))
    active_revision: str | None = None

    for index, revision in enumerate(revisions):
        idempotency_hash = f"{index + 1:x}" * 64
        request_hash = f"{index + 5:x}" * 64
        created_at = datetime(2026, 8, 24, 3, index, tzinfo=UTC)
        operation = store.begin_operation(
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            revision=revision,
            created_at=created_at,
            action="publish",
            expected_base_revision=active_revision,
            source_revision=None,
        )
        assert store.get_pending_operation(request_hash) == operation

        if index == 1:
            with pytest.raises(AdminFailure) as conflict:
                store.begin_operation(
                    idempotency_hash="f" * 64,
                    request_hash="e" * 64,
                    revision="r01k3gqp6g00000000000000009",
                    created_at=created_at,
                    action="publish",
                    expected_base_revision=active_revision,
                    source_revision=None,
                )
            assert conflict.value.code == "PROMPT_REVISION_CONFLICT"
            assert store.get_pending_operation(request_hash) == operation

        summary = PromptRevisionSummary(
            revision=revision,
            created_at=created_at,
            action="publish",
            base_revision=active_revision,
            source_revision=None,
            checksum=f"{index + 9:x}" * 64,
        )
        store.complete_operation(operation=operation, summary=summary)
        completed = store.get_operation(idempotency_hash)
        assert completed is not None
        assert completed.complete is True
        assert store.get_summary(revision) == summary
        assert store.get_pending_operation_any() is None
        active_revision = revision

    first_page = store.list_summaries(limit=2, cursor=None)
    assert tuple(item.revision for item in first_page.items) == tuple(reversed(revisions[2:]))
    assert first_page.next_cursor == revisions[2]
    second_page = store.list_summaries(limit=2, cursor=first_page.next_cursor)
    assert tuple(item.revision for item in second_page.items) == tuple(reversed(revisions[:2]))
    if second_page.next_cursor is not None:
        assert store.list_summaries(limit=2, cursor=second_page.next_cursor).items == ()


def test_memorial_transactions_queue_idempotency_and_atomic_reset(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
    memorial_source_table: str,
    memorial_source_profile: AffectionProfile,
) -> None:
    statistics_table = table_names[2]
    requester_key = memorial_source_profile.requester_key
    profile_key = {
        "PK": f"AFFECTION#REQUESTER#{requester_key}",
        "SK": "PROFILE",
    }
    unlocked_at = memorial_source_profile.updated_at
    repository = DynamoMemorialRepository(
        dynamodb_client,
        source_table_name=memorial_source_table,
        statistics_table_name=statistics_table,
    )
    upload_hash = "1" * 64
    queue_hash = "2" * 64
    reset_hash = "3" * 64

    reservation = repository.reserve_upload(
        requester_key=requester_key,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=1024,
        sha256="4" * 64,
        idempotency_hash=upload_hash,
        now=unlocked_at,
    )
    assert (
        repository.reserve_upload(
            requester_key=requester_key,
            expected_cycle=1,
            content_type="image/png",
            size_bytes=1024,
            sha256="4" * 64,
            idempotency_hash=upload_hash,
            now=unlocked_at,
        )
        == reservation
    )
    checkpoint_key = {
        "PK": f"MEMORIAL#REQUESTER#{requester_key}",
        "SK": "CYCLE#00000001",
    }
    checkpoint = unmarshal_item(
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(checkpoint_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert checkpoint["state"] == "unlocked"
    assert checkpoint["upload_asset_key"] == reservation.asset_key
    assert checkpoint["upload_idempotency_hash"] == upload_hash
    assert unmarshal_item(
        dynamodb_client.get_item(
            TableName=memorial_source_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )["scores"] == [1000, 830, 410]

    queued = repository.queue_generation(
        requester_key=requester_key,
        expected_cycle=1,
        idempotency_hash=queue_hash,
        now=unlocked_at + timedelta(minutes=1),
    )
    assert queued.state == "queued"
    checkpoint = unmarshal_item(
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(checkpoint_key),
            ConsistentRead=True,
        )["Item"]
    )
    result_asset_key = checkpoint["result_asset_key"]
    assert checkpoint["queue_idempotency_hash"] == queue_hash
    assert str(result_asset_key).startswith("memorials/")

    replayed = repository.queue_generation(
        requester_key=requester_key,
        expected_cycle=1,
        idempotency_hash=queue_hash,
        now=unlocked_at + timedelta(minutes=1),
    )
    assert replayed.state == "queued"
    assert (
        unmarshal_item(
            dynamodb_client.get_item(
                TableName=statistics_table,
                Key=marshal_item(checkpoint_key),
                ConsistentRead=True,
            )["Item"]
        )["result_asset_key"]
        == result_asset_key
    )
    duplicate = repository.queue_generation(
        requester_key=requester_key,
        expected_cycle=1,
        idempotency_hash="5" * 64,
        now=unlocked_at + timedelta(minutes=1),
    )
    assert duplicate.state == "queued"
    duplicate_checkpoint = unmarshal_item(
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(checkpoint_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert duplicate_checkpoint["queue_idempotency_hash"] == queue_hash
    assert duplicate_checkpoint["result_asset_key"] == result_asset_key

    with pytest.raises(MemorialFailure) as active_generation:
        repository.reset_affection(
            requester_key=requester_key,
            expected_cycle=1,
            reset_score=500,
            idempotency_hash=reset_hash,
            now=unlocked_at + timedelta(minutes=2),
        )
    assert active_generation.value.code == "MEMORIAL_RESET_NOT_ALLOWED"
    unchanged_profile = unmarshal_item(
        dynamodb_client.get_item(
            TableName=memorial_source_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert unchanged_profile["scores"] == [1000, 830, 410]
    assert unchanged_profile["memorial_cycle"] == 1
    assert (
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(
                {
                    "PK": checkpoint_key["PK"],
                    "SK": "RESET#00000001",
                }
            ),
            ConsistentRead=True,
        ).get("Item")
        is None
    )

    first_job = repository.claim_generation(
        requester_key=requester_key,
        cycle=1,
        now=unlocked_at + timedelta(minutes=2),
    )
    assert first_job is not None
    assert first_job.generation_attempt == 1
    repository.release_generation_to_queue(
        job=first_job,
        released_at=unlocked_at + timedelta(minutes=2, seconds=1),
        refund_attempt=True,
    )
    repository.release_generation_to_queue(
        job=first_job,
        released_at=unlocked_at + timedelta(minutes=2, seconds=1),
        refund_attempt=True,
    )
    second_job = repository.claim_generation(
        requester_key=requester_key,
        cycle=1,
        now=unlocked_at + timedelta(minutes=2, seconds=2),
    )
    assert second_job is not None
    assert second_job.generation_attempt == 1
    assert second_job.generation_claim_token != first_job.generation_claim_token
    with pytest.raises(MemorialFailure) as stale_release:
        repository.release_generation_to_queue(
            job=first_job,
            released_at=unlocked_at + timedelta(minutes=2, seconds=3),
            refund_attempt=True,
        )
    assert stale_release.value.code == "MEMORIAL_STATE_CONFLICT"
    repository.release_generation_to_queue(
        job=second_job,
        released_at=unlocked_at + timedelta(minutes=2, seconds=3),
        refund_attempt=True,
    )
    refunded_checkpoint = unmarshal_item(
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(checkpoint_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert refunded_checkpoint["state"] == "queued"
    assert refunded_checkpoint["generation_attempt"] == 0
    job = repository.claim_generation(
        requester_key=requester_key,
        cycle=1,
        now=unlocked_at + timedelta(minutes=2, seconds=4),
    )
    assert job is not None
    assert job.generation_attempt == 1
    repository.fail_generation(
        job=job,
        failed_at=unlocked_at + timedelta(minutes=3),
        preserve_derived=False,
    )
    reset = repository.reset_affection(
        requester_key=requester_key,
        expected_cycle=1,
        reset_score=500,
        idempotency_hash=reset_hash,
        now=unlocked_at + timedelta(minutes=4),
    )
    assert reset.state == "locked"
    assert reset.cycle == 2
    assert reset.reset_count == 1
    updated_profile = unmarshal_item(
        dynamodb_client.get_item(
            TableName=memorial_source_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert updated_profile["scores"] == [500, 500, 500]
    assert updated_profile["reset_count"] == 1
    assert updated_profile["memorial_cycle"] == 2
    assert updated_profile["version"] == 8
    parsed_profile = deserialize_affection_profile(updated_profile)
    assert parsed_profile.scores == (500, 500, 500)
    assert parsed_profile.updated_at == unlocked_at + timedelta(minutes=4)
    assert parsed_profile.memorial_unlock is None
    projected = project_affection_profile(updated_profile, identity_hmac_key=b"i" * 32)
    assert projected["scores"] == {
        "participant-a": 500,
        "participant-b": 500,
        "participant-c": 500,
    }
    assert projected["reset_count"] == 1
    assert projected["source_version"] == 8
    assert not {
        "unlocked_participant",
        "unlocked_at",
        "unlock_debate_id",
        "unlock_display_name",
        "unlock_retroactive",
    }.intersection(updated_profile)
    reset_receipt = unmarshal_item(
        dynamodb_client.get_item(
            TableName=statistics_table,
            Key=marshal_item(
                {
                    "PK": checkpoint_key["PK"],
                    "SK": "RESET#00000001",
                }
            ),
            ConsistentRead=True,
        )["Item"]
    )
    assert reset_receipt["record_type"] == "memorial_reset"
    assert reset_receipt["reset_to_cycle"] == 2
    assert reset_receipt["idempotency_hash"] == reset_hash

    replayed_reset = repository.reset_affection(
        requester_key=requester_key,
        expected_cycle=1,
        reset_score=500,
        idempotency_hash=reset_hash,
        now=unlocked_at + timedelta(minutes=5),
    )
    assert replayed_reset.state == "locked"
    assert replayed_reset.cycle == 2
    assert replayed_reset.reset_count == 1


def test_memorial_repeated_failed_recovery_preserves_image_and_attempts_with_same_key(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
    memorial_source_table: str,
    memorial_source_profile: AffectionProfile,
) -> None:
    repository = DynamoMemorialRepository(
        dynamodb_client,
        source_table_name=memorial_source_table,
        statistics_table_name=table_names[2],
    )
    owner = memorial_source_profile.requester_key
    now = memorial_source_profile.updated_at
    repository.reserve_upload(
        requester_key=owner,
        expected_cycle=1,
        content_type="image/png",
        size_bytes=1024,
        sha256="4" * 64,
        idempotency_hash="1" * 64,
        now=now,
    )
    repository.queue_generation(
        requester_key=owner,
        expected_cycle=1,
        idempotency_hash="2" * 64,
        now=now + timedelta(seconds=1),
    )
    for attempt in (1, 2):
        job = repository.claim_generation(
            requester_key=owner, cycle=1, now=now + timedelta(seconds=attempt * 2)
        )
        assert job is not None
        assert job.generation_attempt == attempt
        repository.release_generation_to_queue(
            job=job, released_at=now + timedelta(seconds=attempt * 2 + 1)
        )
    job = repository.claim_generation(requester_key=owner, cycle=1, now=now + timedelta(seconds=6))
    assert job is not None
    assert job.generation_attempt == 3
    result_key = job.result_asset_key
    job = repository.checkpoint_narrative(
        job=job, narrative="思" * 700, now=now + timedelta(seconds=7)
    )
    job = repository.checkpoint_image(
        job=job, image_asset_key=result_key, now=now + timedelta(seconds=8)
    )
    repository.fail_generation(job=job, failed_at=now + timedelta(seconds=9), preserve_derived=True)

    # A lost HTTP response can hide queued -> failed, so the browser retries the same key.
    recovery_hash = "3" * 64
    for attempt in (4, 5):
        recovered_at = now + timedelta(seconds=attempt * 3)
        queued = repository.queue_generation(
            requester_key=owner,
            expected_cycle=1,
            idempotency_hash=recovery_hash,
            now=recovered_at,
        )
        assert queued.state == "queued"
        job = repository.claim_generation(requester_key=owner, cycle=1, now=recovered_at)
        assert job is not None
        assert job.generation_attempt == attempt
        assert job.result_asset_key == job.image_asset_key == result_key
        assert job.narrative == "思" * 700
        if attempt == 4:
            repository.fail_generation(job=job, failed_at=recovered_at, preserve_derived=True)

    # Completion-only recovery may exceed the paid-attempt budget, but creates no new work.
    repository.complete_generation(job=job, generated_at=now + timedelta(seconds=20))
    replayed = repository.queue_generation(
        requester_key=owner,
        expected_cycle=1,
        idempotency_hash=recovery_hash,
        now=now + timedelta(seconds=21),
    )
    assert replayed.state == "ready"
    assert len(replayed.memories) == 1
    memory = repository.get_memory(requester_key=owner, cycle=1)
    assert memory is not None
    assert memory.image_asset_key == result_key
    assert (
        repository.claim_generation(requester_key=owner, cycle=1, now=now + timedelta(seconds=22))
        is None
    )


class _NoopS3:
    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://media.example.invalid/signed"
