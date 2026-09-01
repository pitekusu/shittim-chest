"""SDK exception boundary for Discord Outbox completion."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import ANY, Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.outbox import DynamoDbOutboxRepository
from shittim_chest.adapters.dynamodb.serializer import serialize_outbox
from shittim_chest.application import (
    DebateSnapshot,
    DiscordBotSlot,
    LeaseGrant,
    OutboxOperation,
    OutboxStatus,
)
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.domain import AttemptId, DebateId, DebateState

NOW = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
TABLE_NAME = "test-table"


def _client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def _claimed_operation() -> tuple[DebateSnapshot, OutboxOperation]:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    expected = DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="requester",
        guild_id="guild",
        channel_id="channel",
        created_at=NOW,
        attempt_created_at=NOW,
        lease=LeaseGrant("runtime", 0, 1, NOW + timedelta(minutes=10)),
    )
    content = "content"
    operation = OutboxOperation(
        operation_id="operation-1",
        debate_id=debate_id,
        attempt_id=attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="101",
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        nonce="A" * 22,
        chunk_sequence=0,
        status=OutboxStatus.PREPARED,
        created_at=NOW,
    )
    return expected, replace(
        operation,
        status=OutboxStatus.CLAIMED,
        claim_owner="publisher",
        claim_expires_at=NOW + timedelta(minutes=1),
        delivery_attempt=1,
    )


@pytest.mark.asyncio
async def test_mark_sent_hides_provider_error_details_behind_repository_boundary() -> None:
    sdk = _client()
    repository = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    expected, operation = _claimed_operation()
    operation_key = {
        "PK": f"DEBATE#{operation.debate_id}",
        "SK": f"ATTEMPT#{operation.attempt_id}#OUTBOX#{operation.operation_id}",
    }

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_outbox(operation))},
            {
                "TableName": TABLE_NAME,
                "Key": marshal_item(operation_key),
                "ConsistentRead": True,
            },
        )
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="InternalServerError",
            service_message="provider detail must stay private",
            http_status_code=500,
            expected_params={
                "TransactItems": ANY,
                "ClientRequestToken": ANY,
                "ReturnConsumedCapacity": "NONE",
            },
        )

        with pytest.raises(RepositoryUnavailable, match=r"^repository_unavailable$"):
            await repository.mark_sent(
                expected=expected,
                operation=operation,
                message_id="message-1",
                at=NOW + timedelta(seconds=1),
            )
        stubber.assert_no_pending_responses()
