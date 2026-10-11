"""Real image phase staging, ordered outbox completion and durable receipts."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4, uuid7

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import DynamoDbOutboxRepository
from shittim_chest.application import (
    DiscordBotSlot,
    OutboxOperation,
    OutboxStatus,
    PhaseDeliveryPlan,
)
from shittim_chest.application.discord import content_sha256, nonce_from_uuid7
from shittim_chest.domain import DebatePhase
from shittim_chest.domain.attachments import DebateImage
from tests.integration.dynamodb.test_repository import (
    NOW,
    DynamoDbDebateRepository,
    new_snapshot,
)


@pytest.mark.asyncio
async def test_three_images_stage_complete_and_keep_receipts_before_first_model_phase(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    identities = tuple(str(uuid4()) for _ in range(3))
    images = tuple(
        DebateImage(
            identity,
            "image/png",
            123,
            "a" * 64,
            10,
            20,
            f"debate-images/{identity}/original.png",
            f"debate-images/{identity}/vision.webp",
        )
        for identity in identities
    )
    source = replace(
        new_snapshot(),
        attachments=images,
        thread_id="101",
        starter_message_id="102",
        control_panel_message_id="103",
    )
    accepted = await debates.create(source, operation_id="image-accept", lease_owner="runtime")
    at = NOW + timedelta(seconds=1)
    operations = tuple(
        OutboxOperation(
            operation_id=f"input-image-{image.attachment_id}",
            debate_id=accepted.state.debate_id,
            attempt_id=accepted.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="101",
            content=f"添付画像 {index + 1}/3",
            content_hash=content_sha256(f"添付画像 {index + 1}/3"),
            nonce=nonce_from_uuid7(uuid7()),
            chunk_sequence=index,
            status=OutboxStatus.PREPARED,
            created_at=at,
            record_schema_version=2,
            phase=DebatePhase.SCORING_AFFECTION,
            plan_id="input-images",
            delivery_sequence=index,
            deadline_at=at + timedelta(minutes=15),
            attachments=(image,),
        )
        for index, image in enumerate(images)
    )
    plan = PhaseDeliveryPlan(
        plan_id="input-images",
        source_phase=DebatePhase.ACCEPTED,
        target_phase=DebatePhase.SCORING_AFFECTION,
        operation_ids=tuple(operation.operation_id for operation in operations),
        content_hashes=tuple(operation.content_hash for operation in operations),
        delivery_sequences=(0, 1, 2),
        staged_at=at,
        deadline_at=at + timedelta(minutes=15),
    )
    staged = await debates.stage_terminal_delivery(
        expected=accepted,
        staged=replace(
            accepted, state=replace(accepted.state, updated_at=at), terminal_delivery=plan
        ),
        operations=operations,
    )
    receipts = []
    for index, operation in enumerate(operations):
        claim = await outbox.claim(
            expected=staged,
            operation_id=operation.operation_id,
            claim_owner="publisher",
            at=at + timedelta(seconds=index + 1),
        )
        assert claim is not None
        message_id = str(1000 + index)
        await outbox.mark_sent(
            expected=staged,
            operation=claim,
            message_id=message_id,
            at=at + timedelta(seconds=index + 2),
        )
        receipts.append((operation.attachments[0].attachment_id, message_id))
    current = await debates.record_image_receipts(
        expected=staged, receipts=tuple(receipts), at=at + timedelta(seconds=6)
    )
    finalized = await debates.finalize_phase_delivery(
        expected=current,
        updated=replace(
            current,
            state=current.state.transition_to(
                DebatePhase.SCORING_AFFECTION, at=at + timedelta(seconds=7)
            ),
            terminal_delivery=None,
        ),
    )
    reread = await debates.get(finalized.state.debate_id)
    assert reread is not None
    assert reread.state.phase is DebatePhase.SCORING_AFFECTION
    assert reread.attachments == images and reread.attachment_message_ids == tuple(receipts)
    assert (
        await outbox.list_pending(
            debate_id=reread.state.debate_id, attempt_id=reread.state.attempt_id
        )
        == ()
    )
