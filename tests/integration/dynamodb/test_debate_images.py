"""Real image phase staging, ordered outbox completion and durable receipts."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4, uuid7

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import DynamoDbIngressRepository, DynamoDbOutboxRepository
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import serialize_outbox
from shittim_chest.application import (
    DeliveryAbandonReason,
    DiscordBotSlot,
    IngressClaimFence,
    IngressRequest,
    IngressStatus,
    OutboxActivity,
    OutboxOperation,
    OutboxStatus,
    PhaseDeliveryPlan,
    prepare_initial_opinion_outbox_operations,
)
from shittim_chest.application.discord import content_sha256, nonce_from_uuid7
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import DebatePhase, InitialOpinion, ParticipantSlot
from shittim_chest.domain.attachments import DebateImage
from tests.integration.dynamodb.test_repository import (
    NOW,
    DynamoDbDebateRepository,
    finalize_terminal_delivery,
    new_snapshot,
)


def image() -> DebateImage:
    identity = str(uuid4())
    return DebateImage(
        identity,
        "image/png",
        123,
        "a" * 64,
        10,
        20,
        f"debate-images/{identity}/original.png",
        f"debate-images/{identity}/vision.webp",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("image_count", [1, 2, 3])
@pytest.mark.parametrize("expired", [False, True], ids=["deliver", "expire"])
async def test_saved_image_sequences_resume_through_initial_opinion_delivery(
    dynamodb_client: DynamoDBClient, dynamodb_table: str, image_count: int, expired: bool
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = IngressRequest.new_debate(
        interaction_id="interaction-image-accept",
        operation_id="image-accept",
        application_id="201",
        question="fixture question",
        requester_id="301",
        requester_username="fixture",
        requester_display_name="Fixture User",
        guild_id="401",
        channel_id="104",
        command_name="shittim",
        created_at=NOW,
    )
    await ingress.enqueue(request)
    claimed_request = await ingress.claim(
        request=request, claim_owner="runtime-instance", at=NOW + timedelta(microseconds=1)
    )
    assert claimed_request is not None
    request = claimed_request
    fence = IngressClaimFence.from_claimed_request(
        request, claim_owner="runtime-instance", write_at=NOW + timedelta(microseconds=2)
    )
    images = tuple(image() for _ in range(image_count))
    source = replace(
        new_snapshot(),
        question=request.question or "fixture question",
        requester_id=request.requester_id,
        requester_username=request.requester_username,
        requester_display_name=request.requester_display_name,
        guild_id=request.guild_id,
        channel_id=request.channel_id,
        attachments=images,
        thread_id="101",
        starter_message_id="102",
        control_panel_message_id="103",
    )
    accepted = await debates.create(
        source, operation_id="image-accept", lease_owner="runtime", ingress_claim=fence
    )
    accepted_request = await ingress.mark_accepted(
        request=request,
        claim_owner="runtime-instance",
        at=NOW + timedelta(microseconds=3),
        debate_id=accepted.state.debate_id,
        attempt_id=accepted.state.attempt_id,
    )
    at = NOW + timedelta(seconds=1)
    operations = tuple(
        OutboxOperation(
            operation_id=f"input-image-{image.attachment_id}",
            debate_id=accepted.state.debate_id,
            attempt_id=accepted.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="101",
            content=f"添付画像 {index + 1}/{image_count}",
            content_hash=content_sha256(f"添付画像 {index + 1}/{image_count}"),
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
        delivery_sequences=tuple(range(image_count)),
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
        # Later images cannot overtake an earlier unposted image.
        if index + 1 < image_count:
            assert (
                await outbox.claim(
                    expected=staged,
                    operation_id=operations[index + 1].operation_id,
                    claim_owner="publisher",
                    at=at + timedelta(seconds=index + 1),
                )
                is None
            )
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
    sent_images = [
        await outbox.get(
            debate_id=reread.state.debate_id,
            attempt_id=reread.state.attempt_id,
            operation_id=operation.operation_id,
        )
        for operation in operations
    ]
    assert all(sent is not None and sent.status is OutboxStatus.SENT for sent in sent_images)

    preparing = await debates.settle_affection(
        expected=reread, scores=None, at=NOW + timedelta(seconds=9)
    )
    collecting = await debates.replace(
        expected=preparing,
        updated=replace(
            preparing,
            state=preparing.state.transition_to(
                DebatePhase.COLLECTING_INITIAL_OPINIONS, at=NOW + timedelta(seconds=10)
            ),
            initial_opinions=tuple(
                InitialOpinion(participant, "fixture summary", "fixture proposal")
                for participant in ParticipantSlot
            ),
        ),
    )
    opinion_at = NOW + timedelta(seconds=11)
    opinion_operations = prepare_initial_opinion_outbox_operations(
        snapshot=collecting, created_at=opinion_at
    )
    # Preserve the production collision: the SENT image and first opinion both
    # retain raw delivery_sequence=0. No stored row is renumbered for recovery.
    assert operations[0].delivery_sequence == opinion_operations[0].delivery_sequence == 0
    assert tuple(operation.delivery_sequence for operation in opinion_operations) == (0, 8, 16)
    opinion_plan = PhaseDeliveryPlan(
        plan_id="initial-opinions",
        source_phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
        target_phase=DebatePhase.DISCUSSING,
        operation_ids=tuple(operation.operation_id for operation in opinion_operations),
        content_hashes=tuple(operation.content_hash for operation in opinion_operations),
        delivery_sequences=(0, 8, 16),
        staged_at=opinion_at,
        deadline_at=opinion_at + timedelta(minutes=15),
    )
    opinion_staged = await debates.stage_terminal_delivery(
        expected=collecting,
        staged=replace(
            collecting,
            state=replace(collecting.state, updated_at=opinion_at),
            terminal_delivery=opinion_plan,
        ),
        operations=opinion_operations,
    )
    # Recreate process repositories after both plans are already durable.
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    current_opinions = await debates.get(opinion_staged.state.debate_id)
    assert current_opinions == opinion_staged
    assert (
        await outbox.list_pending(
            debate_id=opinion_staged.state.debate_id, attempt_id=opinion_staged.state.attempt_id
        )
        == opinion_operations
    )
    for operation in operations:
        assert (
            await outbox.claim(
                expected=opinion_staged,
                operation_id=operation.operation_id,
                claim_owner="publisher",
                at=opinion_at,
            )
            is None
        )
    if expired:
        expired_at = opinion_plan.deadline_at + timedelta(seconds=1)
        # Recover through the normal fenced lease path after the original lease
        # and delivery deadline have elapsed, retaining every saved raw sequence.
        recovered = await debates.claim_recoverable(lease_owner="recovery", at=expired_at)
        assert len(recovered) == 1
        recovery = recovered[0]
        assert recovery.state.attempt_id == opinion_staged.state.attempt_id
        assert recovery.terminal_delivery == opinion_plan
        for operation in opinion_operations:
            assert (
                await outbox.claim(
                    expected=recovery,
                    operation_id=operation.operation_id,
                    claim_owner="publisher",
                    at=expired_at,
                )
                is None
            )
        terminating = await debates.terminate_terminal_delivery(
            expected=recovery, at=expired_at, reason=DeliveryAbandonReason.DEADLINE_EXCEEDED
        )
        abandoned = await debates.abandon_terminal_delivery(
            expected=terminating,
            at=expired_at + timedelta(seconds=1),
            reason=DeliveryAbandonReason.DEADLINE_EXCEEDED,
        )
        assert await outbox.activity() == OutboxActivity()
        final = await finalize_terminal_delivery(
            debates=debates,
            outbox=outbox,
            expected=abandoned,
            target_phase=DebatePhase.FAILED,
            staged_at=expired_at + timedelta(seconds=2),
            terminal_at=expired_at + timedelta(seconds=3),
            error_code="discord_outbox_deadline_exceeded",
        )
        assert final.state.phase is DebatePhase.FAILED and final.lease is None
        assert final.error_code == "discord_outbox_deadline_exceeded"
        for operation in opinion_operations:
            abandoned_operation = await outbox.get(
                debate_id=final.state.debate_id,
                attempt_id=final.state.attempt_id,
                operation_id=operation.operation_id,
            )
            assert abandoned_operation is not None
            assert abandoned_operation.status is OutboxStatus.ABANDONED
            assert (
                abandoned_operation.delivery_attempt == 0 and abandoned_operation.message_id is None
            )
        terminal_replay = await ingress.get_replay(accepted_request)
        assert (
            terminal_replay is not None and terminal_replay.request.status is IngressStatus.FAILED
        )
        terminal_operation = await ingress.get_operation_result(accepted_request.interaction_id)
        assert terminal_operation is not None and terminal_operation.status is IngressStatus.FAILED
        active_response = dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item({"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"}),
            ConsistentRead=True,
        )
        assert unmarshal_item(active_response["Item"])["count"] == 0
    else:
        for index, operation in enumerate(opinion_operations):
            claimed = await outbox.claim(
                expected=opinion_staged,
                operation_id=operation.operation_id,
                claim_owner="publisher",
                at=opinion_at + timedelta(seconds=index + 1),
            )
            assert claimed is not None
            await outbox.mark_sent(
                expected=opinion_staged,
                operation=claimed,
                message_id=str(2000 + index),
                at=opinion_at + timedelta(seconds=index + 1, microseconds=1),
            )
        final = await debates.finalize_phase_delivery(
            expected=opinion_staged,
            updated=replace(
                opinion_staged,
                state=opinion_staged.state.transition_to(
                    DebatePhase.DISCUSSING, at=NOW + timedelta(seconds=16)
                ),
                terminal_delivery=None,
            ),
        )
        assert final.state.phase is DebatePhase.DISCUSSING
        assert final.lease == opinion_staged.lease
    assert await debates.get(final.state.debate_id) == final
    assert final.attachments == images and final.attachment_message_ids == tuple(receipts)
    assert await outbox.activity() == OutboxActivity()
    assert (
        await outbox.list_pending(
            debate_id=final.state.debate_id, attempt_id=final.state.attempt_id
        )
        == ()
    )
    for operation, original_sent in zip(operations, sent_images, strict=True):
        assert (
            await outbox.get(
                debate_id=final.state.debate_id,
                attempt_id=final.state.attempt_id,
                operation_id=operation.operation_id,
            )
            == original_sent
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_id", ["input-images", "initial-opinions"])
async def test_same_plan_sequence_duplicates_remain_rejected(
    dynamodb_client: DynamoDBClient, dynamodb_table: str, plan_id: str
) -> None:
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    snapshot = new_snapshot()
    for index in range(2):
        attachment = image()
        operation = OutboxOperation(
            operation_id=(
                f"input-image-{attachment.attachment_id}"
                if plan_id == "input-images"
                else f"fixture-opinion-{index}"
            ),
            debate_id=snapshot.state.debate_id,
            attempt_id=snapshot.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR
            if plan_id == "input-images"
            else DiscordBotSlot.PARTICIPANT_A,
            thread_id="101",
            content="fixture delivery",
            content_hash=content_sha256("fixture delivery"),
            nonce=nonce_from_uuid7(uuid7()),
            chunk_sequence=0,
            status=OutboxStatus.PREPARED,
            created_at=NOW,
            record_schema_version=2,
            phase=DebatePhase.SCORING_AFFECTION
            if plan_id == "input-images"
            else DebatePhase.DISCUSSING,
            plan_id=plan_id,
            delivery_sequence=0,
            deadline_at=NOW + timedelta(minutes=15),
            attachments=(attachment,) if plan_id == "input-images" else (),
        )
        dynamodb_client.put_item(
            TableName=dynamodb_table, Item=marshal_item(serialize_outbox(operation))
        )
    with pytest.raises(RepositoryConflict, match="outbox delivery sequence is duplicated"):
        await outbox.list_pending(
            debate_id=snapshot.state.debate_id, attempt_id=snapshot.state.attempt_id
        )
