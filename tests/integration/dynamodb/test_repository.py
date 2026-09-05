"""DynamoDB Local coverage for transactions, leases, indexes, and outbox state."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateRepository as _DynamoDbDebateRepository,
)
from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    DynamoDbOutboxRepository,
    OutboxOperation,
    OutboxStatus,
    ingress_request_sort_key,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.outbox import outbox_activity_action
from shittim_chest.adapters.dynamodb.repository import derive_affection_requester_key
from shittim_chest.adapters.dynamodb.serializer import (
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    serialize_outbox,
    serialize_snapshot,
)
from shittim_chest.application import (
    DebateSnapshot,
    DeliveryAbandonReason,
    DiscordBotSlot,
    DiscordDeliveryTarget,
    GenerationCheckpoint,
    IngressClaimFence,
    IngressKind,
    IngressRequest,
    IngressStatus,
    OutboxActivity,
    PanelRefreshState,
    PhaseDeliveryPlan,
    StatusMessageState,
    StatusPublicationState,
    prepare_final_proposal_outbox_operations,
    prepare_initial_opinion_outbox_operations,
    prepare_terminal_outbox_operations,
    prepare_vote_outbox_operations,
)
from shittim_chest.application.ports import (
    RepositoryBusy,
    RepositoryCancellationCode,
    RepositoryClaimLost,
    RepositoryConflict,
    RepositoryQuotaExceeded,
    RepositoryTransactionAction,
    RepositoryTransactionConflict,
    RepositoryTransactionStage,
)
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
)

NOW = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)
IDENTITY_HMAC_KEY = b"i" * 32
DISPLAY_NAMES = {
    ParticipantSlot.PARTICIPANT_A: "Generic A",
    ParticipantSlot.PARTICIPANT_B: "Generic B",
    ParticipantSlot.PARTICIPANT_C: "Generic C",
}


class DynamoDbDebateRepository(_DynamoDbDebateRepository):
    """Bind the non-secret test identity key for integration coverage."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        super().__init__(
            client=client,
            table_name=table_name,
            identity_hmac_key=IDENTITY_HMAC_KEY,
        )
        self._legacy_profile_to_insert: DynamoItem | None = None

    def insert_legacy_profile_before_next_transaction(self, item: DynamoItem) -> None:
        self._legacy_profile_to_insert = item

    def _transact(
        self,
        actions: Iterable[TransactWriteItemTypeDef],
        *,
        token: str,
        cancellation_stage: RepositoryTransactionStage | None = None,
        cancellation_action_kinds: tuple[RepositoryTransactionAction, ...] = (),
    ) -> None:
        legacy_item = self._legacy_profile_to_insert
        if legacy_item is not None:
            self._legacy_profile_to_insert = None
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(legacy_item),
            )
        super()._transact(
            actions,
            token=token,
            cancellation_stage=cancellation_stage,
            cancellation_action_kinds=cancellation_action_kinds,
        )


def affection_profile_partition(requester_id: str) -> str:
    requester_key = derive_affection_requester_key(IDENTITY_HMAC_KEY, requester_id)
    return f"AFFECTION#REQUESTER#{requester_key}"


def votes_for_winner(winner: ParticipantSlot) -> tuple[Vote, ...]:
    others = tuple(participant for participant in ParticipantSlot if participant is not winner)
    return (
        Vote(winner, others[0], 3, 3, 3, "winner vote"),
        Vote(others[0], winner, 5, 5, 5, "support one"),
        Vote(others[1], winner, 4, 4, 4, "support two"),
    )


def new_snapshot(*, offset: int = 0) -> DebateSnapshot:
    created = NOW + timedelta(seconds=offset)
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=created),
        question=f"question-{offset}",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="channel",
        created_at=created,
        attempt_created_at=created,
    )


async def begin_affection_scoring(
    repository: DynamoDbDebateRepository,
    snapshot: DebateSnapshot,
    *,
    at: datetime,
) -> DebateSnapshot:
    """Enter scoring through the real repository, preserving its CAS checks."""

    return await repository.replace(
        expected=snapshot,
        updated=replace(
            snapshot,
            state=snapshot.state.transition_to(DebatePhase.SCORING_AFFECTION, at=at),
        ),
    )


async def settle_unavailable_affection(
    repository: DynamoDbDebateRepository,
    snapshot: DebateSnapshot,
    *,
    scoring_at: datetime,
) -> DebateSnapshot:
    scoring = await begin_affection_scoring(repository, snapshot, at=scoring_at)
    return await repository.settle_affection(
        expected=scoring,
        scores=None,
        at=scoring_at + timedelta(microseconds=1),
    )


async def claimed_ingress(
    *,
    repository: DynamoDbIngressRepository,
    operation_id: str,
    kind: IngressKind = IngressKind.NEW_DEBATE,
    target: DebateSnapshot | None = None,
    created_at: datetime = NOW,
) -> tuple[IngressRequest, IngressClaimFence]:
    if kind is IngressKind.NEW_DEBATE:
        request = IngressRequest.new_debate(
            interaction_id=f"interaction-{operation_id}",
            operation_id=operation_id,
            application_id="application-id",
            question="question",
            requester_id="requester",
            requester_username="requester",
            requester_display_name="Requester",
            guild_id="guild",
            channel_id="channel",
            command_name="shittim",
            created_at=created_at,
        )
    else:
        if target is None:
            raise AssertionError("control ingress requires a target snapshot")
        request = IngressRequest.control_operation(
            interaction_id=f"interaction-{operation_id}",
            operation_id=operation_id,
            kind=kind,
            application_id="application-id",
            requester_id=target.requester_id,
            requester_username=target.requester_username,
            requester_display_name=target.requester_display_name,
            requester_can_manage_messages=False,
            guild_id=target.guild_id,
            channel_id="thread-id",
            parent_channel_id=target.channel_id,
            source_message_id="panel-message-id",
            source_thread_id="thread-id",
            target_debate_id=target.state.debate_id,
            expected_attempt_id=target.state.attempt_id,
            custom_id=f"shittim:v1:{kind.value}:{operation_id}",
            created_at=created_at,
        )
    await repository.enqueue(request)
    claimed = await repository.claim(
        request=request,
        claim_owner="runtime-instance",
        at=created_at + timedelta(microseconds=1),
    )
    assert claimed is not None
    fence = IngressClaimFence.from_claimed_request(
        claimed,
        claim_owner="runtime-instance",
        write_at=created_at + timedelta(microseconds=2),
    )
    return claimed, fence


def delivery_plan(
    operations: tuple[OutboxOperation, ...],
    *,
    plan_id: str,
    source_phase: DebatePhase,
    target_phase: DebatePhase,
    staged_at: datetime,
    deadline_at: datetime | None = None,
) -> PhaseDeliveryPlan:
    """Build a fixture plan; each test still chooses its phase and operations."""

    return PhaseDeliveryPlan(
        plan_id=plan_id,
        source_phase=source_phase,
        target_phase=target_phase,
        operation_ids=tuple(operation.operation_id for operation in operations),
        content_hashes=tuple(operation.content_hash for operation in operations),
        delivery_sequences=tuple(
            operation.delivery_sequence
            for operation in operations
            if operation.delivery_sequence is not None
        ),
        staged_at=staged_at,
        deadline_at=deadline_at or staged_at + timedelta(minutes=15),
    )


async def finalize_terminal_delivery(
    *,
    debates: DynamoDbDebateRepository,
    outbox: DynamoDbOutboxRepository,
    expected: DebateSnapshot,
    target_phase: DebatePhase,
    staged_at: datetime,
    terminal_at: datetime,
    error_code: str | None = None,
    operation_id: str | None = None,
    ingress_claim: IngressClaimFence | None = None,
    persisted_bot_slot_override: DiscordBotSlot | None = None,
    previous_terminal_shape: bool = False,
    previous_completed_without_affection: bool = False,
    legacy_terminal_plan: bool = False,
    dynamodb_client: DynamoDBClient | None = None,
    dynamodb_table: str | None = None,
) -> DebateSnapshot:
    """Drive a bound attempt through the required durable terminal delivery path."""

    operations = prepare_terminal_outbox_operations(
        snapshot=expected,
        target_phase=target_phase,
        created_at=staged_at,
        error_code=error_code,
        participant_display_names=DISPLAY_NAMES,
    )
    plan = delivery_plan(
        operations,
        plan_id=operations[0].plan_id or f"terminal-{target_phase.value}",
        source_phase=expected.state.phase,
        target_phase=target_phase,
        staged_at=staged_at,
    )
    staged = replace(
        expected,
        state=replace(expected.state, updated_at=staged_at),
        terminal_delivery=plan,
        error_code=error_code,
    )
    persisted = await debates.stage_terminal_delivery(
        expected=expected,
        staged=staged,
        operations=operations,
        operation_id=operation_id,
        ingress_claim=ingress_claim,
    )
    if previous_completed_without_affection:
        if (
            target_phase is not DebatePhase.COMPLETED
            or dynamodb_client is None
            or dynamodb_table is None
        ):
            raise AssertionError("previous completed shape requires a DynamoDB table")
        previous_operations = tuple(
            operation
            for operation in operations
            if operation.operation_id != "terminal-completed-affection-0000"
        )
        if len(previous_operations) != len(operations) - 1:
            raise AssertionError("current completed shape must contain one affection post")
        previous_plan = replace(
            plan,
            operation_ids=tuple(operation.operation_id for operation in previous_operations),
            content_hashes=tuple(operation.content_hash for operation in previous_operations),
            delivery_sequences=tuple(
                operation.delivery_sequence
                for operation in previous_operations
                if operation.delivery_sequence is not None
            ),
        )
        affection_operation = operations[-1]
        dynamodb_client.delete_item(
            TableName=dynamodb_table,
            Key=marshal_item(
                {
                    "PK": f"DEBATE#{persisted.state.debate_id}",
                    "SK": (
                        f"ATTEMPT#{persisted.state.attempt_id}#OUTBOX#"
                        f"{affection_operation.operation_id}"
                    ),
                }
            ),
        )
        persisted = replace(persisted, terminal_delivery=previous_plan)
        for item in serialize_snapshot(persisted):
            dynamodb_client.put_item(
                TableName=dynamodb_table,
                Item=marshal_item(item),
            )
        await asyncio.to_thread(
            outbox._transact,
            [
                outbox_activity_action(
                    table_name=dynamodb_table,
                    pending_delta=-1,
                    claimed_delta=0,
                    at=staged_at,
                )
            ],
            f"previous-completed-shape-{persisted.state.attempt_id}",
        )
        operations = previous_operations
        plan = previous_plan
    elif previous_terminal_shape:
        if persisted_bot_slot_override is None or dynamodb_client is None or dynamodb_table is None:
            raise AssertionError("previous terminal shape requires a persisted owner and table")
        previous_operations = tuple(
            replace(
                operation,
                operation_id=f"terminal-{target_phase.value}-{index:04d}",
                bot_slot=persisted_bot_slot_override,
                chunk_sequence=index,
                delivery_sequence=300 + index,
            )
            for index, operation in enumerate(operations[1:])
        )
        previous_plan = replace(
            plan,
            operation_ids=tuple(operation.operation_id for operation in previous_operations),
            content_hashes=tuple(operation.content_hash for operation in previous_operations),
            delivery_sequences=tuple(
                operation.delivery_sequence
                for operation in previous_operations
                if operation.delivery_sequence is not None
            ),
        )
        for operation in operations:
            dynamodb_client.delete_item(
                TableName=dynamodb_table,
                Key=marshal_item(
                    {
                        "PK": f"DEBATE#{persisted.state.debate_id}",
                        "SK": (
                            f"ATTEMPT#{persisted.state.attempt_id}#OUTBOX#{operation.operation_id}"
                        ),
                    }
                ),
            )
        persisted = replace(persisted, terminal_delivery=previous_plan)
        for item in serialize_snapshot(persisted):
            dynamodb_client.put_item(
                TableName=dynamodb_table,
                Item=marshal_item(item),
            )
        for operation in previous_operations:
            dynamodb_client.put_item(
                TableName=dynamodb_table,
                Item=marshal_item(serialize_outbox(operation)),
            )
        activity_delta = len(previous_operations) - len(operations)
        if activity_delta:
            await asyncio.to_thread(
                outbox._transact,
                [
                    outbox_activity_action(
                        table_name=dynamodb_table,
                        pending_delta=activity_delta,
                        claimed_delta=0,
                        at=staged_at,
                    )
                ],
                f"previous-terminal-shape-{persisted.state.attempt_id}",
            )
        operations = previous_operations
        plan = previous_plan
    elif persisted_bot_slot_override is not None:
        if dynamodb_client is None or dynamodb_table is None:
            raise AssertionError("persisted owner override requires a DynamoDB table")
        for operation in operations:
            dynamodb_client.update_item(
                TableName=dynamodb_table,
                Key=marshal_item(
                    {
                        "PK": f"DEBATE#{persisted.state.debate_id}",
                        "SK": (
                            f"ATTEMPT#{persisted.state.attempt_id}#OUTBOX#{operation.operation_id}"
                        ),
                    }
                ),
                UpdateExpression="SET bot_slot=:bot_slot",
                ExpressionAttributeValues=marshal_item(
                    {":bot_slot": persisted_bot_slot_override.value}
                ),
            )
    for index, operation in enumerate(operations):
        claim_at = staged_at + timedelta(microseconds=(index * 2) + 1)
        claimed = await outbox.claim(
            expected=persisted,
            operation_id=operation.operation_id,
            claim_owner="terminal-publisher",
            at=claim_at,
        )
        assert claimed is not None
        await outbox.mark_sent(
            expected=persisted,
            operation=claimed,
            message_id=str(10_000 + index),
            at=claim_at + timedelta(microseconds=1),
        )
    if legacy_terminal_plan:
        if dynamodb_client is None or dynamodb_table is None:
            raise AssertionError("legacy plan conversion requires a DynamoDB table")
        dynamodb_client.update_item(
            TableName=dynamodb_table,
            Key=marshal_item(
                {
                    "PK": f"DEBATE#{persisted.state.debate_id}",
                    "SK": f"ATTEMPT#{persisted.state.attempt_id}#META",
                }
            ),
            UpdateExpression=(
                "REMOVE terminal_delivery_plan_id, terminal_delivery_source, "
                "terminal_delivery_sequences, terminal_delivery_deadline_at, "
                "terminal_delivery_plan_status"
            ),
        )
        dynamodb_client.delete_item(
            TableName=dynamodb_table,
            Key=marshal_item(
                {
                    "PK": f"DEBATE#{persisted.state.debate_id}",
                    "SK": f"ATTEMPT#{persisted.state.attempt_id}#DELIVERY#{plan.plan_id}",
                }
            ),
        )
        reloaded = await debates.get(persisted.state.debate_id)
        if reloaded is None:
            raise AssertionError("legacy plan conversion removed the debate")
        persisted = reloaded
    persisted_plan = persisted.terminal_delivery
    if persisted_plan is None:
        raise AssertionError("terminal delivery plan disappeared")
    terminal = replace(
        persisted,
        state=persisted.state.transition_to(target_phase, at=terminal_at),
        terminal_delivery=persisted_plan.complete(at=terminal_at),
    )
    return await debates.finalize_terminal(expected=persisted, updated=terminal)


