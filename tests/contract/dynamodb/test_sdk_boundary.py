"""SDK-level contracts that DynamoDB Local cannot reproduce reliably."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.outbox import (
    _require_transaction_size as require_outbox_transaction_size,
)
from shittim_chest.adapters.dynamodb.repository import (
    DynamoDbDebateRepository,
    _abandon_outbox_action,
    derive_affection_requester_key,
)
from shittim_chest.adapters.dynamodb.repository import (
    _require_transaction_size as require_repository_transaction_size,
)
from shittim_chest.adapters.dynamodb.serializer import (
    LEGACY_AFFECTION_SCHEMA_VERSION,
    DynamoItem,
    PersistenceFormatError,
    serialize_affection_profile,
)
from shittim_chest.application import (
    DebateSnapshot,
    DeliveryAbandonReason,
    DiscordBotSlot,
    LeaseGrant,
    OutboxOperation,
    OutboxStatus,
    content_sha256,
)
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable
from shittim_chest.domain import (
    DEFAULT_AFFECTION_SCORE,
    AffectionProfile,
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    ParticipantSlot,
)

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=UTC)


def test_affection_and_records_derive_the_same_opaque_requester_key() -> None:
    identity_key = b"i" * 32

    assert derive_affection_requester_key(identity_key, "private-requester") == (
        "xDtrTAPtslo-r6StMr0FS6GliBLskiI-CbXUlFIlbfI"
    )


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["current", "legacy", "absent", "migrated_between_reads"])
async def test_affection_score_reads_one_performer_from_the_requester_profile(source: str) -> None:
    sdk = client()
    identity_key = b"i" * 32
    requester_id = "private-requester"
    requester_key = derive_affection_requester_key(identity_key, requester_id)
    repository = DynamoDbDebateRepository(
        client=sdk, table_name="test-table", identity_hmac_key=identity_key
    )
    profile = AffectionProfile(
        requester_key=requester_key,
        requester_username="requester",
        requester_display_name="Requester",
        scores=(0, 700, 299),
        version=1,
        updated_at=NOW,
    )
    current = serialize_affection_profile(profile)
    legacy: DynamoItem = {
        "PK": f"AFFECTION#REQUESTER#{requester_id}",
        "SK": "PROFILE",
        "record_type": "affection_profile",
        "schema_version": LEGACY_AFFECTION_SCHEMA_VERSION,
        "requester_id": requester_id,
        "requester_username": profile.requester_username,
        "requester_display_name": profile.requester_display_name,
        "scores": list(profile.scores),
        "version": profile.version,
        "updated_at": NOW.isoformat(),
    }
    current_get = {
        "TableName": "test-table",
        "Key": marshal_item({"PK": current["PK"], "SK": "PROFILE"}),
        "ConsistentRead": True,
    }
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(current)} if source == "current" else {},
            current_get,
        )
        if source != "current":
            stubber.add_response(
                "get_item",
                {"Item": marshal_item(legacy)} if source == "legacy" else {},
                {
                    "TableName": "test-table",
                    "Key": marshal_item({"PK": legacy["PK"], "SK": "PROFILE"}),
                    "ConsistentRead": True,
                },
            )
        if source in {"absent", "migrated_between_reads"}:
            stubber.add_response(
                "get_item",
                {"Item": marshal_item(current)} if source == "migrated_between_reads" else {},
                current_get,
            )
        score = await repository.get_affection_score(requester_id, ParticipantSlot.PARTICIPANT_B)
        assert score == (DEFAULT_AFFECTION_SCORE if source == "absent" else 700)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_affection_score_sdk_failure_is_distinct_from_an_absent_profile() -> None:
    sdk = client()
    repository = DynamoDbDebateRepository(
        client=sdk, table_name="test-table", identity_hmac_key=b"i" * 32
    )
    with Stubber(sdk) as stubber:
        stubber.add_client_error("get_item", "ProvisionedThroughputExceededException")
        with pytest.raises(RepositoryUnavailable):
            await repository.get_affection_score("private-requester", ParticipantSlot.PARTICIPANT_A)


def leased_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="channel",
        created_at=NOW,
        attempt_created_at=NOW,
        lease=LeaseGrant("worker", 0, 1, NOW + timedelta(seconds=60)),
    )


def test_native_codec_round_trip_and_decimal_rejection() -> None:
    item: DynamoItem = {"PK": "DEBATE#1", "SK": "META", "count": 1, "active": True}
    assert unmarshal_item(marshal_item(item)) == item

    with pytest.raises(PersistenceFormatError, match="decimal"):
        unmarshal_item({"value": {"N": str(Decimal("1.5"))}})


def test_phase_abandonment_cas_binds_the_observed_claim_version() -> None:
    source = leased_snapshot()
    claimed = OutboxOperation(
        operation_id="terminal-cancelled-0000",
        debate_id=source.state.debate_id,
        attempt_id=source.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="102",
        content="message",
        content_hash=content_sha256("message"),
        nonce="A" * 22,
        chunk_sequence=0,
        status=OutboxStatus.CLAIMED,
        created_at=NOW,
        claim_owner="publisher-one",
        claim_expires_at=NOW + timedelta(seconds=30),
        delivery_attempt=1,
        record_schema_version=2,
        phase=DebatePhase.CANCELLED,
        plan_id="terminal-cancelled",
        delivery_sequence=900,
        deadline_at=NOW + timedelta(minutes=15),
    )

    action = cast(
        dict[str, Any],
        _abandon_outbox_action(
            "test-table",
            claimed,
            at=NOW + timedelta(seconds=31),
            reason=DeliveryAbandonReason.CANCELLED,
        ),
    )["Update"]

    condition = action["ConditionExpression"]
    assert "delivery_attempt=:delivery_attempt" in condition
    assert "claim_owner=:claim_owner" in condition
    assert "claim_expiry=:claim_expiry" in condition
    assert "deadline_at=:deadline" in condition


def test_transaction_preflight_rejects_aggregate_larger_than_four_mb() -> None:
    large_value = "x" * (390 * 1024)
    actions = cast(
        list[TransactWriteItemTypeDef],
        [
            {
                "Put": {
                    "TableName": "test-table",
                    "Item": marshal_item(
                        {
                            "PK": f"DEBATE#{index}",
                            "SK": "META",
                            "payload": large_value,
                        }
                    ),
                }
            }
            for index in range(11)
        ],
    )

    with pytest.raises(RepositoryConflict, match="4 MB"):
        require_repository_transaction_size(actions)


@pytest.mark.parametrize(
    "validator",
    (require_repository_transaction_size, require_outbox_transaction_size),
)
def test_transaction_preflight_enforces_the_dynamodb_action_limit(
    validator: Any,
) -> None:
    action = cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": "test-table",
                "Key": marshal_item({"PK": "CONTROL", "SK": "LOCK"}),
                "ConditionExpression": "attribute_exists(PK)",
            }
        },
    )

    with pytest.raises(RepositoryConflict, match="action count"):
        validator([])
    validator([action] * 100)
    with pytest.raises(RepositoryConflict, match="action count"):
        validator([action] * 101)


@pytest.mark.asyncio
async def test_transaction_cancellation_maps_to_repository_conflict() -> None:
    sdk = client()
    repository = DynamoDbDebateRepository(
        client=sdk,
        table_name="test-table",
        identity_hmac_key=b"i" * 32,
    )
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message="conditional request failed",
            http_status_code=400,
        )
        with pytest.raises(RepositoryConflict, match="condition failed"):
            await repository.renew_lease(
                expected=leased_snapshot(),
                at=NOW + timedelta(seconds=20),
            )

        stubber.assert_no_pending_responses()
