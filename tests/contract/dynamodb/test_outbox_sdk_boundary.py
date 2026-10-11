"""SDK boundaries for Discord Outbox ordering and completion."""

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
from mypy_boto3_dynamodb.type_defs import TransactWriteItemsInputTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.outbox import DynamoDbOutboxRepository
from shittim_chest.adapters.dynamodb.serializer import serialize_outbox
from shittim_chest.application import (
    DebateSnapshot,
    DiscordBotSlot,
    LeaseGrant,
    OutboxOperation,
    OutboxStatus,
    PhaseDeliveryPlan,
)
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState
from shittim_chest.domain.attachments import DebateImage

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


def _phase_operations(
    image_count: int,
    *,
    sent_images: bool = False,
) -> tuple[DebateSnapshot, tuple[OutboxOperation, ...], tuple[OutboxOperation, ...]]:
    base, claimed = _claimed_operation()
    prepared = replace(
        claimed,
        status=OutboxStatus.PREPARED,
        claim_owner=None,
        claim_expires_at=None,
        delivery_attempt=0,
    )
    images = []
    for index in range(image_count):
        attachment_id = f"10000000-0000-5000-8000-{index:011x}a"
        image = DebateImage(
            attachment_id=attachment_id,
            content_type="image/png",
            size_bytes=1,
            sha256="a" * 64,
            width=1,
            height=1,
            asset_key=f"debate-images/{attachment_id}/original.png",
            vision_asset_key=f"debate-images/{attachment_id}/vision.webp",
        )
        operation = replace(
            prepared,
            operation_id=f"input-image-{attachment_id}",
            nonce=chr(ord("A") + index) * 22,
            phase=DebatePhase.SCORING_AFFECTION,
            plan_id="input-images",
            record_schema_version=2,
            deadline_at=NOW + timedelta(minutes=15),
            chunk_sequence=index,
            delivery_sequence=index,
            attachments=(image,),
        )
        if sent_images:
            operation = replace(
                operation,
                status=OutboxStatus.SENT,
                message_id=str(201 + index),
                sent_at=NOW + timedelta(seconds=1),
                delivery_attempt=1,
            )
        images.append(operation)
    opinions = tuple(
        replace(
            prepared,
            operation_id=f"initial-opinion-{slot.value}",
            bot_slot=slot,
            nonce=chr(ord("D") + index) * 22,
            phase=DebatePhase.DISCUSSING,
            plan_id="initial-opinions",
            record_schema_version=2,
            deadline_at=NOW + timedelta(minutes=15),
            chunk_sequence=0,
            delivery_sequence=index * 8,
        )
        for index, slot in enumerate(
            (
                DiscordBotSlot.PARTICIPANT_A,
                DiscordBotSlot.PARTICIPANT_B,
                DiscordBotSlot.PARTICIPANT_C,
            )
        )
    )
    expected = replace(
        base,
        state=replace(base.state, phase=DebatePhase.COLLECTING_INITIAL_OPINIONS),
        starter_message_id="102",
        thread_id="101",
        control_panel_message_id="103",
        attachments=tuple(operation.attachments[0] for operation in images),
        terminal_delivery=PhaseDeliveryPlan(
            plan_id="initial-opinions",
            source_phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
            target_phase=DebatePhase.DISCUSSING,
            operation_ids=tuple(operation.operation_id for operation in opinions),
            content_hashes=tuple(operation.content_hash for operation in opinions),
            delivery_sequences=(0, 8, 16),
            staged_at=NOW,
            deadline_at=NOW + timedelta(minutes=15),
        ),
    )
    return expected, tuple(images), opinions


def _stub_attempt_query(
    stubber: Stubber,
    expected: DebateSnapshot,
    operations: tuple[OutboxOperation, ...],
) -> None:
    stubber.add_response(
        "query",
        {
            "Items": [
                marshal_item(serialize_outbox(operation)) for operation in reversed(operations)
            ]
        },
        {
            "TableName": TABLE_NAME,
            "KeyConditionExpression": "PK=:pk AND begins_with(SK,:prefix)",
            "ExpressionAttributeValues": marshal_item(
                {
                    ":pk": f"DEBATE#{expected.state.debate_id}",
                    ":prefix": f"ATTEMPT#{expected.state.attempt_id}#OUTBOX#",
                }
            ),
            "ConsistentRead": True,
        },
    )