@pytest.mark.asyncio
async def test_completed_terminal_finalize_accepts_plan_without_separate_affection_post(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        replace(new_snapshot(), channel_id="100"),
        operation_id="pre-affection-post-plan",
        lease_owner="worker-1",
    )
    scoring = await begin_affection_scoring(debates, accepted, at=NOW + timedelta(microseconds=1))
    assessed = await debates.settle_affection(
        expected=scoring,
        scores=(10, -20, 100),
        at=NOW + timedelta(microseconds=2),
    )
    bound = await debates.replace(
        expected=assessed,
        updated=replace(
            assessed,
            state=replace(
                assessed.state,
                phase=DebatePhase.GENERATING_DECISION,
                updated_at=NOW + timedelta(milliseconds=500),
            ),
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
            final_decision=FinalDecision(
                winner=ParticipantSlot.PARTICIPANT_A,
                decision="fixture decision",
                actions=("fixture action",),
                caveats=("fixture caveat",),
            ),
            votes=votes_for_winner(ParticipantSlot.PARTICIPANT_A),
        ),
    )

    completed = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=bound,
        target_phase=DebatePhase.COMPLETED,
        staged_at=NOW + timedelta(seconds=1),
        terminal_at=NOW + timedelta(seconds=2),
        previous_completed_without_affection=True,
        dynamodb_client=dynamodb_client,
        dynamodb_table=dynamodb_table,
    )

    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.affection_assessment == assessed.affection_assessment
    assert (
        await outbox.get(
            debate_id=completed.state.debate_id,
            attempt_id=completed.state.attempt_id,
            operation_id="terminal-completed-affection-0000",
        )
        is None
    )


@pytest.mark.asyncio
async def test_initial_opinion_delivery_stages_and_finalizes_without_releasing_the_lease(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(),
        operation_id="initial-opinion-accept",
        lease_owner="worker-1",
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        ),
    )
    preparing = await settle_unavailable_affection(
        debates,
        bound,
        scoring_at=NOW + timedelta(seconds=1),
    )
    collecting = await debates.replace(
        expected=preparing,
        updated=replace(
            preparing,
            state=preparing.state.transition_to(
                DebatePhase.COLLECTING_INITIAL_OPINIONS,
                at=NOW + timedelta(seconds=2),
            ),
            initial_opinions=tuple(
                InitialOpinion(participant, "summary", "proposal")
                for participant in ParticipantSlot
            ),
        ),
    )
    staged_at = NOW + timedelta(seconds=3)
    operations = prepare_initial_opinion_outbox_operations(
        snapshot=collecting,
        created_at=staged_at,
    )
    plan = delivery_plan(
        operations,
        plan_id="initial-opinions",
        source_phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
        target_phase=DebatePhase.DISCUSSING,
        staged_at=staged_at,
    )
    staged = await debates.stage_terminal_delivery(
        expected=collecting,
        staged=replace(
            collecting,
            state=replace(collecting.state, updated_at=staged_at),
            terminal_delivery=plan,
        ),
        operations=operations,
    )
    finalized_at = NOW + timedelta(seconds=10)
    finalized_snapshot = replace(
        staged,
        state=staged.state.transition_to(DebatePhase.DISCUSSING, at=finalized_at),
        terminal_delivery=None,
    )
    with pytest.raises(RepositoryTransactionConflict):
        await debates.finalize_phase_delivery(
            expected=staged,
            updated=finalized_snapshot,
        )

    for index, operation in enumerate(operations):
        claimed = await outbox.claim(
            expected=staged,
            operation_id=operation.operation_id,
            claim_owner="initial-opinion-publisher",
            at=staged_at + timedelta(seconds=index + 1),
        )
        assert claimed is not None
        await outbox.mark_sent(
            expected=staged,
            operation=claimed,
            message_id=str(200 + index),
            at=staged_at + timedelta(seconds=index + 1, microseconds=1),
        )

    finalized = await debates.finalize_phase_delivery(
        expected=staged,
        updated=finalized_snapshot,
    )
    assert finalized.state.phase is DebatePhase.DISCUSSING
    assert finalized.terminal_delivery is None
    assert finalized.lease == staged.lease
    assert await debates.get(finalized.state.debate_id) == finalized
    assert await outbox.activity() == OutboxActivity()
    plan_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"DEBATE#{staged.state.debate_id}",
                "SK": f"ATTEMPT#{staged.state.attempt_id}#DELIVERY#initial-opinions",
            }
        ),
        ConsistentRead=True,
    )
    assert unmarshal_item(plan_response["Item"])["status"] == "delivered"


@pytest.mark.asyncio
async def test_final_proposal_delivery_stages_and_finalizes_in_its_reserved_range(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(offset=20),
        operation_id="final-proposal-accept",
        lease_owner="worker-1",
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            starter_message_id="301",
            thread_id="302",
            control_panel_message_id="303",
        ),
    )
    preparing = await settle_unavailable_affection(
        debates,
        bound,
        scoring_at=NOW + timedelta(seconds=21),
    )
    collecting_initial = await debates.replace(
        expected=preparing,
        updated=replace(
            preparing,
            state=preparing.state.transition_to(
                DebatePhase.COLLECTING_INITIAL_OPINIONS,
                at=NOW + timedelta(seconds=22),
            ),
            initial_opinions=tuple(
                InitialOpinion(participant, "summary", "proposal")
                for participant in ParticipantSlot
            ),
        ),
    )
    discussing = await debates.replace(
        expected=collecting_initial,
        updated=replace(
            collecting_initial,
            state=collecting_initial.state.transition_to(
                DebatePhase.DISCUSSING,
                at=NOW + timedelta(seconds=23),
            ),
        ),
    )
    collecting_final = await debates.replace(
        expected=discussing,
        updated=replace(
            discussing,
            state=discussing.state.transition_to(
                DebatePhase.COLLECTING_FINAL_PROPOSALS,
                at=NOW + timedelta(seconds=24),
            ),
            final_proposals=tuple(
                FinalProposal(participant, "title", "proposal") for participant in ParticipantSlot
            ),
        ),
    )
    staged_at = NOW + timedelta(seconds=25)
    operations = prepare_final_proposal_outbox_operations(
        snapshot=collecting_final,
        created_at=staged_at,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (100, 108, 116)
    plan = delivery_plan(
        operations,
        plan_id="final-proposals",
        source_phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
        target_phase=DebatePhase.SELECTING_WINNER,
        staged_at=staged_at,
    )
    staged = await debates.stage_terminal_delivery(
        expected=collecting_final,
        staged=replace(
            collecting_final,
            state=replace(collecting_final.state, updated_at=staged_at),
            terminal_delivery=plan,
        ),
        operations=operations,
    )
    for index, operation in enumerate(operations):
        claimed = await outbox.claim(
            expected=staged,
            operation_id=operation.operation_id,
            claim_owner="final-proposal-publisher",
            at=staged_at + timedelta(seconds=index + 1),
        )
        assert claimed is not None
        await outbox.mark_sent(
            expected=staged,
            operation=claimed,
            message_id=str(400 + index),
            at=staged_at + timedelta(seconds=index + 1, microseconds=1),
        )
    finalized_at = NOW + timedelta(seconds=30)
    finalized = await debates.finalize_phase_delivery(
        expected=staged,
        updated=replace(
            staged,
            state=staged.state.transition_to(
                DebatePhase.SELECTING_WINNER,
                at=finalized_at,
            ),
            terminal_delivery=None,
        ),
    )
    assert finalized.state.phase is DebatePhase.SELECTING_WINNER
    assert finalized.terminal_delivery is None
    assert finalized.lease == staged.lease
    assert await outbox.activity() == OutboxActivity()
    plan_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"DEBATE#{staged.state.debate_id}",
                "SK": f"ATTEMPT#{staged.state.attempt_id}#DELIVERY#final-proposals",
            }
        ),
        ConsistentRead=True,
    )
    assert unmarshal_item(plan_response["Item"])["status"] == "delivered"


