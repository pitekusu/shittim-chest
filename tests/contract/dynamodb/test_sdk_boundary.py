"""SDK-level contracts that DynamoDB Local cannot reproduce reliably."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.repository import DynamoDbDebateRepository
from shittim_chest.adapters.dynamodb.serializer import DynamoItem, PersistenceFormatError
from shittim_chest.application import DebateSnapshot, LeaseGrant
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import AttemptId, DebateId, DebateState

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=UTC)


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def leased_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="question",
        requester_id="requester",
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


@pytest.mark.asyncio
async def test_transaction_cancellation_maps_to_repository_conflict() -> None:
    sdk = client()
    repository = DynamoDbDebateRepository(client=sdk, table_name="test-table")
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