def _stub_operation_get(stubber: Stubber, operation: OutboxOperation) -> None:
    stubber.add_response(
        "get_item",
        {"Item": marshal_item(serialize_outbox(operation))},
        {
            "TableName": TABLE_NAME,
            "Key": marshal_item(
                {
                    "PK": f"DEBATE#{operation.debate_id}",
                    "SK": f"ATTEMPT#{operation.attempt_id}#OUTBOX#{operation.operation_id}",
                }
            ),
            "ConsistentRead": True,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("image_count", (1, 2, 3))
async def test_saved_sent_images_share_raw_sequence_zero_without_blocking_initial_claim(
    image_count: int,
) -> None:
    sdk = _client()
    repository = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    expected, images, opinions = _phase_operations(image_count, sent_images=True)
    captured: list[TransactWriteItemsInputTypeDef] = []

    def capture(params: TransactWriteItemsInputTypeDef, **_kwargs: object) -> None:
        captured.append(params)

    sdk.meta.events.register("before-parameter-build.dynamodb.TransactWriteItems", capture)
    claimed = replace(
        opinions[0],
        status=OutboxStatus.CLAIMED,
        claim_owner="publisher",
        claim_expires_at=NOW + timedelta(minutes=1),
        delivery_attempt=1,
    )
    with Stubber(sdk) as stubber:
        _stub_attempt_query(stubber, expected, (*images, *opinions))
        pending = await repository.list_pending(
            debate_id=expected.state.debate_id, attempt_id=expected.state.attempt_id
        )
        assert pending == opinions
        assert tuple(operation.delivery_sequence for operation in pending) == (0, 8, 16)
        _stub_operation_get(stubber, opinions[0])
        _stub_attempt_query(stubber, expected, (*images, *opinions))
        stubber.add_response(
            "transact_write_items",
            {},
            {"TransactItems": ANY, "ClientRequestToken": ANY, "ReturnConsumedCapacity": "NONE"},
        )
        _stub_operation_get(stubber, claimed)
        result = await repository.claim(
            expected=expected,
            operation_id=opinions[0].operation_id,
            claim_owner="publisher",
            at=NOW,
        )
        assert result == claimed
        assert len(captured) == 1
        operation_updates = [
            action["Update"]
            for action in captured[0]["TransactItems"]
            if "Update" in action
            and action["Update"]["Key"]["SK"]
            == {"S": f"ATTEMPT#{expected.state.attempt_id}#OUTBOX#{opinions[0].operation_id}"}
        ]
        assert len(operation_updates) == 1
        assert operation_updates[0]["ExpressionAttributeValues"][":delivery_sequence"] == {"N": "0"}
        assert images[0].delivery_sequence == 0
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_count", (1, 2, 3))
async def test_pending_images_are_ordered_first_and_initial_opinions_cannot_overtake(
    image_count: int,
) -> None:
    sdk = _client()
    repository = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    expected, images, opinions = _phase_operations(image_count)
    with Stubber(sdk) as stubber:
        _stub_attempt_query(stubber, expected, (*images, *opinions))
        pending = await repository.list_pending(
            debate_id=expected.state.debate_id, attempt_id=expected.state.attempt_id
        )
        assert pending == (*images, *opinions)
        assert tuple(operation.delivery_sequence for operation in pending[:image_count]) == tuple(
            range(image_count)
        )
        _stub_operation_get(stubber, opinions[0])
        _stub_attempt_query(stubber, expected, (*images, *opinions))
        assert (
            await repository.claim(
                expected=expected,
                operation_id=opinions[0].operation_id,
                claim_owner="publisher",
                at=NOW,
            )
            is None
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_count", (2, 3))
async def test_later_input_image_cannot_overtake_its_pending_predecessor(image_count: int) -> None:
    sdk = _client()
    repository = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    expected, images, opinions = _phase_operations(image_count)
    expected = replace(
        expected,
        state=replace(expected.state, phase=DebatePhase.ACCEPTED),
        terminal_delivery=PhaseDeliveryPlan(
            plan_id="input-images",
            source_phase=DebatePhase.ACCEPTED,
            target_phase=DebatePhase.SCORING_AFFECTION,
            operation_ids=tuple(operation.operation_id for operation in images),
            content_hashes=tuple(operation.content_hash for operation in images),
            delivery_sequences=tuple(range(image_count)),
            staged_at=NOW,
            deadline_at=NOW + timedelta(minutes=15),
        ),
    )
    with Stubber(sdk) as stubber:
        _stub_operation_get(stubber, images[-1])
        _stub_attempt_query(stubber, expected, (*images, *opinions))
        assert (
            await repository.claim(
                expected=expected,
                operation_id=images[-1].operation_id,
                claim_owner="publisher",
                at=NOW,
            )
            is None
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ("input-images", "initial-opinions"))
async def test_duplicate_sequences_still_reject_each_image_or_normal_plan(plan: str) -> None:
    sdk = _client()
    repository = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    expected, images, opinions = _phase_operations(2)
    duplicate = replace(
        images[1] if plan == "input-images" else opinions[1],
        delivery_sequence=0,
        chunk_sequence=0,
    )
    operations = (images[0], duplicate) if plan == "input-images" else (opinions[0], duplicate)
    with Stubber(sdk) as stubber:
        _stub_attempt_query(stubber, expected, operations)
        with pytest.raises(RepositoryConflict, match="outbox delivery sequence is duplicated"):
            await repository.list_pending(
                debate_id=expected.state.debate_id, attempt_id=expected.state.attempt_id
            )
        stubber.assert_no_pending_responses()