@pytest.mark.asyncio
async def test_vote_delivery_stages_and_finalizes_only_the_complete_ballot(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(offset=40),
        operation_id="vote-accept",
        lease_owner="worker-1",
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            starter_message_id="501",
            thread_id="502",
            control_panel_message_id="503",
        ),
    )
    preparing = await settle_unavailable_affection(
        debates,
        bound,
        scoring_at=NOW + timedelta(seconds=41),
    )
    collecting_initial = await debates.replace(
        expected=preparing,
        updated=replace(
            preparing,
            state=preparing.state.transition_to(
                DebatePhase.COLLECTING_INITIAL_OPINIONS,
                at=NOW + timedelta(seconds=42),
            ),
            initial_opinions=tuple(
                InitialOpinion(participant, "summary", "proposal")
                for participant in ParticipantSlot
            ),
        ),
    )
    discussing = await debates.replace(
        expected=collecting_initial,
        updated=replace(
            collecting_initial,
            state=collecting_initial.state.transition_to(
                DebatePhase.DISCUSSING,
                at=NOW + timedelta(seconds=43),
            ),
        ),
    )
    collecting_final = await debates.replace(
        expected=discussing,
        updated=replace(
            discussing,
            state=discussing.state.transition_to(
                DebatePhase.COLLECTING_FINAL_PROPOSALS,
                at=NOW + timedelta(seconds=44),
            ),
            final_proposals=tuple(
                FinalProposal(participant, "title", "proposal") for participant in ParticipantSlot
            ),
        ),
    )
    selecting = await debates.replace(
        expected=collecting_final,
        updated=replace(
            collecting_final,
            state=collecting_final.state.transition_to(
                DebatePhase.SELECTING_WINNER,
                at=NOW + timedelta(seconds=45),
            ),
            votes=(
                Vote(
                    ParticipantSlot.PARTICIPANT_A,
                    ParticipantSlot.PARTICIPANT_B,
                    3,
                    4,
                    5,
                    "reason-a",
                ),
                Vote(
                    ParticipantSlot.PARTICIPANT_B,
                    ParticipantSlot.PARTICIPANT_C,
                    3,
                    4,
                    5,
                    "reason-b",
                ),
                Vote(
                    ParticipantSlot.PARTICIPANT_C,
                    ParticipantSlot.PARTICIPANT_A,
                    3,
                    4,
                    5,
                    "reason-c",
                ),
            ),
        ),
    )
    staged_at = NOW + timedelta(seconds=46)
    operations = prepare_vote_outbox_operations(
        snapshot=selecting,
        participant_display_names={
            ParticipantSlot.PARTICIPANT_A: "Generic A",
            ParticipantSlot.PARTICIPANT_B: "Generic B",
            ParticipantSlot.PARTICIPANT_C: "Generic C",
        },
        created_at=staged_at,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (200, 208, 216)
    plan = delivery_plan(
        operations,
        plan_id="votes",
        source_phase=DebatePhase.SELECTING_WINNER,
        target_phase=DebatePhase.GENERATING_DECISION,
        staged_at=staged_at,
    )
    staged = await debates.stage_terminal_delivery(
        expected=selecting,
        staged=replace(
            selecting,
            state=replace(selecting.state, updated_at=staged_at),
            terminal_delivery=plan,
        ),
        operations=operations,
    )
    for index, operation in enumerate(operations):
        claimed = await outbox.claim(
            expected=staged,
            operation_id=operation.operation_id,
            claim_owner="vote-publisher",
            at=staged_at + timedelta(seconds=index + 1),
        )
        assert claimed is not None
        await outbox.mark_sent(
            expected=staged,
            operation=claimed,
            message_id=str(600 + index),
            at=staged_at + timedelta(seconds=index + 1, microseconds=1),
        )
    finalized_at = NOW + timedelta(seconds=51)
    finalized = await debates.finalize_phase_delivery(
        expected=staged,
        updated=replace(
            staged,
            state=staged.state.transition_to(
                DebatePhase.GENERATING_DECISION,
                at=finalized_at,
            ),
            terminal_delivery=None,
        ),
    )
    assert finalized.state.phase is DebatePhase.GENERATING_DECISION
    assert finalized.terminal_delivery is None
    assert finalized.lease == staged.lease
    assert await outbox.activity() == OutboxActivity()
    plan_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"DEBATE#{staged.state.debate_id}",
                "SK": f"ATTEMPT#{staged.state.attempt_id}#DELIVERY#votes",
            }
        ),
        ConsistentRead=True,
    )
    assert unmarshal_item(plan_response["Item"])["status"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "winner",
        "persisted_bot_slot_override",
        "previous_terminal_shape",
        "legacy_terminal_plan",
        "finalization_succeeds",
    ),
    (
        *((winner, None, False, False, True) for winner in ParticipantSlot),
        (ParticipantSlot.PARTICIPANT_A, DiscordBotSlot.MODERATOR, True, False, True),
        (ParticipantSlot.PARTICIPANT_A, DiscordBotSlot.MODERATOR, True, True, True),
        (ParticipantSlot.PARTICIPANT_A, DiscordBotSlot.PARTICIPANT_B, False, False, False),
    ),
    ids=(
        "new-winner-a",
        "new-winner-b",
        "new-winner-c",
        "pre-upgrade-phase-plan-moderator",
        "legacy-terminal-plan-moderator",
        "non-winner-participant-rejected",
    ),
)
async def test_terminal_finalize_atomically_completes_origin_ingress_status(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    winner: ParticipantSlot,
    persisted_bot_slot_override: DiscordBotSlot | None,
    previous_terminal_shape: bool,
    legacy_terminal_plan: bool,
    finalization_succeeds: bool,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    claimed, fence = await claimed_ingress(
        repository=ingress,
        operation_id="terminal-origin",
    )
    source = replace(
        new_snapshot(),
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
    )
    accepted = await debates.create(
        source,
        operation_id="terminal-origin",
        lease_owner="runtime-instance",
        ingress_claim=fence,
    )
    accepted_request = await ingress.mark_accepted(
        request=claimed,
        claim_owner="runtime-instance",
        at=NOW + timedelta(microseconds=3),
        debate_id=accepted.state.debate_id,
        attempt_id=accepted.state.attempt_id,
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            state=replace(
                accepted.state,
                phase=DebatePhase.GENERATING_DECISION,
                updated_at=NOW + timedelta(milliseconds=500),
            ),
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
            final_decision=FinalDecision(
                winner=winner,
                decision="fixture decision",
                actions=("fixture action",),
                caveats=("fixture caveat",),
            ),
            votes=votes_for_winner(winner),
        ),
    )

    terminal_delivery = finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=bound,
        target_phase=DebatePhase.COMPLETED,
        staged_at=NOW + timedelta(seconds=1),
        terminal_at=NOW + timedelta(seconds=2),
        persisted_bot_slot_override=persisted_bot_slot_override,
        previous_terminal_shape=previous_terminal_shape,
        legacy_terminal_plan=legacy_terminal_plan,
        dynamodb_client=dynamodb_client,
        dynamodb_table=dynamodb_table,
    )
    if not finalization_succeeds:
        with pytest.raises(RepositoryConflict, match="invalid Bot owner"):
            await terminal_delivery
        return
    completed = await terminal_delivery

    assert completed.state.phase is DebatePhase.COMPLETED
    replay = await ingress.get_replay(accepted_request)
    assert replay is not None
    assert replay.request.status is IngressStatus.COMPLETED
    assert replay.request.status_message_state is StatusMessageState.COMPLETED
    assert replay.request.accepted_debate_id == completed.state.debate_id
    assert replay.request.accepted_attempt_id == completed.state.attempt_id
    publication = await ingress.get_status_publication(accepted_request.interaction_id)
    assert publication is not None
    assert publication.desired_state is StatusMessageState.COMPLETED
    assert publication.state is StatusPublicationState.PREPARED


@pytest.mark.asyncio
async def test_accept_replay_three_slots_and_terminal_release(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted: list[DebateSnapshot] = []
    for index in range(3):
        source = new_snapshot(offset=index)
        persisted = await repository.create(
            source,
            operation_id=f"accept-{index}",
            lease_owner=f"worker-{index}",
        )
        accepted.append(persisted)
        loaded = await repository.get(source.state.debate_id)
        assert loaded == persisted
        assert loaded is not None
        assert loaded.requester_username == "pitekusu"
        assert loaded.requester_display_name == "ぬし"
        assert loaded.requester_username != loaded.requester_id

    replay = await repository.create(
        new_snapshot(offset=20),
        operation_id="accept-0",
        lease_owner="another-worker",
    )
    assert replay == accepted[0]
    assert replay.requester_username == "pitekusu"
    assert replay.requester_display_name == "ぬし"

    with pytest.raises(RepositoryBusy):
        await repository.create(
            new_snapshot(offset=30),
            operation_id="accept-over-capacity",
            lease_owner="worker-4",
        )

    first = accepted[0]
    cancelled = replace(
        first,
        state=first.state.transition_to(
            DebatePhase.CANCELLED,
            at=first.state.updated_at + timedelta(seconds=1),
        ),
    )
    persisted_cancel = await repository.replace(
        expected=first,
        updated=cancelled,
        operation_id="cancel-0",
    )
    assert persisted_cancel.state.phase is DebatePhase.CANCELLED
    assert persisted_cancel.lease is None
    assert persisted_cancel.requester_username == first.requester_username
    assert persisted_cancel.requester_display_name == first.requester_display_name

    replacement = await repository.create(
        new_snapshot(offset=40),
        operation_id="accept-after-release",
        lease_owner="worker-4",
    )
    assert replacement.lease is not None
    assert first.lease is not None
    assert replacement.lease.slot == first.lease.slot


@pytest.mark.asyncio
async def test_accept_transaction_requires_the_exact_live_ingress_claim(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    claimed, fence = await claimed_ingress(
        repository=ingress,
        operation_id="fenced-accept",
    )
    source = new_snapshot(offset=1)
    metadata_at = NOW + timedelta(milliseconds=500)
    metadata_timestamp = metadata_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS",
                "SK": ingress_request_sort_key(claimed),
            }
        ),
        UpdateExpression="SET updated_at=:at",
        ExpressionAttributeValues=marshal_item({":at": metadata_timestamp}),
    )
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": f"INGRESS_OPERATION#{claimed.interaction_id}", "SK": "RESULT"}),
        UpdateExpression="SET updated_at=:at",
        ExpressionAttributeValues=marshal_item({":at": metadata_timestamp}),
    )

    persisted = await debates.create(
        source,
        operation_id="fenced-accept",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )

    assert persisted.state.debate_id == source.state.debate_id
    assert await debates.get_operation_result("fenced-accept") == persisted
    stale_claim, stale_fence = await claimed_ingress(
        repository=ingress,
        operation_id="stale-fenced-accept",
        created_at=NOW + timedelta(seconds=10),
    )
    current_claim = await ingress.claim(
        request=stale_claim,
        claim_owner="replacement-runtime",
        at=stale_claim.claim_expires_at or NOW,
    )
    assert current_claim is not None
    stale_source = new_snapshot(offset=11)
    with pytest.raises(RepositoryClaimLost, match="no longer current"):
        await debates.create(
            stale_source,
            operation_id="stale-fenced-accept",
            lease_owner="runtime-instance",
            ingress_claim=stale_fence.for_write_at(stale_source.state.updated_at),
        )
    assert await debates.get(stale_source.state.debate_id) is None


@pytest.mark.asyncio
async def test_terminal_deadline_wins_before_domain_start_without_orphan_records(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = IngressRequest.new_debate(
        interaction_id="interaction-terminal-wins",
        operation_id="terminal-wins",
        application_id="application-id",
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
        command_name="shittim",
        created_at=NOW,
    )
    await ingress.enqueue(request)
    claimed = await ingress.claim(
        request=request,
        claim_owner="runtime-instance",
        at=request.terminal_deadline_at - timedelta(seconds=30),
    )
    assert claimed is not None
    failed = await ingress.mark_terminal_deadline(
        request=claimed,
        at=request.terminal_deadline_at,
        error_code="startup_terminal_deadline_exceeded",
    )
    source = new_snapshot(offset=1)
    fence = IngressClaimFence.from_claimed_request(
        claimed,
        claim_owner="runtime-instance",
        write_at=request.terminal_deadline_at,
    )

    with pytest.raises(RepositoryClaimLost, match="no longer current"):
        await debates.create(
            source,
            operation_id=request.operation_id,
            lease_owner="runtime-instance",
            ingress_claim=fence,
        )

    assert failed.status is IngressStatus.FAILED
    assert await debates.get(source.state.debate_id) is None
    assert await debates.get_operation_result(request.operation_id) is None
    assert await ingress.active_count() == 0


@pytest.mark.asyncio
async def test_domain_start_wins_before_terminal_deadline_and_settles_after_it(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = IngressRequest.new_debate(
        interaction_id="interaction-domain-wins",
        operation_id="domain-wins",
        application_id="application-id",
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
        command_name="shittim",
        created_at=NOW,
    )
    await ingress.enqueue(request)
    claimed = await ingress.claim(
        request=request,
        claim_owner="runtime-instance",
        at=request.terminal_deadline_at - timedelta(seconds=30),
    )
    assert claimed is not None
    write_at = request.terminal_deadline_at - timedelta(microseconds=1)
    fence = IngressClaimFence.from_claimed_request(
        claimed,
        claim_owner="runtime-instance",
        write_at=write_at,
    )
    source = new_snapshot(offset=1)

    persisted = await debates.create(
        source,
        operation_id=request.operation_id,
        lease_owner="runtime-instance",
        ingress_claim=fence,
    )
    with pytest.raises(RepositoryConflict, match="cannot overtake started processing"):
        await ingress.mark_terminal_deadline(
            request=claimed,
            at=request.terminal_deadline_at,
            error_code="startup_terminal_deadline_exceeded",
        )
    accepted = await ingress.mark_accepted(
        request=claimed,
        claim_owner="runtime-instance",
        at=request.terminal_deadline_at + timedelta(seconds=1),
        debate_id=persisted.state.debate_id,
        attempt_id=persisted.state.attempt_id,
    )

    assert accepted.status is IngressStatus.ACCEPTED
    assert accepted.processing_started_at == write_at
    assert await ingress.active_count() == 0


@pytest.mark.asyncio
async def test_predeadline_domain_start_can_be_reclaimed_after_terminal_deadline(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = IngressRequest.new_debate(
        interaction_id="interaction-postdeadline-replay",
        operation_id="postdeadline-replay",
        application_id="application-id",
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
        command_name="shittim",
        created_at=NOW,
    )
    await ingress.enqueue(request)
    claimed = await ingress.claim(
        request=request,
        claim_owner="runtime-old",
        at=request.terminal_deadline_at - timedelta(seconds=30),
    )
    assert claimed is not None
    write_at = request.terminal_deadline_at - timedelta(microseconds=1)
    source = new_snapshot(offset=1)
    persisted = await debates.create(
        source,
        operation_id=request.operation_id,
        lease_owner="runtime-old",
        ingress_claim=IngressClaimFence.from_claimed_request(
            claimed,
            claim_owner="runtime-old",
            write_at=write_at,
        ),
    )
    assert claimed.claim_expires_at is not None
    reclaim_at = claimed.claim_expires_at + timedelta(seconds=1)
    ready = await ingress.list_ready(at=reclaim_at)
    assert len(ready) == 1
    assert ready[0].processing_started_at == write_at
    reclaimed = await ingress.claim(
        request=ready[0],
        claim_owner="runtime-new",
        at=reclaim_at,
    )
    assert reclaimed is not None
    replay = await debates.create(
        source,
        operation_id=request.operation_id,
        lease_owner="runtime-new",
        ingress_claim=IngressClaimFence.from_claimed_request(
            reclaimed,
            claim_owner="runtime-new",
            write_at=reclaim_at,
        ),
    )
    accepted = await ingress.mark_accepted(
        request=reclaimed,
        claim_owner="runtime-new",
        at=reclaim_at + timedelta(seconds=1),
        debate_id=replay.state.debate_id,
        attempt_id=replay.state.attempt_id,
    )

    assert replay.state.debate_id == persisted.state.debate_id
    assert accepted.status is IngressStatus.ACCEPTED
    assert accepted.processing_started_at == write_at


@pytest.mark.asyncio
async def test_malformed_processing_marker_cannot_bypass_terminal_deadline(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = IngressRequest.new_debate(
        interaction_id="interaction-malformed-start-marker",
        operation_id="malformed-start-marker",
        application_id="application-id",
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
        command_name="shittim",
        created_at=NOW,
    )
    await ingress.enqueue(request)
    claimed = await ingress.claim(
        request=request,
        claim_owner="runtime-instance",
        at=request.terminal_deadline_at - timedelta(seconds=30),
    )
    assert claimed is not None
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS",
                "SK": ingress_request_sort_key(claimed),
            }
        ),
        UpdateExpression="SET processing_started_at=:malformed",
        ExpressionAttributeValues=marshal_item({":malformed": "2026-07-17T02:00:01.000000"}),
    )
    source = new_snapshot(offset=1)
    fence = IngressClaimFence.from_claimed_request(
        claimed,
        claim_owner="runtime-instance",
        write_at=request.terminal_deadline_at + timedelta(seconds=1),
    )

    with pytest.raises(RepositoryClaimLost, match="request is invalid"):
        await debates.create(
            source,
            operation_id=request.operation_id,
            lease_owner="runtime-instance",
            ingress_claim=fence,
        )

    assert await debates.get(source.state.debate_id) is None


@pytest.mark.asyncio
async def test_unbound_ingress_replay_reclaims_lease_outside_normal_recovery(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    _, fence = await claimed_ingress(
        repository=ingress,
        operation_id="reclaim-unbound",
    )
    source = new_snapshot(offset=1)
    accepted = await debates.create(
        source,
        operation_id="reclaim-unbound",
        lease_owner="old-runtime",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )
    assert accepted.lease is not None
    reclaim_at = NOW + timedelta(seconds=62)

    assert (
        await debates.claim_recoverable(
            lease_owner="normal-recovery",
            at=reclaim_at,
        )
        == ()
    )
    reclaimed = await debates.reclaim_for_ingress(
        expected=accepted,
        lease_owner="new-runtime",
        at=reclaim_at,
        ingress_claim=fence.for_write_at(reclaim_at),
    )

    assert reclaimed.state.debate_id == accepted.state.debate_id
    assert reclaimed.state.attempt_id == accepted.state.attempt_id
    assert reclaimed.lease is not None
    assert reclaimed.lease.owner_id == "new-runtime"
    assert reclaimed.lease.fencing_token == accepted.lease.fencing_token + 1


@pytest.mark.asyncio
async def test_same_owner_ingress_replay_renews_lease_before_setup_safety_window(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    _, fence = await claimed_ingress(repository=ingress, operation_id="renew-before-setup")
    source = new_snapshot(offset=1)
    accepted = await debates.create(
        source,
        operation_id="renew-before-setup",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )
    assert accepted.lease is not None
    replay_at = NOW + timedelta(seconds=12)

    renewed = await debates.reclaim_for_ingress(
        expected=accepted,
        lease_owner="runtime-instance",
        at=replay_at,
        ingress_claim=fence.for_write_at(replay_at),
    )

    assert renewed.lease is not None
    assert renewed.lease.slot == accepted.lease.slot
    assert renewed.lease.fencing_token == accepted.lease.fencing_token
    assert renewed.lease.expires_at == replay_at + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_origin_ingress_attempt_is_not_recoverable_until_exact_acceptance_mapping(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    claimed, fence = await claimed_ingress(repository=ingress, operation_id="recovery-gate")
    source = replace(
        new_snapshot(offset=1),
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
    )
    accepted = await debates.create(
        source,
        operation_id="recovery-gate",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )
    recovery_at = NOW + timedelta(seconds=62)

    assert await debates.claim_recoverable(lease_owner="recovery", at=recovery_at) == ()

    await ingress.mark_accepted(
        request=claimed,
        claim_owner="runtime-instance",
        at=recovery_at,
        debate_id=accepted.state.debate_id,
        attempt_id=accepted.state.attempt_id,
    )
    recovered = await debates.claim_recoverable(
        lease_owner="recovery",
        at=recovery_at + timedelta(seconds=1),
    )

    assert len(recovered) == 1
    assert recovered[0].state.debate_id == accepted.state.debate_id
    assert recovered[0].state.attempt_id == accepted.state.attempt_id


@pytest.mark.asyncio
async def test_terminal_transition_winning_after_recovery_read_blocks_stale_lease_claim(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    source = replace(
        new_snapshot(),
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
    )
    accepted = await debates.create(
        source,
        operation_id="terminal-before-recovery-claim",
        lease_owner="old-runtime",
    )
    assert accepted.lease is not None
    recovery_at = NOW + timedelta(seconds=61)
    terminal_at = NOW + timedelta(seconds=59)
    stale_snapshot_loaded = Event()
    allow_claim_transaction = Event()
    original_claim_one = debates._claim_one

    def claim_after_terminal_transition(
        snapshot: DebateSnapshot,
        lease_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        stale_snapshot_loaded.set()
        if not allow_claim_transaction.wait(timeout=5):
            raise AssertionError("terminal transition did not release the recovery claim")
        return original_claim_one(snapshot, lease_owner, at)

    monkeypatch.setattr(debates, "_claim_one", claim_after_terminal_transition)
    claim_task = asyncio.create_task(
        debates.claim_recoverable(lease_owner="replacement-runtime", at=recovery_at)
    )
    try:
        assert await asyncio.to_thread(stale_snapshot_loaded.wait, 5)
        persisted_terminal = await finalize_terminal_delivery(
            debates=debates,
            outbox=outbox,
            expected=accepted,
            target_phase=DebatePhase.CANCELLED,
            staged_at=terminal_at - timedelta(seconds=1),
            terminal_at=terminal_at,
        )
    finally:
        allow_claim_transaction.set()

    assert await claim_task == ()
    current = await debates.get(accepted.state.debate_id)
    assert current == persisted_terminal
    assert current is not None
    assert current.state.phase is DebatePhase.CANCELLED
    assert current.lease is None
    slot_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#GLOBAL",
                "SK": f"SLOT#{accepted.lease.slot}",
            }
        ),
        ConsistentRead=True,
    )
    slot = unmarshal_item(slot_response["Item"])
    assert "lease_owner" not in slot
    assert "lease_expiry" not in slot


@pytest.mark.asyncio
async def test_pre_activation_failure_releases_slot_and_replays_idempotently(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    _, fence = await claimed_ingress(repository=ingress, operation_id="failed-before-context")
    source = new_snapshot(offset=1)
    accepted = await debates.create(
        source,
        operation_id="failed-before-context",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )
    assert accepted.lease is not None
    failed_at = NOW + timedelta(seconds=2)
    failed = replace(
        accepted,
        state=accepted.state.transition_to(DebatePhase.FAILED, at=failed_at),
        error_code="discord_context_invalid",
    )

    persisted = await debates.fail_pre_activation(
        expected=accepted,
        updated=failed,
        ingress_claim=fence.for_write_at(failed_at),
    )
    replay = await debates.fail_pre_activation(
        expected=accepted,
        updated=failed,
        ingress_claim=fence.for_write_at(failed_at),
    )

    assert persisted == replay
    assert persisted.state.phase is DebatePhase.FAILED
    assert persisted.error_code == "discord_context_invalid"
    assert persisted.lease is None
    quota_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "QUOTA#GUILD#guild",
                "SK": "DAY#2026-07-17",
            }
        ),
        ConsistentRead=True,
    )
    assert unmarshal_item(quota_response["Item"])["count"] == 1
    replacement = await debates.create(
        new_snapshot(offset=3),
        operation_id="replacement-after-compensation",
        lease_owner="replacement-runtime",
    )
    assert replacement.lease is not None
    assert replacement.lease.slot == accepted.lease.slot


@pytest.mark.asyncio
async def test_retry_pre_activation_failure_preserves_source_attempt_and_quota(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        replace(
            new_snapshot(),
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        ),
        operation_id="retry-source",
        lease_owner="initial-runtime",
    )
    persisted_source = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=accepted,
        target_phase=DebatePhase.FAILED,
        staged_at=NOW + timedelta(milliseconds=500),
        terminal_at=NOW + timedelta(seconds=1),
        error_code="source_failure",
    )
    assert await debates.pending_panel_refresh_count() == 1
    source_panel_claim = await debates.claim_panel_refresh(
        debate_id=persisted_source.state.debate_id,
        attempt_id=persisted_source.state.attempt_id,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=1),
    )
    assert source_panel_claim is not None
    persisted_source = await debates.complete_panel_refresh(
        expected=source_panel_claim,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=2),
    )
    assert await debates.pending_panel_refresh_count() == 0
    _, fence = await claimed_ingress(
        repository=ingress,
        operation_id="retry-before-activation",
        kind=IngressKind.RETRY,
        target=persisted_source,
        created_at=NOW + timedelta(seconds=2),
    )
    retry_state = persisted_source.state.new_retry_attempt(
        AttemptId.new(),
        at=NOW + timedelta(seconds=3),
    )
    retry = replace(
        persisted_source,
        state=retry_state,
        attempt_created_at=retry_state.updated_at,
        lease=None,
        error_code=None,
        terminal_delivery=None,
    )
    persisted_retry = await debates.create_retry(
        expected_failed=persisted_source,
        retry=retry,
        operation_id="retry-before-activation",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(retry_state.updated_at),
    )
    assert persisted_retry.panel_refresh_pending is True
    assert await debates.pending_panel_refresh_count() == 1
    assert (
        await debates.claim_panel_refresh(
            debate_id=persisted_retry.state.debate_id,
            attempt_id=persisted_retry.state.attempt_id,
            claim_owner="panel-publisher",
            at=NOW + timedelta(seconds=3, microseconds=1),
        )
        is None
    )
    failed_at = NOW + timedelta(seconds=4)
    failed_retry = replace(
        persisted_retry,
        state=persisted_retry.state.transition_to(DebatePhase.FAILED, at=failed_at),
        error_code="discord_context_invalid",
    )

    compensated = await debates.fail_pre_activation(
        expected=persisted_retry,
        updated=failed_retry,
        ingress_claim=fence.for_write_at(failed_at),
    )
    compensated_panel = await debates.claim_panel_refresh(
        debate_id=compensated.state.debate_id,
        attempt_id=compensated.state.attempt_id,
        claim_owner="panel-publisher",
        at=failed_at + timedelta(microseconds=1),
    )
    assert compensated_panel is not None
    assert await debates.pending_panel_refresh_count() == 1

    current = await debates.get(compensated.state.debate_id)
    source_replay = await debates.get_operation_result("retry-source")
    quota_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "QUOTA#GUILD#guild",
                "SK": "DAY#2026-07-17",
            }
        ),
        ConsistentRead=True,
    )
    assert current == compensated_panel
    assert compensated.state.phase is DebatePhase.FAILED
    assert compensated.state.retry_of == persisted_source.state.attempt_id
    assert compensated.lease is None
    assert source_replay == persisted_source
    assert unmarshal_item(quota_response["Item"])["count"] == 1


@pytest.mark.asyncio
async def test_phase_write_cannot_restore_a_stale_panel_refresh_claim(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        replace(
            new_snapshot(),
            starter_message_id="201",
            thread_id="202",
            control_panel_message_id="203",
        ),
        operation_id="panel-race-source",
        lease_owner="runtime-instance",
    )
    failed = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=accepted,
        target_phase=DebatePhase.FAILED,
        staged_at=NOW + timedelta(milliseconds=500),
        terminal_at=NOW + timedelta(seconds=1),
        error_code="fixture_failure",
    )
    failed_claim = await debates.claim_panel_refresh(
        debate_id=failed.state.debate_id,
        attempt_id=failed.state.attempt_id,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=1),
    )
    assert failed_claim is not None
    failed = await debates.complete_panel_refresh(
        expected=failed_claim,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=2),
    )
    retry_state = failed.state.new_retry_attempt(
        AttemptId.new(),
        at=NOW + timedelta(seconds=2),
    )
    retry = await debates.create_retry(
        expected_failed=failed,
        retry=replace(
            failed,
            state=retry_state,
            attempt_created_at=retry_state.updated_at,
            lease=None,
            error_code=None,
            terminal_delivery=None,
        ),
        operation_id="panel-race-retry",
        lease_owner="runtime-instance",
    )
    stale_phase_write = replace(
        retry,
        state=retry.state.transition_to(
            DebatePhase.SCORING_AFFECTION,
            at=NOW + timedelta(seconds=3),
        ),
    )
    panel_claim = await debates.claim_panel_refresh(
        debate_id=retry.state.debate_id,
        attempt_id=retry.state.attempt_id,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=2, microseconds=1),
    )
    assert panel_claim is not None

    with pytest.raises(RepositoryConflict):
        await debates.replace(expected=retry, updated=stale_phase_write)

    current = await debates.get(retry.state.debate_id)
    assert current == panel_claim
    stale_claimed_phase_write = replace(
        panel_claim,
        state=panel_claim.state.transition_to(
            DebatePhase.SCORING_AFFECTION,
            at=NOW + timedelta(seconds=3),
        ),
    )
    completed = await debates.complete_panel_refresh(
        expected=panel_claim,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=2, microseconds=2),
    )
    assert await debates.pending_panel_refresh_count() == 0
    with pytest.raises(RepositoryConflict):
        await debates.replace(
            expected=panel_claim,
            updated=stale_claimed_phase_write,
        )
    advanced = await debates.replace(
        expected=completed,
        updated=replace(
            completed,
            state=completed.state.transition_to(
                DebatePhase.SCORING_AFFECTION,
                at=NOW + timedelta(seconds=3),
            ),
        ),
    )
    assert advanced.state.phase is DebatePhase.SCORING_AFFECTION
    assert advanced.panel_refresh_pending is False


@pytest.mark.asyncio
async def test_claimed_panel_refresh_abandonment_is_atomic_and_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        replace(
            new_snapshot(),
            starter_message_id="301",
            thread_id="302",
            control_panel_message_id="303",
        ),
        operation_id="panel-abandon-source",
        lease_owner="runtime-instance",
    )
    required_at = NOW + timedelta(seconds=1)
    failed = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=accepted,
        target_phase=DebatePhase.FAILED,
        staged_at=NOW + timedelta(milliseconds=500),
        terminal_at=required_at,
        error_code="fixture_failure",
    )
    assert failed.panel_refresh_state is PanelRefreshState.PENDING
    assert await debates.pending_panel_refresh_count() == 1
    assert await debates.abandoned_panel_refresh_count() == 0

    claimed = await debates.claim_panel_refresh(
        debate_id=failed.state.debate_id,
        attempt_id=failed.state.attempt_id,
        claim_owner="panel-publisher",
        at=required_at + timedelta(microseconds=1),
    )
    assert claimed is not None
    abandoned_at = required_at + timedelta(microseconds=2)
    abandoned = await debates.abandon_panel_refresh(
        expected=claimed,
        claim_owner="panel-publisher",
        at=abandoned_at,
        error_code="discord_permission_denied",
    )

    assert abandoned.panel_refresh_state is PanelRefreshState.ABANDONED
    assert abandoned.panel_refresh_failed_at == abandoned_at
    assert abandoned.panel_refresh_error_code == "discord_permission_denied"
    assert abandoned.panel_refresh_claim_owner is None
    assert abandoned.panel_refresh_claim_expires_at is None
    assert abandoned.panel_refresh_next_attempt_at is None
    assert await debates.pending_panel_refresh_count() == 0
    assert await debates.abandoned_panel_refresh_count() == 1
    assert await debates.get(abandoned.state.debate_id) == abandoned

    response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"DEBATE#{abandoned.state.debate_id}",
                "SK": f"ATTEMPT#{abandoned.state.attempt_id}#META",
            }
        ),
        ConsistentRead=True,
    )
    attempt_item = unmarshal_item(response["Item"])
    assert attempt_item["panel_refresh_failed_at"] == abandoned_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert attempt_item["panel_refresh_error_code"] == "discord_permission_denied"
    assert "panel_refresh_claim_owner" not in attempt_item
    assert "panel_refresh_claim_expiry" not in attempt_item
    assert "panel_refresh_next_attempt_at" not in attempt_item
    assert "gsi2pk" not in attempt_item
    assert "gsi2sk" not in attempt_item

    with pytest.raises(RepositoryConflict):
        await debates.complete_panel_refresh(
            expected=claimed,
            claim_owner="panel-publisher",
            at=abandoned_at + timedelta(microseconds=1),
        )
    with pytest.raises(RepositoryConflict):
        await debates.abandon_panel_refresh(
            expected=claimed,
            claim_owner="panel-publisher",
            at=abandoned_at + timedelta(microseconds=1),
            error_code="discord_permission_denied",
        )
    assert await debates.pending_panel_refresh_count() == 0
    assert await debates.abandoned_panel_refresh_count() == 1


@pytest.mark.asyncio
async def test_expired_pre_activation_compensation_never_clears_replacement_slot(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    _, fence = await claimed_ingress(repository=ingress, operation_id="expired-compensation")
    source = new_snapshot(offset=1)
    accepted = await debates.create(
        source,
        operation_id="expired-compensation",
        lease_owner="runtime-instance",
        ingress_claim=fence.for_write_at(source.state.updated_at),
    )
    assert accepted.lease is not None
    replacement = await debates.create(
        new_snapshot(offset=62),
        operation_id="replacement-owner",
        lease_owner="replacement-runtime",
    )
    assert replacement.lease is not None
    assert replacement.lease.slot == accepted.lease.slot
    assert replacement.lease.fencing_token == accepted.lease.fencing_token + 1
    failed_at = NOW + timedelta(seconds=63)
    failed = replace(
        accepted,
        state=accepted.state.transition_to(DebatePhase.FAILED, at=failed_at),
        error_code="discord_context_invalid",
    )

    persisted = await debates.fail_pre_activation(
        expected=accepted,
        updated=failed,
        ingress_claim=fence.for_write_at(failed_at),
    )
    renewed_replacement = await debates.renew_lease(
        expected=replacement,
        at=NOW + timedelta(seconds=64),
    )

    assert persisted.lease is None
    assert renewed_replacement.fencing_token == replacement.lease.fencing_token


@pytest.mark.asyncio
async def test_current_claim_reports_slot_busy_but_stale_claim_reports_claim_lost(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    for index in range(3):
        await debates.create(
            new_snapshot(offset=index),
            operation_id=f"fill-slot-{index}",
            lease_owner=f"worker-{index}",
        )
    claimed, fence = await claimed_ingress(
        repository=ingress,
        operation_id="busy-fenced-accept",
        created_at=NOW + timedelta(seconds=10),
    )
    source = new_snapshot(offset=11)

    with pytest.raises(RepositoryBusy):
        await debates.create(
            source,
            operation_id="busy-fenced-accept",
            lease_owner="runtime-instance",
            ingress_claim=fence.for_write_at(source.state.updated_at),
        )

    await ingress.reschedule(
        request=claimed,
        claim_owner="runtime-instance",
        at=NOW + timedelta(seconds=12),
        next_attempt_at=NOW + timedelta(seconds=20),
        error_code="slot_busy",
    )
    with pytest.raises(RepositoryClaimLost, match="no longer current"):
        await debates.create(
            source,
            operation_id="busy-fenced-accept",
            lease_owner="runtime-instance",
            ingress_claim=fence.for_write_at(source.state.updated_at),
        )


@pytest.mark.asyncio
async def test_retry_and_cancel_mutations_share_the_ingress_claim_transaction(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        replace(
            new_snapshot(),
            starter_message_id="401",
            thread_id="402",
            control_panel_message_id="403",
        ),
        operation_id="initial-accept",
        lease_owner="worker-1",
    )
    persisted_failed = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=accepted,
        target_phase=DebatePhase.FAILED,
        staged_at=NOW + timedelta(milliseconds=500),
        terminal_at=NOW + timedelta(seconds=1),
        error_code="test_failure",
    )
    failed_panel_claim = await debates.claim_panel_refresh(
        debate_id=persisted_failed.state.debate_id,
        attempt_id=persisted_failed.state.attempt_id,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=1),
    )
    assert failed_panel_claim is not None
    persisted_failed = await debates.complete_panel_refresh(
        expected=failed_panel_claim,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=2),
    )
    retry_claimed, retry_fence = await claimed_ingress(
        repository=ingress,
        operation_id="fenced-retry",
        kind=IngressKind.RETRY,
        target=persisted_failed,
        created_at=NOW + timedelta(seconds=2),
    )
    retry_state = persisted_failed.state.new_retry_attempt(
        AttemptId.new(),
        at=NOW + timedelta(seconds=3),
    )
    retry = replace(
        persisted_failed,
        state=retry_state,
        attempt_created_at=retry_state.updated_at,
        lease=None,
        error_code=None,
        terminal_delivery=None,
    )
    persisted_retry = await debates.create_retry(
        expected_failed=persisted_failed,
        retry=retry,
        operation_id="fenced-retry",
        lease_owner="runtime-instance",
        ingress_claim=retry_fence.for_write_at(retry_state.updated_at),
    )
    await ingress.mark_accepted(
        request=retry_claimed,
        claim_owner="runtime-instance",
        at=NOW + timedelta(seconds=3, microseconds=1),
        debate_id=persisted_retry.state.debate_id,
        attempt_id=persisted_retry.state.attempt_id,
    )
    _, cancel_fence = await claimed_ingress(
        repository=ingress,
        operation_id="fenced-cancel",
        kind=IngressKind.CANCEL,
        target=persisted_retry,
        created_at=NOW + timedelta(seconds=4),
    )
    persisted_cancel = await finalize_terminal_delivery(
        debates=debates,
        outbox=outbox,
        expected=persisted_retry,
        target_phase=DebatePhase.CANCELLED,
        staged_at=NOW + timedelta(seconds=5),
        terminal_at=NOW + timedelta(seconds=6),
        operation_id="fenced-cancel",
        ingress_claim=cancel_fence.for_write_at(NOW + timedelta(seconds=5)),
    )

    assert persisted_cancel.state.phase is DebatePhase.CANCELLED
    assert persisted_cancel.lease is None


@pytest.mark.asyncio
async def test_daily_quota_condition_fails_closed(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(
            {
                "PK": "QUOTA#GUILD#guild",
                "SK": "DAY#2026-07-17",
                "record_type": "guild_daily_quota",
                "schema_version": 2,
                "count": 30,
            }
        ),
    )
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)

    with pytest.raises(RepositoryQuotaExceeded):
        await repository.create(
            new_snapshot(),
            operation_id="quota-exhausted",
            lease_owner="worker",
        )


@pytest.mark.asyncio
async def test_affection_settlement_is_atomic_idempotent_and_reapplies_after_profile_cas(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    first = await repository.create(
        new_snapshot(),
        operation_id="affection-first",
        lease_owner="worker-1",
    )
    first = await begin_affection_scoring(repository, first, at=NOW + timedelta(microseconds=1))

    settled, replay = await asyncio.gather(
        repository.settle_affection(
            expected=first,
            scores=(10, -20, 100),
            at=NOW + timedelta(microseconds=2),
        ),
        repository.settle_affection(
            expected=first,
            scores=(10, -20, 100),
            at=NOW + timedelta(microseconds=2),
        ),
    )

    assert settled == replay
    assert settled.state.phase is DebatePhase.PREPARING_EVIDENCE
    assert settled.affection_assessment is not None
    assert tuple(item.after for item in settled.affection_assessment.participants) == (
        510,
        480,
        600,
    )
    profile_key = {
        "PK": affection_profile_partition(settled.requester_id),
        "SK": "PROFILE",
    }
    raw_profile = unmarshal_item(
        dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert raw_profile["scores"] == [510, 480, 600]
    assert raw_profile["version"] == 1

    second = await repository.create(
        new_snapshot(offset=10),
        operation_id="affection-second",
        lease_owner="worker-2",
    )
    second = await begin_affection_scoring(repository, second, at=NOW + timedelta(seconds=11))
    third = await repository.create(
        new_snapshot(offset=20),
        operation_id="affection-third",
        lease_owner="worker-3",
    )
    third = await begin_affection_scoring(repository, third, at=NOW + timedelta(seconds=21))
    second, third = await asyncio.gather(
        repository.settle_affection(
            expected=second,
            scores=(20, 20, -100),
            at=NOW + timedelta(seconds=30),
        ),
        repository.settle_affection(
            expected=third,
            scores=(30, -10, 50),
            at=NOW + timedelta(seconds=31),
        ),
    )
    assert second.affection_assessment is not None
    assert third.affection_assessment is not None
    assert tuple(item.question_score for item in second.affection_assessment.participants) == (
        20,
        20,
        -100,
    )
    assert tuple(item.question_score for item in third.affection_assessment.participants) == (
        30,
        -10,
        50,
    )
    raw_profile = unmarshal_item(
        dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert raw_profile["scores"] == [560, 490, 550]
    assert raw_profile["version"] == 3


@pytest.mark.asyncio
async def test_first_unavailable_affection_assessment_does_not_create_a_profile(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(new_snapshot(), requester_id="unavailable-only"),
        operation_id="affection-unavailable-first",
        lease_owner="worker-1",
    )
    scoring = await begin_affection_scoring(
        repository, accepted, at=accepted.created_at + timedelta(seconds=1)
    )
    settled = await repository.settle_affection(
        expected=scoring,
        scores=None,
        at=accepted.created_at + timedelta(seconds=2),
    )

    assert settled.affection_assessment is not None
    assert settled.affection_assessment.status.value == "unavailable"
    profile_key = {
        "PK": affection_profile_partition(settled.requester_id),
        "SK": "PROFILE",
    }
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(profile_key),
        ConsistentRead=True,
    )


@pytest.mark.asyncio
async def test_successful_affection_atomically_migrates_the_v8_raw_profile(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    requester_id = "legacy-private-requester"
    legacy_key = {"PK": f"AFFECTION#REQUESTER#{requester_id}", "SK": "PROFILE"}
    legacy_item: DynamoItem = {
        **legacy_key,
        "record_type": "affection_profile",
        "schema_version": PREVIOUS_SCHEMA_VERSION,
        "requester_id": requester_id,
        "requester_username": "legacy",
        "requester_display_name": "Legacy",
        "scores": [1000, 990, 500],
        "version": 4,
        "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(legacy_item))
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(
            new_snapshot(),
            requester_id=requester_id,
            requester_username="current",
            requester_display_name="Current",
        ),
        operation_id="affection-v8-migration",
        lease_owner="worker-1",
    )
    scoring = await begin_affection_scoring(
        repository, accepted, at=NOW + timedelta(microseconds=1)
    )

    settled = await repository.settle_affection(
        expected=scoring,
        scores=(-100, 10, 0),
        at=NOW + timedelta(microseconds=2),
    )

    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(legacy_key),
        ConsistentRead=True,
    )
    new_key = {
        "PK": affection_profile_partition(requester_id),
        "SK": "PROFILE",
    }
    migrated = unmarshal_item(
        dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item(new_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert migrated["scores"] == [900, 1000, 500]
    assert migrated["requester_key"] == derive_affection_requester_key(
        IDENTITY_HMAC_KEY,
        requester_id,
    )
    assert "requester_id" not in migrated
    assert migrated["reset_count"] == 0
    assert migrated["memorial_cycle"] == 1
    assert migrated["unlocked_participant"] == ParticipantSlot.PARTICIPANT_A.value
    assert migrated["unlock_debate_id"] == str(settled.state.debate_id)
    assert migrated["unlock_display_name"] == "Current"
    assert migrated["unlock_retroactive"] is True
    assert settled.affection_assessment is not None
    assert settled.affection_assessment.memorial_unlock is not None
    assert settled.affection_assessment.memorial_unlock.retroactive is True


@pytest.mark.asyncio
async def test_new_affection_profile_retries_when_a_v8_profile_appears_after_the_read(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    requester_id = "legacy-race-requester"
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(new_snapshot(), requester_id=requester_id),
        operation_id="affection-v8-race",
        lease_owner="worker-1",
    )
    scoring = await begin_affection_scoring(
        repository, accepted, at=NOW + timedelta(microseconds=1)
    )
    legacy_key = {"PK": f"AFFECTION#REQUESTER#{requester_id}", "SK": "PROFILE"}
    legacy_item: DynamoItem = {
        **legacy_key,
        "record_type": "affection_profile",
        "schema_version": PREVIOUS_SCHEMA_VERSION,
        "requester_id": requester_id,
        "requester_username": "legacy",
        "requester_display_name": "Legacy",
        "scores": [600, 400, 500],
        "version": 7,
        "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    repository.insert_legacy_profile_before_next_transaction(legacy_item)

    settled = await repository.settle_affection(
        expected=scoring,
        scores=(10, -20, 30),
        at=NOW + timedelta(microseconds=2),
    )

    assert settled.affection_assessment is not None
    assert tuple(item.after for item in settled.affection_assessment.participants) == (
        610,
        380,
        530,
    )
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(legacy_key),
        ConsistentRead=True,
    )
    migrated = unmarshal_item(
        dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item(
                {
                    "PK": affection_profile_partition(requester_id),
                    "SK": "PROFILE",
                }
            ),
            ConsistentRead=True,
        )["Item"]
    )
    assert migrated["scores"] == [610, 380, 530]
    assert migrated["version"] == 8
    assert "requester_id" not in migrated


@pytest.mark.asyncio
async def test_unavailable_affection_does_not_migrate_the_v8_raw_profile(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    requester_id = "legacy-unavailable-requester"
    legacy_key = {"PK": f"AFFECTION#REQUESTER#{requester_id}", "SK": "PROFILE"}
    legacy_item: DynamoItem = {
        **legacy_key,
        "record_type": "affection_profile",
        "schema_version": PREVIOUS_SCHEMA_VERSION,
        "requester_id": requester_id,
        "requester_username": "legacy",
        "requester_display_name": "Legacy",
        "scores": [500, 500, 500],
        "version": 2,
        "updated_at": NOW.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(legacy_item))
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(new_snapshot(), requester_id=requester_id),
        operation_id="affection-v8-unavailable",
        lease_owner="worker-1",
    )

    await settle_unavailable_affection(
        repository,
        accepted,
        scoring_at=NOW + timedelta(microseconds=1),
    )

    retained = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(legacy_key),
        ConsistentRead=True,
    )["Item"]
    assert unmarshal_item(retained) == legacy_item
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": affection_profile_partition(requester_id), "SK": "PROFILE"}),
        ConsistentRead=True,
    )


@pytest.mark.asyncio
async def test_unavailable_affection_assessment_does_not_rewrite_an_existing_profile(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    applied = await repository.create(
        new_snapshot(),
        operation_id="affection-profile-source",
        lease_owner="worker-1",
    )
    applied = await begin_affection_scoring(repository, applied, at=NOW + timedelta(microseconds=1))
    applied = await repository.settle_affection(
        expected=applied,
        scores=(10, -20, 100),
        at=NOW + timedelta(microseconds=2),
    )
    profile_key = {
        "PK": affection_profile_partition(applied.requester_id),
        "SK": "PROFILE",
    }
    profile_before = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(profile_key),
        ConsistentRead=True,
    )["Item"]

    snapshots: list[DebateSnapshot] = []
    for index in range(2):
        accepted = await repository.create(
            new_snapshot(offset=(index + 1) * 10),
            operation_id=f"affection-unavailable-existing-{index}",
            lease_owner=f"worker-{index + 2}",
        )
        snapshots.append(
            await settle_unavailable_affection(
                repository,
                accepted,
                scoring_at=accepted.created_at + timedelta(seconds=1),
            )
        )
    assert all(
        snapshot.affection_assessment is not None
        and snapshot.affection_assessment.status.value == "unavailable"
        for snapshot in snapshots
    )
    profile_after = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(profile_key),
        ConsistentRead=True,
    )["Item"]
    assert profile_after == profile_before
    raw_profile = unmarshal_item(profile_after)
    assert raw_profile["scores"] == [510, 480, 600]
    assert raw_profile["version"] == 1


@pytest.mark.asyncio
async def test_failed_attempt_retry_is_atomic_and_does_not_consume_quota(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(
            new_snapshot(),
            channel_id="500",
            starter_message_id="501",
            thread_id="502",
            control_panel_message_id="503",
        ),
        operation_id="accept",
        lease_owner="worker-1",
    )
    accepted = await begin_affection_scoring(
        repository, accepted, at=NOW + timedelta(microseconds=100)
    )
    accepted = await repository.settle_affection(
        expected=accepted,
        scores=(10, -20, 100),
        at=NOW + timedelta(microseconds=101),
    )
    persisted_failed = await finalize_terminal_delivery(
        debates=repository,
        outbox=outbox,
        expected=accepted,
        target_phase=DebatePhase.FAILED,
        staged_at=NOW + timedelta(milliseconds=500),
        terminal_at=NOW + timedelta(seconds=1),
        error_code="test_failure",
    )
    affection_post = await outbox.get(
        debate_id=persisted_failed.state.debate_id,
        attempt_id=persisted_failed.state.attempt_id,
        operation_id="terminal-failed-affection-0000",
    )
    assert affection_post is not None
    assert affection_post.status is OutboxStatus.SENT
    assert affection_post.record_schema_version == 3
    assert affection_post.delivery_target is DiscordDeliveryTarget.CHANNEL
    assert affection_post.channel_id == "500"
    failed_panel_claim = await repository.claim_panel_refresh(
        debate_id=persisted_failed.state.debate_id,
        attempt_id=persisted_failed.state.attempt_id,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=1),
    )
    assert failed_panel_claim is not None
    persisted_failed = await repository.complete_panel_refresh(
        expected=failed_panel_claim,
        claim_owner="panel-publisher",
        at=NOW + timedelta(seconds=1, microseconds=2),
    )
    retry_state = persisted_failed.state.new_retry_attempt(
        AttemptId.new(),
        at=persisted_failed.state.updated_at + timedelta(seconds=1),
    )
    retry = replace(
        persisted_failed,
        state=retry_state,
        attempt_created_at=retry_state.updated_at,
        lease=None,
        error_code=None,
        terminal_delivery=None,
    )

    persisted_retry = await repository.create_retry(
        expected_failed=persisted_failed,
        retry=retry,
        operation_id="retry",
        lease_owner="worker-2",
    )

    assert persisted_retry.state.retry_of == persisted_failed.state.attempt_id
    assert persisted_retry.lease is not None
    assert persisted_retry.requester_username == accepted.requester_username
    assert persisted_retry.requester_display_name == accepted.requester_display_name
    assert persisted_retry.affection_assessment == accepted.affection_assessment
    profile_key = {
        "PK": affection_profile_partition(accepted.requester_id),
        "SK": "PROFILE",
    }
    profile = unmarshal_item(
        dynamodb_client.get_item(
            TableName=dynamodb_table,
            Key=marshal_item(profile_key),
            ConsistentRead=True,
        )["Item"]
    )
    assert profile["scores"] == [510, 480, 600]
    assert profile["version"] == 1
    assert await repository.get_operation_result("retry") == persisted_retry
    assert (
        await repository.create_retry(
            expected_failed=persisted_failed,
            retry=replace(retry, state=replace(retry.state, attempt_id=AttemptId.new())),
            operation_id="retry",
            lease_owner="worker-3",
        )
        == persisted_retry
    )


@pytest.mark.asyncio
async def test_recoverable_gsi_claim_and_lease_renewal_are_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        replace(
            new_snapshot(),
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        ),
        operation_id="accept",
        lease_owner="old-worker",
    )
    assert accepted.lease is not None
    expired = (NOW - timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    for key in (
        {
            "PK": f"DEBATE#{accepted.state.debate_id}",
            "SK": f"ATTEMPT#{accepted.state.attempt_id}#META",
        },
        {"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{accepted.lease.slot}"},
    ):
        dynamodb_client.update_item(
            TableName=dynamodb_table,
            Key=marshal_item(key),
            UpdateExpression="SET lease_expiry=:expired",
            ExpressionAttributeValues=marshal_item({":expired": expired}),
        )

    claimed = await repository.claim_recoverable(lease_owner="new-worker", at=NOW)
    assert len(claimed) == 1
    assert claimed[0].lease is not None
    assert claimed[0].lease.owner_id == "new-worker"
    assert claimed[0].lease.fencing_token == accepted.lease.fencing_token + 1
    stale_update = replace(
        accepted,
        state=accepted.state.transition_to(
            DebatePhase.SCORING_AFFECTION,
            at=NOW + timedelta(seconds=1),
        ),
    )
    with pytest.raises(RepositoryConflict):
        await repository.replace(expected=accepted, updated=stale_update)

    renewed = await repository.renew_lease(
        expected=claimed[0],
        at=NOW + timedelta(seconds=20),
    )
    assert renewed.expires_at == NOW + timedelta(seconds=80)
    current = await repository.get(accepted.state.debate_id)
    assert current is not None
    assert current.lease == renewed
    advanced = replace(
        current,
        state=current.state.transition_to(
            DebatePhase.SCORING_AFFECTION,
            at=NOW + timedelta(seconds=21),
        ),
    )
    await repository.replace(expected=current, updated=advanced)
    reloaded = await repository.get(accepted.state.debate_id)
    assert reloaded is not None
    assert reloaded.lease == renewed


@pytest.mark.asyncio
async def test_outbox_enforces_chunk_order_claim_retry_and_idempotent_completion(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debate_repository = DynamoDbDebateRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    outbox_repository = DynamoDbOutboxRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    snapshot = await debate_repository.create(
        new_snapshot(),
        operation_id="accept",
        lease_owner="worker-1",
    )

    def operation(sequence: int) -> OutboxOperation:
        content = f"chunk-{sequence}"
        return OutboxOperation(
            operation_id=f"post-{sequence}",
            debate_id=snapshot.state.debate_id,
            attempt_id=snapshot.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="102",
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            nonce="A" * 22 if sequence == 0 else "B" * 22,
            chunk_sequence=sequence,
            status=OutboxStatus.PREPARED,
            created_at=NOW,
        )

    first = await outbox_repository.prepare(expected=snapshot, operation=operation(0))
    second = await outbox_repository.prepare(expected=snapshot, operation=operation(1))
    assert (
        await outbox_repository.claim(
            expected=snapshot,
            operation_id=second.operation_id,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=1),
        )
        is None
    )

    claimed = await outbox_repository.claim(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.status is OutboxStatus.CLAIMED
    rescheduled = await outbox_repository.reschedule(
        expected=snapshot,
        operation=claimed,
        at=NOW + timedelta(seconds=2),
        next_retry_at=NOW + timedelta(seconds=5),
    )
    assert rescheduled.status is OutboxStatus.PREPARED
    assert await outbox_repository.list_pending(
        debate_id=snapshot.state.debate_id,
        attempt_id=snapshot.state.attempt_id,
    ) == (rescheduled, second)
    assert (
        await outbox_repository.claim(
            expected=snapshot,
            operation_id=first.operation_id,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=4),
        )
        is None
    )

    reclaimed = await outbox_repository.claim(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=5),
    )
    assert reclaimed is not None
    with pytest.raises(RepositoryConflict, match="claim changed"):
        await outbox_repository.mark_sent(
            expected=snapshot,
            operation=claimed,
            message_id="104",
            at=NOW + timedelta(seconds=6),
        )
    with pytest.raises(RepositoryConflict, match="claim changed"):
        await outbox_repository.reschedule(
            expected=snapshot,
            operation=claimed,
            at=NOW + timedelta(seconds=6),
            next_retry_at=NOW + timedelta(seconds=8),
        )
    sent = await outbox_repository.mark_sent(
        expected=snapshot,
        operation=reclaimed,
        message_id="104",
        at=NOW + timedelta(seconds=6),
    )
    assert sent.status is OutboxStatus.SENT
    assert (
        await outbox_repository.mark_sent(
            expected=snapshot,
            operation=reclaimed,
            message_id="104",
            at=NOW + timedelta(seconds=7),
        )
        == sent
    )

    second_claimed = await outbox_repository.claim(
        expected=snapshot,
        operation_id=second.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=7),
    )
    assert second_claimed is not None
    assert await outbox_repository.list_pending(
        debate_id=snapshot.state.debate_id,
        attempt_id=snapshot.state.attempt_id,
    ) == (second_claimed,)


@pytest.mark.asyncio
async def test_outbox_idempotency_token_binds_the_complete_transaction_request(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    """A later application retry with a new timestamp must not reuse an old payload token."""

    outbox = DynamoDbOutboxRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    first = outbox_activity_action(
        table_name=dynamodb_table,
        pending_delta=1,
        claimed_delta=0,
        at=NOW,
    )
    second = outbox_activity_action(
        table_name=dynamodb_table,
        pending_delta=1,
        claimed_delta=0,
        at=NOW + timedelta(seconds=1),
    )

    await asyncio.to_thread(outbox._transact, [first], "same-logical-outbox-retry")
    await asyncio.to_thread(outbox._transact, [second], "same-logical-outbox-retry")

    assert await outbox.activity() == OutboxActivity(pending=2, claimed=0)


@pytest.mark.asyncio
async def test_terminal_delivery_requires_sent_outbox_before_atomic_release(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(),
        operation_id="terminal-accept",
        lease_owner="worker-1",
    )
    bound = replace(
        accepted,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
    )
    bound = await debates.replace(expected=accepted, updated=bound)
    staged_at = NOW + timedelta(seconds=1)
    with pytest.raises(RepositoryConflict):
        await debates.replace(
            expected=bound,
            updated=replace(
                bound,
                state=bound.state.transition_to(DebatePhase.CANCELLED, at=staged_at),
            ),
            operation_id="bound-direct-cancel",
        )
    operations = prepare_terminal_outbox_operations(
        snapshot=bound,
        target_phase=DebatePhase.CANCELLED,
        created_at=staged_at,
    )
    plan = delivery_plan(
        operations,
        plan_id=operations[0].plan_id or "terminal-cancelled",
        source_phase=bound.state.phase,
        target_phase=DebatePhase.CANCELLED,
        staged_at=staged_at,
    )
    staged = replace(
        bound,
        state=replace(bound.state, updated_at=staged_at),
        terminal_delivery=plan,
    )

    persisted = await debates.stage_terminal_delivery(
        expected=bound,
        staged=staged,
        operations=operations,
        operation_id="terminal-cancel",
    )
    assert persisted == staged
    assert persisted.state.phase is DebatePhase.ACCEPTED
    assert (await outbox.activity()).pending == len(operations)

    terminal_at = NOW + timedelta(seconds=4)
    terminal = replace(
        persisted,
        state=persisted.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        terminal_delivery=plan.complete(at=terminal_at),
    )
    with pytest.raises(RepositoryTransactionConflict) as caught:
        await debates.finalize_terminal(expected=persisted, updated=terminal)
    assert caught.value.failures == tuple(
        (
            RepositoryTransactionAction.OUTBOX_SENT_CHECK,
            RepositoryCancellationCode.CONDITIONAL_CHECK_FAILED,
        )
        for _ in operations
    )
    assert caught.value.reasons_complete
    assert not caught.value.retryable

    for index, operation in enumerate(operations):
        claim_at = NOW + timedelta(seconds=2, microseconds=index)
        claimed = await outbox.claim(
            expected=persisted,
            operation_id=operation.operation_id,
            claim_owner="publisher",
            at=claim_at,
        )
        assert claimed is not None
        await outbox.mark_sent(
            expected=persisted,
            operation=claimed,
            message_id=str(104 + index),
            at=claim_at + timedelta(microseconds=1),
        )

    finalized = await debates.finalize_terminal(expected=persisted, updated=terminal)
    assert finalized.state.phase is DebatePhase.CANCELLED
    assert finalized.terminal_delivery_complete
    assert finalized.lease is None
    assert finalized.panel_refresh_pending
    assert await outbox.activity() == OutboxActivity()

    counter_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"}),
        ConsistentRead=True,
    )
    counter_item = unmarshal_item(counter_response["Item"])
    assert counter_item["count"] == 0


@pytest.mark.asyncio
async def test_checkpoint_collection_cas_rejects_same_timestamp_stale_writer(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(),
        operation_id="generation-cas",
        lease_owner="worker-1",
    )
    phase_at = NOW + timedelta(seconds=1)
    generating = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            state=replace(
                accepted.state,
                phase=DebatePhase.GENERATING_DECISION,
                updated_at=phase_at,
            ),
        ),
    )
    participant_a = replace(
        generating,
        generation_checkpoints=(
            GenerationCheckpoint.planned(
                phase=DebatePhase.GENERATING_DECISION,
                participant=ParticipantSlot.PARTICIPANT_A,
                at=phase_at,
            ),
        ),
    )
    persisted = await debates.replace(expected=generating, updated=participant_a)
    stale_participant_b = replace(
        generating,
        generation_checkpoints=(
            GenerationCheckpoint.planned(
                phase=DebatePhase.GENERATING_DECISION,
                participant=ParticipantSlot.PARTICIPANT_B,
                at=phase_at,
            ),
        ),
    )

    with pytest.raises(RepositoryConflict):
        await debates.replace(expected=generating, updated=stale_participant_b)

    assert await debates.get(generating.state.debate_id) == persisted


@pytest.mark.asyncio
async def test_phase_outbox_requires_atomic_stage_and_complete_predecessors(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(),
        operation_id="phase-order",
        lease_owner="worker-1",
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        ),
    )
    staged_at = NOW + timedelta(seconds=1)
    deadline_at = staged_at + timedelta(minutes=15)
    operations = tuple(
        OutboxOperation(
            operation_id=f"terminal-cancelled-{sequence:04d}",
            debate_id=bound.state.debate_id,
            attempt_id=bound.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="102",
            content=f"chunk-{sequence}",
            content_hash=hashlib.sha256(f"chunk-{sequence}".encode()).hexdigest(),
            nonce=("A" if sequence == 0 else "B") * 22,
            chunk_sequence=sequence,
            status=OutboxStatus.PREPARED,
            created_at=staged_at,
            record_schema_version=2,
            phase=DebatePhase.CANCELLED,
            plan_id="terminal-cancelled",
            delivery_sequence=910 + sequence,
            deadline_at=deadline_at,
        )
        for sequence in range(2)
    )
    with pytest.raises(RepositoryConflict, match="staged atomically"):
        await outbox.prepare(expected=bound, operation=operations[0])

    plan = delivery_plan(
        operations,
        plan_id="terminal-cancelled",
        source_phase=bound.state.phase,
        target_phase=DebatePhase.CANCELLED,
        staged_at=staged_at,
        deadline_at=deadline_at,
    )
    staged = replace(
        bound,
        state=replace(bound.state, updated_at=staged_at),
        terminal_delivery=plan,
    )
    persisted = await debates.stage_terminal_delivery(
        expected=bound,
        staged=staged,
        operations=operations,
        operation_id="phase-cancel",
    )
    assert (
        await outbox.claim(
            expected=persisted,
            operation_id=operations[1].operation_id,
            claim_owner="publisher",
            at=staged_at + timedelta(seconds=1),
        )
        is None
    )
    first_claim = await outbox.claim(
        expected=persisted,
        operation_id=operations[0].operation_id,
        claim_owner="publisher",
        at=staged_at + timedelta(seconds=1),
    )
    assert first_claim is not None
    prepared_again = await outbox.reschedule(
        expected=persisted,
        operation=first_claim,
        at=staged_at + timedelta(seconds=2),
        next_retry_at=staged_at + timedelta(seconds=3),
    )
    second_claim = await outbox.claim(
        expected=persisted,
        operation_id=prepared_again.operation_id,
        claim_owner="publisher",
        at=staged_at + timedelta(seconds=3),
    )
    assert second_claim is not None
    with pytest.raises(RepositoryConflict, match="claim changed"):
        await outbox.mark_sent(
            expected=persisted,
            operation=first_claim,
            message_id="104",
            at=staged_at + timedelta(seconds=4),
        )
    await outbox.mark_sent(
        expected=persisted,
        operation=second_claim,
        message_id="104",
        at=staged_at + timedelta(seconds=4),
    )
    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"DEBATE#{bound.state.debate_id}",
                "SK": (f"ATTEMPT#{bound.state.attempt_id}#OUTBOX#{operations[0].operation_id}"),
            }
        ),
    )

    with pytest.raises(RepositoryConflict, match="outbox is incomplete"):
        await outbox.claim(
            expected=persisted,
            operation_id=operations[1].operation_id,
            claim_owner="publisher",
            at=staged_at + timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_phase_abandonment_atomically_clears_mixed_outbox_activity(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debates = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    outbox = DynamoDbOutboxRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await debates.create(
        new_snapshot(),
        operation_id="phase-abandon",
        lease_owner="worker-1",
    )
    bound = await debates.replace(
        expected=accepted,
        updated=replace(
            accepted,
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        ),
    )
    staged_at = NOW + timedelta(seconds=1)
    operations = tuple(
        OutboxOperation(
            operation_id=f"terminal-cancelled-{sequence:04d}",
            debate_id=bound.state.debate_id,
            attempt_id=bound.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="102",
            content=f"chunk-{sequence}",
            content_hash=hashlib.sha256(f"chunk-{sequence}".encode()).hexdigest(),
            nonce=chr(ord("A") + sequence) * 22,
            chunk_sequence=sequence,
            status=OutboxStatus.PREPARED,
            created_at=staged_at,
            record_schema_version=2,
            phase=DebatePhase.CANCELLED,
            plan_id="terminal-cancelled",
            delivery_sequence=910 + sequence,
            deadline_at=staged_at + timedelta(minutes=15),
        )
        for sequence in range(3)
    )
    plan = delivery_plan(
        operations,
        plan_id="terminal-cancelled",
        source_phase=bound.state.phase,
        target_phase=DebatePhase.CANCELLED,
        staged_at=staged_at,
    )
    staged = await debates.stage_terminal_delivery(
        expected=bound,
        staged=replace(
            bound,
            state=replace(bound.state, updated_at=staged_at),
            terminal_delivery=plan,
        ),
        operations=operations,
    )
    first = await outbox.claim(
        expected=staged,
        operation_id=operations[0].operation_id,
        claim_owner="publisher",
        at=staged_at + timedelta(seconds=1),
    )
    assert first is not None
    await outbox.mark_sent(
        expected=staged,
        operation=first,
        message_id="104",
        at=staged_at + timedelta(seconds=2),
    )
    second = await outbox.claim(
        expected=staged,
        operation_id=operations[1].operation_id,
        claim_owner="publisher",
        at=staged_at + timedelta(seconds=3),
    )
    assert second is not None

    terminating = await debates.terminate_terminal_delivery(
        expected=staged,
        at=staged_at + timedelta(seconds=4),
        reason=DeliveryAbandonReason.CANCELLED,
    )
    abandoned = await debates.abandon_terminal_delivery(
        expected=terminating,
        at=staged_at + timedelta(seconds=5),
        reason=DeliveryAbandonReason.CANCELLED,
    )

    activity_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"}),
        ConsistentRead=True,
    )
    activity_item = activity_response.get("Item")
    assert activity_item is not None
    activity = unmarshal_item(activity_item)
    assert activity["pending_count"] == 0
    assert activity["claimed_count"] == 0
    assert (
        await outbox.list_pending(
            debate_id=bound.state.debate_id,
            attempt_id=bound.state.attempt_id,
        )
        == ()
    )
    settled_operations = []
    for operation in operations:
        settled_operations.append(
            await outbox.get(
                debate_id=bound.state.debate_id,
                attempt_id=bound.state.attempt_id,
                operation_id=operation.operation_id,
            )
        )
    assert settled_operations[0] is not None
    assert settled_operations[0].status is OutboxStatus.SENT
    assert all(
        operation is not None and operation.status is OutboxStatus.ABANDONED
        for operation in settled_operations[1:]
    )

    terminal_at = staged_at + timedelta(seconds=6)
    terminal = await debates.finalize_terminal(
        expected=abandoned,
        updated=replace(
            abandoned,
            state=abandoned.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        ),
    )
    active_response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"}),
        ConsistentRead=True,
    )
    active = unmarshal_item(active_response["Item"])
    assert terminal.state.phase is DebatePhase.CANCELLED
    assert active["count"] == 0
