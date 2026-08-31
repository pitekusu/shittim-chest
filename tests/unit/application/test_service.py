"""Tests for the SDK-independent debate application service."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from shittim_chest.application import (
    AcceptDebateRequest,
    AcceptedDebate,
    BindDiscordContextCommand,
    CancelDebateCommand,
    DebateApplication,
    DebateNotFound,
    DiscordBotSlot,
    DiscordDeliveryTarget,
    GenerationProviderError,
    IngressClaimFence,
    IngressKind,
    InvalidApplicationOperation,
    OutboxRecoveryAbandoned,
    OutboxRecoveryFailed,
    RequestNotAllowed,
    RetryDebateCommand,
    RuntimeNotReady,
)
from shittim_chest.application.models import (
    DebateSnapshot,
    GenerationCheckpoint,
    GenerationStatus,
    LeaseGrant,
    MetricEvent,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
)
from shittim_chest.application.ports import (
    RepositoryCancellationCode,
    RepositoryConflict,
    RepositoryTransactionAction,
    RepositoryTransactionConflict,
    RepositoryTransactionStage,
)
from shittim_chest.domain import (
    PARTICIPANTS,
    DebatePhase,
    DebateState,
    EvidenceBundle,
    EvidenceSearchStatus,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
    SearchRequirement,
    Vote,
)
from tests.unit.application.fakes import (
    FakeCandidateOrderer,
    FakeClock,
    FakeDiscord,
    FakeEvidence,
    FakeIds,
    FakeMetrics,
    FakeOpenAI,
    FakeOutboxRecovery,
    FakeRepository,
)


@pytest.fixture
def dependencies() -> tuple[
    FakeClock,
    FakeIds,
    FakeMetrics,
    FakeDiscord,
    FakeEvidence,
    FakeOpenAI,
    FakeRepository,
    FakeCandidateOrderer,
]:
    return (
        FakeClock(),
        FakeIds(),
        FakeMetrics(),
        FakeDiscord(),
        FakeEvidence(),
        FakeOpenAI(),
        FakeRepository(),
        FakeCandidateOrderer(),
    )


def make_application(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    *,
    session_timeout: float = 300.0,
    phase_timeout: float = 60.0,
    lease_renewal: float = 20.0,
    outbox_recovery: FakeOutboxRecovery | None = None,
    participant_display_names: dict[ParticipantSlot, str] | None = None,
    lease_owner: str = "worker-1",
    terminal_delivery_conflict_retry_seconds: float = 0.0,
) -> DebateApplication:
    clock, ids, metrics, discord, evidence, openai, repository, orderer = dependencies
    return DebateApplication(
        clock=clock,
        ids=ids,
        metrics=metrics,
        discord=discord,
        evidence=evidence,
        openai=openai,
        repository=repository,
        candidate_orderer=orderer,
        outbox_recovery=outbox_recovery or FakeOutboxRecovery(),
        participant_display_names=(
            {
                ParticipantSlot.PARTICIPANT_A: "Generic A",
                ParticipantSlot.PARTICIPANT_B: "Generic B",
                ParticipantSlot.PARTICIPANT_C: "Generic C",
            }
            if participant_display_names is None
            else participant_display_names
        ),
        lease_owner=lease_owner,
        session_timeout_seconds=session_timeout,
        phase_timeout_seconds=phase_timeout,
        lease_renewal_seconds=lease_renewal,
        terminal_delivery_conflict_retry_seconds=terminal_delivery_conflict_retry_seconds,
    )


def test_application_rejects_renderer_incompatible_participant_display_name(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    with pytest.raises(ValueError, match="forbidden Unicode"):
        make_application(
            dependencies,
            participant_display_names={
                ParticipantSlot.PARTICIPANT_A: "Generic\u200dA",
                ParticipantSlot.PARTICIPANT_B: "Generic B",
                ParticipantSlot.PARTICIPANT_C: "Generic C",
            },
        )


def request(*, requester_id: str = "requester") -> AcceptDebateRequest:
    return AcceptDebateRequest(
        question="What should we eat?",
        requester_id=requester_id,
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        operation_id="accept-operation",
    )


async def accept_bound_debate(
    application: DebateApplication,
    debate_request: AcceptDebateRequest | None = None,
) -> AcceptedDebate:
    """Create the production-valid Discord context required for terminal Outbox delivery."""

    accepted = await application.accept_debate(debate_request or request())
    await application.bind_discord_context(
        BindDiscordContextCommand(
            debate_id=accepted.debate_id,
            starter_message_id="101",
            thread_id="102",
            control_panel_message_id="103",
        )
    )
    return accepted


def ingress_claim(
    *,
    operation_id: str,
    at: datetime,
    kind: IngressKind = IngressKind.NEW_DEBATE,
    claim_owner: str = "new-worker",
) -> IngressClaimFence:
    return IngressClaimFence(
        interaction_id=f"interaction-{operation_id}",
        operation_id=operation_id,
        kind=kind,
        created_at=at - timedelta(seconds=1),
        terminal_deadline_at=at - timedelta(seconds=1) + timedelta(minutes=15),
        claim_owner=claim_owner,
        claim_expires_at=at + timedelta(minutes=2),
        delivery_attempt=2,
        write_at=at,
    )


@pytest.mark.asyncio
async def test_ingress_accept_replay_reclaims_expired_lease_without_changing_ids(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    repository = dependencies[6]
    original_app = make_application(dependencies, lease_owner="old-worker")
    accepted = await original_app.accept_debate(
        request(),
        ingress_claim=ingress_claim(
            operation_id="accept-operation",
            at=dependencies[0].current,
            claim_owner="old-worker",
        ),
    )
    original = repository.current[accepted.debate_id]
    assert original.lease is not None
    replay_at = dependencies[0].current
    stale = replace(
        original,
        lease=replace(
            original.lease,
            owner_id="old-worker",
            expires_at=replay_at - timedelta(microseconds=1),
        ),
    )
    repository.current[accepted.debate_id] = stale
    repository.operations["accept-operation"] = stale
    replay_app = make_application(dependencies, lease_owner="new-worker")

    replayed = await replay_app.accept_debate(
        request(),
        ingress_claim=ingress_claim(operation_id="accept-operation", at=replay_at),
    )

    assert replayed == accepted
    reclaimed = repository.current[accepted.debate_id]
    assert reclaimed.state.debate_id == accepted.debate_id
    assert reclaimed.state.attempt_id == accepted.attempt_id
    assert reclaimed.lease is not None
    assert reclaimed.lease.owner_id == "new-worker"


@pytest.mark.asyncio
async def test_pre_activation_failure_keeps_quota_semantics_but_releases_attempt_lease(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies, lease_owner="new-worker")
    repository = dependencies[6]
    claim = ingress_claim(
        operation_id="accept-operation",
        at=dependencies[0].current,
    )
    accepted = await app.accept_debate(request(), ingress_claim=claim)

    error_code = await app.fail_pre_activation(
        debate_id=accepted.debate_id,
        attempt_id=accepted.attempt_id,
        kind=IngressKind.NEW_DEBATE,
        ingress_claim=claim,
        error_code="discord_context_invalid",
    )

    failed = repository.current[accepted.debate_id]
    assert error_code == "discord_context_invalid"
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.ACCEPTED
    assert failed.error_code == error_code
    assert failed.lease is None
    assert failed.origin_ingress_interaction_id == claim.interaction_id
    assert (
        await app.fail_pre_activation(
            debate_id=accepted.debate_id,
            attempt_id=accepted.attempt_id,
            kind=IngressKind.NEW_DEBATE,
            ingress_claim=claim,
            error_code="different_replay_code",
        )
        == "discord_context_invalid"
    )


@pytest.mark.asyncio
async def test_bound_retry_pre_activation_failure_waits_for_required_outbox(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies, lease_owner="new-worker")
    clock, _, _, _, _, _, repository, _ = dependencies
    accepted = await accept_bound_debate(app)
    source = repository.current[accepted.debate_id]
    failed = replace(
        source,
        state=source.state.transition_to(DebatePhase.FAILED, at=clock.now()),
        error_code="source_failure",
    )
    await repository.replace(expected=source, updated=failed)
    claim = ingress_claim(
        operation_id="retry-operation",
        at=clock.current,
        kind=IngressKind.RETRY,
    )
    retry = await app.retry_debate(
        RetryDebateCommand(
            accepted.debate_id,
            "requester",
            "retry-operation",
            expected_attempt_id=failed.state.attempt_id,
        ),
        ingress_claim=claim,
    )

    error_code = await app.fail_pre_activation(
        debate_id=retry.debate_id,
        attempt_id=retry.attempt_id,
        kind=IngressKind.RETRY,
        ingress_claim=claim,
        error_code="discord_context_invalid",
    )
    staged = repository.current[retry.debate_id]
    assert error_code == "discord_context_invalid"
    assert staged.state.phase is DebatePhase.ACCEPTED
    assert staged.error_code == error_code
    assert staged.terminal_delivery is not None
    assert staged.terminal_delivery.target_phase is DebatePhase.FAILED
    assert staged.lease is not None
    assert (
        await app.fail_pre_activation(
            debate_id=retry.debate_id,
            attempt_id=retry.attempt_id,
            kind=IngressKind.RETRY,
            ingress_claim=claim,
            error_code="different_replay_code",
        )
        == error_code
    )

    await app.run_debate(retry.debate_id)

    terminal = repository.current[retry.debate_id]
    assert terminal.state.phase is DebatePhase.FAILED
    assert terminal.terminal_delivery_complete
    assert terminal.lease is None


@pytest.mark.asyncio
async def test_bind_discord_context_is_idempotent_and_rebinding_fails(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    command = BindDiscordContextCommand(
        debate_id=accepted.debate_id,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
    )

    first = await app.bind_discord_context(command)
    replay = await app.bind_discord_context(command)

    assert replay == first
    assert repository.current[accepted.debate_id].control_panel_message_id == "103"
    with pytest.raises(InvalidApplicationOperation, match="already bound"):
        await app.bind_discord_context(replace(command, control_panel_message_id="104"))


@pytest.mark.asyncio
async def test_bind_discord_context_rejects_started_debate(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    current = repository.current[accepted.debate_id]
    repository.current[accepted.debate_id] = replace(
        current,
        state=current.state.transition_to(
            DebatePhase.SCORING_AFFECTION,
            at=dependencies[0].now(),
        ),
    )

    with pytest.raises(InvalidApplicationOperation, match="before debate work"):
        await app.bind_discord_context(
            BindDiscordContextCommand(
                debate_id=accepted.debate_id,
                starter_message_id="101",
                thread_id="102",
                control_panel_message_id="103",
            )
        )


@pytest.mark.asyncio
async def test_accept_and_run_complete_debate_with_shared_evidence_and_ordering(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    _, _, metrics, _, evidence, openai, repository, orderer = dependencies
    evidence.bundle = EvidenceBundle(
        required_search_satisfied=False,
        search_requirement=SearchRequirement.OPTIONAL,
        search_status=EvidenceSearchStatus.OPTIONAL_UNAVAILABLE,
        router_rules_version="agentic-search-v1",
        routing_reason="agentic_search_unavailable",
    )

    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.final_decision is not None
    assert completed.final_decision.winner is ParticipantSlot.PARTICIPANT_B
    assert completed.final_decision.victory_message == "persona victory message"
    assert completed.escalation_assessment is not None
    assert completed.escalation_assessment.split_vote is True
    assert completed.escalation_assessment.executed is False
    assert evidence.calls == ["What should we eat?"]
    assert len(openai.evidence_calls) == 10
    assert all(bundle is evidence.bundle for bundle in openai.evidence_calls)
    assert set(openai.initial_calls) == set(ParticipantSlot)
    assert set(openai.affection_calls) == set(ParticipantSlot)
    assert set(openai.proposal_calls) == set(ParticipantSlot)
    assert len(openai.vote_calls) == 3
    assert (
        len(openai.initial_calls)
        + len(openai.proposal_calls)
        + len(openai.vote_calls)
        + len(openai.decision_calls)
        == 10
    )
    assert len(orderer.calls) == 3
    assert all(voter not in candidates for voter, candidates in orderer.calls)
    completed_operations = tuple(
        operation
        for operation in repository.terminal_operations.values()
        if operation.plan_id == "terminal-completed"
    )
    assert completed_operations
    assert completed_operations[0].bot_slot is DiscordBotSlot.MODERATOR
    assert all(
        operation.bot_slot is DiscordBotSlot.PARTICIPANT_B
        for operation in completed_operations[1:-1]
    )
    affection_operation = completed_operations[-1]
    assert affection_operation.bot_slot is DiscordBotSlot.MODERATOR
    assert affection_operation.delivery_target is DiscordDeliveryTarget.CHANNEL
    assert affection_operation.channel_id == "100"
    assert "persona victory message" in "\n".join(
        operation.content for operation in completed_operations[1:-1]
    )
    assert MetricEvent.COMPLETED in {event for event, _ in metrics.events}
    assert [item.state.phase for item in repository.history[accepted.debate_id]] == [
        DebatePhase.ACCEPTED,
        DebatePhase.ACCEPTED,
        DebatePhase.SCORING_AFFECTION,
        DebatePhase.PREPARING_EVIDENCE,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.DISCUSSING,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.GENERATING_DECISION,
        DebatePhase.GENERATING_DECISION,
        DebatePhase.GENERATING_DECISION,
        DebatePhase.GENERATING_DECISION,
        DebatePhase.COMPLETED,
    ]


@pytest.mark.asyncio
async def test_affection_scores_are_all_applied_before_persona_responses(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    openai = dependencies[5]
    repository = dependencies[6]
    openai.affection_scores = {
        ParticipantSlot.PARTICIPANT_A: 35,
        ParticipantSlot.PARTICIPANT_B: -43,
        ParticipantSlot.PARTICIPANT_C: 100,
    }
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    assert completed.affection_assessment is not None
    assert tuple(item.after for item in completed.affection_assessment.participants) == (
        535,
        457,
        600,
    )
    assert repository.affection_profiles[completed.requester_id].scores == (535, 457, 600)
    assert openai.response_affection_scores.count((ParticipantSlot.PARTICIPANT_A, 535)) == 2
    assert openai.response_affection_scores.count((ParticipantSlot.PARTICIPANT_B, 457)) == 3
    assert openai.response_affection_scores.count((ParticipantSlot.PARTICIPANT_C, 600)) == 2


@pytest.mark.asyncio
async def test_one_affection_provider_failure_discards_all_scores_and_continues(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    openai = dependencies[5]
    repository = dependencies[6]
    openai.affection_scores = {
        ParticipantSlot.PARTICIPANT_A: 100,
        ParticipantSlot.PARTICIPANT_B: 100,
        ParticipantSlot.PARTICIPANT_C: 100,
    }
    openai.affection_errors[ParticipantSlot.PARTICIPANT_B] = GenerationProviderError(
        "openai_unavailable",
        "content-free provider failure",
        retryable=True,
    )
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.affection_assessment is not None
    assert completed.affection_assessment.status.value == "unavailable"
    assert completed.requester_id not in repository.affection_profiles
    assert tuple(item.after for item in completed.affection_assessment.participants) == (
        500,
        500,
        500,
    )
    assert set(openai.affection_calls) == set(PARTICIPANTS)
    assert {score for _, score in openai.response_affection_scores} == {500}


@pytest.mark.asyncio
async def test_initial_opinions_are_persisted_then_delivered_by_each_participant_in_order(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    discord = dependencies[3]
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    initial_stage = next(
        snapshot
        for snapshot in repository.terminal_stages
        if isinstance(snapshot.terminal_delivery, PhaseDeliveryPlan)
        and snapshot.terminal_delivery.plan_id == "initial-opinions"
    )
    operations = tuple(
        operation
        for operation in repository.terminal_operations.values()
        if operation.plan_id == "initial-opinions"
    )
    assert tuple(operation.bot_slot for operation in operations) == (
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (0, 8, 16)
    assert initial_stage.state.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
    assert repository.phase_delivery_finalizations[0].state.phase is DebatePhase.DISCUSSING
    assert repository.phase_delivery_finalizations[0].terminal_delivery is None
    assert discord.delivery_checks[:3] == [
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
    ]
    persisted_counts = [
        len(snapshot.initial_opinions)
        for snapshot in repository.history[accepted.debate_id]
        if snapshot.state.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
        and snapshot.initial_opinions
    ]
    assert persisted_counts[:3] == [1, 2, 3]
    for snapshot in repository.history[accepted.debate_id]:
        for opinion in snapshot.initial_opinions:
            checkpoint = snapshot.checkpoint_for(
                phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                participant=opinion.participant,
            )
            assert checkpoint is not None
            assert checkpoint.status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_final_proposals_are_persisted_then_delivered_by_each_participant_in_order(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    discord = dependencies[3]
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    final_stage = next(
        snapshot
        for snapshot in repository.terminal_stages
        if isinstance(snapshot.terminal_delivery, PhaseDeliveryPlan)
        and snapshot.terminal_delivery.plan_id == "final-proposals"
    )
    operations = tuple(
        operation
        for operation in repository.terminal_operations.values()
        if operation.plan_id == "final-proposals"
    )
    assert tuple(operation.bot_slot for operation in operations) == (
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (100, 108, 116)
    assert final_stage.state.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    assert repository.phase_delivery_finalizations[1].state.phase is DebatePhase.SELECTING_WINNER
    assert repository.phase_delivery_finalizations[1].terminal_delivery is None
    assert discord.delivery_checks[3:6] == [
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
    ]
    persisted_counts = [
        len(snapshot.final_proposals)
        for snapshot in repository.history[accepted.debate_id]
        if snapshot.state.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
        and snapshot.final_proposals
    ]
    assert persisted_counts[:3] == [1, 2, 3]
    for snapshot in repository.history[accepted.debate_id]:
        for proposal in snapshot.final_proposals:
            checkpoint = snapshot.checkpoint_for(
                phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                participant=proposal.participant,
            )
            assert checkpoint is not None
            assert checkpoint.status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_final_proposal_preflight_failure_stops_before_every_proposal_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    discord = dependencies[3]
    openai = dependencies[5]
    repository = dependencies[6]
    discord.delivery_ready_results = [True, True, True, True, False]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "discord_delivery_preflight_failed"
    assert set(openai.initial_calls) == set(PARTICIPANTS)
    assert openai.proposal_calls == []
    checkpoints = tuple(
        checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    )
    assert len(checkpoints) == 3
    assert all(checkpoint.status is GenerationStatus.FAILED for checkpoint in checkpoints)
    assert all(checkpoint.logical_attempt == 0 for checkpoint in checkpoints)


@pytest.mark.asyncio
async def test_known_final_proposal_failure_keeps_other_completed_outputs_durable(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.proposal_errors[ParticipantSlot.PARTICIPANT_A] = GenerationProviderError(
        "openai_proposal_failure",
        "content-free provider failure",
        retryable=False,
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_proposal_failure"
    assert {proposal.participant for proposal in failed.final_proposals} == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }
    checkpoints = {
        checkpoint.participant: checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    }
    assert checkpoints[ParticipantSlot.PARTICIPANT_A].status is GenerationStatus.FAILED
    assert checkpoints[ParticipantSlot.PARTICIPANT_B].status is GenerationStatus.COMPLETED
    assert checkpoints[ParticipantSlot.PARTICIPANT_C].status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_final_proposal_recovery_uses_one_successor_call_per_participant(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = (
        DebateState.accepted(debate_id, attempt_id, at=accepted_at)
        .transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
        .transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
        .transition_to(DebatePhase.DISCUSSING, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_FINAL_PROPOSALS, at=clock.now())
    )
    old_lease = LeaseGrant(
        owner_id="worker-old",
        slot=0,
        fencing_token=10,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    checkpoints = tuple(
        GenerationCheckpoint.planned(
            phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
            participant=participant,
            at=state.updated_at,
        ).claim(lease=old_lease, at=clock.now())
        for participant in PARTICIPANTS
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        initial_opinions=tuple(
            InitialOpinion(participant, "summary", "proposal") for participant in PARTICIPANTS
        ),
        generation_checkpoints=checkpoints,
    )
    await repository.create(snapshot, operation_id="recover-proposals", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    completed = repository.current[debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    recovered = tuple(
        checkpoint
        for checkpoint in completed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    )
    assert len(recovered) == 3
    assert all(checkpoint.status is GenerationStatus.COMPLETED for checkpoint in recovered)
    assert all(checkpoint.logical_attempt == 2 for checkpoint in recovered)
    assert set(openai.proposal_calls) == set(PARTICIPANTS)


@pytest.mark.asyncio
async def test_votes_are_persisted_privately_then_delivered_by_each_participant_in_order(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    discord = dependencies[3]
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    vote_stage = next(
        snapshot
        for snapshot in repository.terminal_stages
        if isinstance(snapshot.terminal_delivery, PhaseDeliveryPlan)
        and snapshot.terminal_delivery.plan_id == "votes"
    )
    operations = tuple(
        operation
        for operation in repository.terminal_operations.values()
        if operation.plan_id == "votes"
    )
    assert len(vote_stage.votes) == 3
    assert tuple(operation.bot_slot for operation in operations) == (
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (200, 208, 216)
    assert vote_stage.state.phase is DebatePhase.SELECTING_WINNER
    assert repository.phase_delivery_finalizations[2].state.phase is DebatePhase.GENERATING_DECISION
    assert repository.phase_delivery_finalizations[2].terminal_delivery is None
    assert discord.delivery_checks[6:9] == [
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
    ]
    persisted_counts = [
        len(snapshot.votes)
        for snapshot in repository.history[accepted.debate_id]
        if snapshot.state.phase is DebatePhase.SELECTING_WINNER and snapshot.votes
    ]
    assert persisted_counts[:3] == [1, 2, 3]
    for snapshot in repository.history[accepted.debate_id]:
        for vote in snapshot.votes:
            checkpoint = snapshot.checkpoint_for(
                phase=DebatePhase.SELECTING_WINNER,
                participant=vote.voter,
            )
            assert checkpoint is not None
            assert checkpoint.status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_vote_preflight_failure_stops_before_every_vote_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    discord = dependencies[3]
    openai = dependencies[5]
    repository = dependencies[6]
    discord.delivery_ready_results = [True] * 7 + [False]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "discord_delivery_preflight_failed"
    assert set(openai.proposal_calls) == set(PARTICIPANTS)
    assert openai.vote_calls == []
    assert not any(
        operation.plan_id == "votes" for operation in repository.terminal_operations.values()
    )
    checkpoints = tuple(
        checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.SELECTING_WINNER
    )
    assert len(checkpoints) == 3
    assert all(checkpoint.status is GenerationStatus.FAILED for checkpoint in checkpoints)
    assert all(checkpoint.logical_attempt == 0 for checkpoint in checkpoints)


@pytest.mark.asyncio
async def test_known_vote_failure_keeps_other_votes_private_and_durable(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.vote_errors[ParticipantSlot.PARTICIPANT_A] = GenerationProviderError(
        "openai_vote_failure",
        "content-free provider failure",
        retryable=False,
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_vote_failure"
    assert {vote.voter for vote in failed.votes} == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }
    assert not any(
        operation.plan_id == "votes" for operation in repository.terminal_operations.values()
    )
    checkpoints = {
        checkpoint.participant: checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.SELECTING_WINNER
    }
    assert checkpoints[ParticipantSlot.PARTICIPANT_A].status is GenerationStatus.FAILED
    assert checkpoints[ParticipantSlot.PARTICIPANT_B].status is GenerationStatus.COMPLETED
    assert checkpoints[ParticipantSlot.PARTICIPANT_C].status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_vote_recovery_uses_one_successor_call_per_participant(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = (
        DebateState.accepted(debate_id, attempt_id, at=accepted_at)
        .transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
        .transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
        .transition_to(DebatePhase.DISCUSSING, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_FINAL_PROPOSALS, at=clock.now())
        .transition_to(DebatePhase.SELECTING_WINNER, at=clock.now())
    )
    old_lease = LeaseGrant(
        owner_id="worker-old",
        slot=0,
        fencing_token=10,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    checkpoints = tuple(
        GenerationCheckpoint.planned(
            phase=DebatePhase.SELECTING_WINNER,
            participant=participant,
            at=state.updated_at,
        ).claim(lease=old_lease, at=clock.now())
        for participant in PARTICIPANTS
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        initial_opinions=tuple(
            InitialOpinion(participant, "summary", "proposal") for participant in PARTICIPANTS
        ),
        final_proposals=tuple(
            FinalProposal(participant, "title", "proposal") for participant in PARTICIPANTS
        ),
        generation_checkpoints=checkpoints,
    )
    await repository.create(snapshot, operation_id="recover-votes", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    completed = repository.current[debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    recovered = tuple(
        checkpoint
        for checkpoint in completed.generation_checkpoints
        if checkpoint.phase is DebatePhase.SELECTING_WINNER
    )
    assert len(recovered) == 3
    assert all(checkpoint.status is GenerationStatus.COMPLETED for checkpoint in recovered)
    assert all(checkpoint.logical_attempt == 2 for checkpoint in recovered)
    assert {voter for voter, _ in openai.vote_calls} == set(PARTICIPANTS)


@pytest.mark.asyncio
async def test_vote_participant_mismatch_fails_without_public_vote_delivery(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.vote_voter_override[ParticipantSlot.PARTICIPANT_A] = ParticipantSlot.PARTICIPANT_B
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_participant_mismatch"
    assert not any(
        operation.plan_id == "votes" for operation in repository.terminal_operations.values()
    )


@pytest.mark.asyncio
async def test_vote_generation_exhaustion_stops_before_a_third_logical_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = (
        DebateState.accepted(debate_id, attempt_id, at=accepted_at)
        .transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
        .transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
        .transition_to(DebatePhase.DISCUSSING, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_FINAL_PROPOSALS, at=clock.now())
        .transition_to(DebatePhase.SELECTING_WINNER, at=clock.now())
    )
    first_lease = LeaseGrant(
        owner_id="worker-old-1",
        slot=0,
        fencing_token=10,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    second_lease = LeaseGrant(
        owner_id="worker-old-2",
        slot=1,
        fencing_token=11,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    checkpoints = tuple(
        GenerationCheckpoint.planned(
            phase=DebatePhase.SELECTING_WINNER,
            participant=participant,
            at=state.updated_at,
        )
        .claim(lease=first_lease, at=clock.now())
        .claim(lease=second_lease, at=clock.now())
        for participant in PARTICIPANTS
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        initial_opinions=tuple(
            InitialOpinion(participant, "summary", "proposal") for participant in PARTICIPANTS
        ),
        final_proposals=tuple(
            FinalProposal(participant, "title", "proposal") for participant in PARTICIPANTS
        ),
        generation_checkpoints=checkpoints,
    )
    await repository.create(snapshot, operation_id="exhaust-votes", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    failed = repository.current[debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "generation_attempts_exhausted"
    assert openai.vote_calls == []
    exhausted = tuple(
        checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.SELECTING_WINNER
    )
    assert all(checkpoint.status is GenerationStatus.FAILED for checkpoint in exhausted)
    assert all(checkpoint.logical_attempt == 2 for checkpoint in exhausted)


@pytest.mark.asyncio
async def test_complete_legacy_ballot_is_delivered_without_regeneration(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = (
        DebateState.accepted(debate_id, attempt_id, at=accepted_at)
        .transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
        .transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
        .transition_to(DebatePhase.DISCUSSING, at=clock.now())
        .transition_to(DebatePhase.COLLECTING_FINAL_PROPOSALS, at=clock.now())
        .transition_to(DebatePhase.SELECTING_WINNER, at=clock.now())
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        initial_opinions=tuple(
            InitialOpinion(participant, "summary", "proposal") for participant in PARTICIPANTS
        ),
        final_proposals=tuple(
            FinalProposal(participant, "title", "proposal") for participant in PARTICIPANTS
        ),
        votes=(
            Vote(
                ParticipantSlot.PARTICIPANT_A,
                ParticipantSlot.PARTICIPANT_C,
                3,
                3,
                3,
                "reason-a",
            ),
            Vote(
                ParticipantSlot.PARTICIPANT_B,
                ParticipantSlot.PARTICIPANT_A,
                3,
                3,
                3,
                "reason-b",
            ),
            Vote(
                ParticipantSlot.PARTICIPANT_C,
                ParticipantSlot.PARTICIPANT_B,
                3,
                3,
                3,
                "reason-c",
            ),
        ),
    )
    await repository.create(snapshot, operation_id="legacy-votes", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    completed = repository.current[debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert openai.vote_calls == []
    assert any(
        operation.plan_id == "votes" for operation in repository.terminal_operations.values()
    )


@pytest.mark.asyncio
async def test_initial_opinion_preflight_failure_stops_before_every_provider_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    discord = dependencies[3]
    openai = dependencies[5]
    repository = dependencies[6]
    discord.delivery_ready_by_slot[DiscordBotSlot.PARTICIPANT_B] = False
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "discord_delivery_preflight_failed"
    assert openai.initial_calls == []
    assert discord.delivery_checks == [
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
    ]
    checkpoints = tuple(
        checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
    )
    assert len(checkpoints) == 3
    assert all(checkpoint.status is GenerationStatus.FAILED for checkpoint in checkpoints)
    assert all(checkpoint.logical_attempt == 0 for checkpoint in checkpoints)


@pytest.mark.asyncio
async def test_initial_opinion_generation_recovery_uses_one_successor_call_per_participant(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = DebateState.accepted(debate_id, attempt_id, at=accepted_at)
    state = state.transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
    state = state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
    state = state.transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
    old_lease = LeaseGrant(
        owner_id="worker-old",
        slot=0,
        fencing_token=10,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    checkpoints = tuple(
        GenerationCheckpoint.planned(
            phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
            participant=participant,
            at=state.updated_at,
        ).claim(lease=old_lease, at=clock.now())
        for participant in PARTICIPANTS
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        generation_checkpoints=checkpoints,
    )
    await repository.create(snapshot, operation_id="recover-initial", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    completed = repository.current[debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    recovered = tuple(
        checkpoint
        for checkpoint in completed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
    )
    assert len(recovered) == 3
    assert all(checkpoint.status is GenerationStatus.COMPLETED for checkpoint in recovered)
    assert all(checkpoint.logical_attempt == 2 for checkpoint in recovered)
    assert set(openai.initial_calls) == set(PARTICIPANTS)


@pytest.mark.asyncio
async def test_initial_opinion_generation_stops_before_a_third_logical_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    clock, ids, _, _, _, openai, repository, _ = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    accepted_at = clock.now()
    state = DebateState.accepted(debate_id, attempt_id, at=accepted_at)
    state = state.transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
    state = state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
    state = state.transition_to(DebatePhase.COLLECTING_INITIAL_OPINIONS, at=clock.now())
    first_lease = LeaseGrant(
        owner_id="worker-old-1",
        slot=0,
        fencing_token=10,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    second_lease = LeaseGrant(
        owner_id="worker-old-2",
        slot=1,
        fencing_token=11,
        expires_at=state.updated_at + timedelta(seconds=60),
    )
    checkpoints = tuple(
        GenerationCheckpoint.planned(
            phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
            participant=participant,
            at=state.updated_at,
        )
        .claim(lease=first_lease, at=clock.now())
        .claim(lease=second_lease, at=clock.now())
        for participant in PARTICIPANTS
    )
    snapshot = DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=accepted_at,
        attempt_created_at=accepted_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=EvidenceBundle(),
        generation_checkpoints=checkpoints,
    )
    await repository.create(snapshot, operation_id="exhaust-initial", lease_owner="worker-1")
    app = make_application(dependencies)

    await app.run_debate(debate_id)

    failed = repository.current[debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "generation_attempts_exhausted"
    assert openai.initial_calls == []


@pytest.mark.asyncio
async def test_known_initial_provider_failure_keeps_other_completed_outputs_durable(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.initial_errors[ParticipantSlot.PARTICIPANT_A] = GenerationProviderError(
        "openai_initial_failure",
        "content-free provider failure",
        retryable=False,
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_initial_failure"
    assert {opinion.participant for opinion in failed.initial_opinions} == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }
    checkpoints = {
        checkpoint.participant: checkpoint
        for checkpoint in failed.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
    }
    assert checkpoints[ParticipantSlot.PARTICIPANT_A].status is GenerationStatus.FAILED
    assert checkpoints[ParticipantSlot.PARTICIPANT_B].status is GenerationStatus.COMPLETED
    assert checkpoints[ParticipantSlot.PARTICIPANT_C].status is GenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_initial_provider_participant_mismatch_fails_without_persisting_the_wrong_output(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.initial_participant_override[ParticipantSlot.PARTICIPANT_A] = (
        ParticipantSlot.PARTICIPANT_B
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_participant_mismatch"
    assert {opinion.participant for opinion in failed.initial_opinions} == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }


@pytest.mark.asyncio
async def test_run_renews_lease_while_a_phase_is_in_progress(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies, lease_renewal=0.001)
    _, _, _, _, evidence, _, repository, _ = dependencies
    evidence.delay = 0.01

    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)

    assert repository.renew_calls
    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED


@pytest.mark.asyncio
async def test_accept_fails_closed_when_runtime_or_channel_is_not_ready(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    discord = dependencies[3]
    repository = dependencies[6]
    discord.ready = False

    with pytest.raises(RuntimeNotReady):
        await app.accept_debate(request())
    discord.ready = True
    discord.allowed = False
    with pytest.raises(RequestNotAllowed):
        await app.accept_debate(request())

    assert repository.current == {}


@pytest.mark.asyncio
async def test_accept_operation_is_idempotent_and_bound_to_request(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    discord = dependencies[3]
    repository = dependencies[6]

    first = await app.accept_debate(request())
    discord.ready = False
    repeated = await app.accept_debate(request())

    assert repeated == first
    assert len(repository.current) == 1
    accepted = repository.current[first.debate_id]
    assert accepted.requester_username == "pitekusu"
    assert accepted.requester_display_name == "ぬし"
    with pytest.raises(InvalidApplicationOperation, match="another request"):
        await app.accept_debate(replace(request(), question="A different question"))
    with pytest.raises(InvalidApplicationOperation, match="another request"):
        await app.accept_debate(replace(request(), requester_username="other-user"))
    with pytest.raises(InvalidApplicationOperation, match="another request"):
        await app.accept_debate(replace(request(), requester_display_name="別名"))


@pytest.mark.parametrize("question", ["", " ", "x" * 1001])
def test_accept_request_rejects_invalid_question(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        AcceptDebateRequest(
            question,
            "requester",
            "pitekusu",
            "ぬし",
            "guild",
            "channel",
            "operation",
        )


@pytest.mark.parametrize("field", ["requester", "guild", "channel"])
def test_accept_request_rejects_empty_identifiers(field: str) -> None:
    values = {"requester": "requester", "guild": "guild", "channel": "channel"}
    values[field] = " "
    with pytest.raises(ValueError, match="must not be empty"):
        AcceptDebateRequest(
            "question",
            values["requester"],
            "pitekusu",
            "ぬし",
            values["guild"],
            values["channel"],
            "operation",
        )


@pytest.mark.parametrize(
    ("username", "display_name"),
    [("", "ぬし"), (" ", "ぬし"), ("pitekusu", ""), ("pitekusu", " ")],
)
def test_accept_request_rejects_empty_requester_names(
    username: str,
    display_name: str,
) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        AcceptDebateRequest(
            "question",
            "requester-id",
            username,
            display_name,
            "guild",
            "channel",
            "operation",
        )


def test_accept_request_preserves_unicode_names_without_normalization() -> None:
    request = AcceptDebateRequest(
        question="question",
        requester_id="requester-id",
        requester_username=" Pitekusu\u3000",
        requester_display_name=" ぬし ",
        guild_id="guild",
        channel_id="100",
        operation_id="operation",
    )
    assert request.requester_username == " Pitekusu\u3000"
    assert request.requester_display_name == " ぬし "
    assert request.requester_id == "requester-id"


@pytest.mark.asyncio
async def test_cancel_is_authorized_idempotent_and_terminal(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    with pytest.raises(RequestNotAllowed):
        await app.cancel_debate(CancelDebateCommand(accepted.debate_id, "other", "cancel-other"))
    cancelled = await app.cancel_debate(
        CancelDebateCommand(accepted.debate_id, "requester", "cancel-operation")
    )
    repeated = await app.cancel_debate(
        CancelDebateCommand(accepted.debate_id, "moderator", "cancel-operation", True)
    )
    await app.run_debate(accepted.debate_id)

    assert cancelled == repeated
    assert repository.current[accepted.debate_id].state.phase is DebatePhase.CANCELLED
    assert repository.current[accepted.debate_id].terminal_delivery_complete


@pytest.mark.asyncio
async def test_stale_runtime_cannot_cancel_with_the_replacement_runtime_lease(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    stale = make_application(dependencies, lease_owner="stale-runtime")
    replacement = make_application(dependencies, lease_owner="replacement-runtime")
    repository = dependencies[6]
    accepted = await accept_bound_debate(stale)
    previous = repository.current[accepted.debate_id]
    assert previous.lease is not None
    replacement_snapshot = replace(
        previous,
        lease=replace(
            previous.lease,
            owner_id="replacement-runtime",
            fencing_token=previous.lease.fencing_token + 1,
        ),
    )
    repository.current[accepted.debate_id] = replacement_snapshot

    with pytest.raises(RepositoryConflict, match="not owned"):
        await stale.cancel_debate(
            CancelDebateCommand(accepted.debate_id, "requester", "stale-cancel")
        )

    cancelled = await replacement.cancel_debate(
        CancelDebateCommand(accepted.debate_id, "requester", "replacement-cancel")
    )
    await replacement.run_debate(accepted.debate_id)

    assert cancelled.debate_id == accepted.debate_id
    assert repository.current[accepted.debate_id].state.phase is DebatePhase.CANCELLED


@pytest.mark.asyncio
async def test_expired_debate_lease_cannot_authorize_cancellation(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    current = repository.current[accepted.debate_id]
    assert current.lease is not None
    repository.current[accepted.debate_id] = replace(
        current,
        lease=replace(current.lease, expires_at=dependencies[0].current - timedelta(seconds=1)),
    )

    with pytest.raises(RepositoryConflict, match="not owned"):
        await app.cancel_debate(
            CancelDebateCommand(accepted.debate_id, "requester", "expired-lease-cancel")
        )


@pytest.mark.asyncio
async def test_completed_debate_cannot_be_cancelled(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)

    with pytest.raises(InvalidApplicationOperation):
        await app.cancel_debate(
            CancelDebateCommand(accepted.debate_id, "requester", "cancel-completed")
        )


@pytest.mark.asyncio
async def test_panel_cancel_rejects_a_different_source_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    accepted = await app.accept_debate(request())

    with pytest.raises(InvalidApplicationOperation, match="another attempt"):
        await app.cancel_debate(
            CancelDebateCommand(
                accepted.debate_id,
                "requester",
                "cancel-stale-panel",
                expected_attempt_id=dependencies[1].new_attempt_id(),
            )
        )


@pytest.mark.asyncio
async def test_failed_attempt_retry_preserves_source_and_reuses_completed_artifacts(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    clock, _, _, _, _, _, repository, _ = dependencies
    accepted = await app.accept_debate(request())
    source = repository.current[accepted.debate_id]
    failed_state = source.state.transition_to(DebatePhase.FAILED, at=clock.now())
    failed = replace(source, state=failed_state, error_code="test_failure")
    await repository.replace(expected=source, updated=failed)

    retried = await app.retry_debate(
        RetryDebateCommand(accepted.debate_id, "moderator", "retry-operation", True)
    )

    current = repository.current[accepted.debate_id]
    assert current.state.phase is DebatePhase.ACCEPTED
    assert current.state.retry_of == failed.state.attempt_id
    assert current.state.attempt_id == retried.attempt_id
    assert failed.state.phase is DebatePhase.FAILED
    assert current.error_code is None
    assert current.requester_username == failed.requester_username == "pitekusu"
    assert current.requester_display_name == failed.requester_display_name == "ぬし"


@pytest.mark.asyncio
async def test_retry_reuses_partial_final_proposals_and_generates_only_the_missing_output(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.proposal_errors[ParticipantSlot.PARTICIPANT_A] = GenerationProviderError(
        "openai_incomplete",
        "content-free provider failure",
        retryable=False,
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert {proposal.participant for proposal in failed.final_proposals} == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }
    del openai.proposal_errors[ParticipantSlot.PARTICIPANT_A]

    await app.retry_debate(
        RetryDebateCommand(accepted.debate_id, "requester", "retry-partial-proposals")
    )

    retried = repository.current[accepted.debate_id]
    checkpoints = {
        checkpoint.participant: checkpoint
        for checkpoint in retried.generation_checkpoints
        if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    }
    assert checkpoints[ParticipantSlot.PARTICIPANT_A].status is GenerationStatus.PLANNED
    assert checkpoints[ParticipantSlot.PARTICIPANT_B].status is GenerationStatus.COMPLETED
    assert checkpoints[ParticipantSlot.PARTICIPANT_C].status is GenerationStatus.COMPLETED
    assert checkpoints[ParticipantSlot.PARTICIPANT_B].logical_attempt == 0
    assert checkpoints[ParticipantSlot.PARTICIPANT_C].logical_attempt == 0

    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert openai.proposal_calls.count(ParticipantSlot.PARTICIPANT_A) == 2
    assert openai.proposal_calls.count(ParticipantSlot.PARTICIPANT_B) == 1
    assert openai.proposal_calls.count(ParticipantSlot.PARTICIPANT_C) == 1


@pytest.mark.asyncio
async def test_retry_operation_is_idempotent(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    clock = dependencies[0]
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    source = repository.current[accepted.debate_id]
    failed = replace(
        source,
        state=source.state.transition_to(DebatePhase.FAILED, at=clock.now()),
        error_code="test_failure",
    )
    await repository.replace(expected=source, updated=failed)
    command = RetryDebateCommand(accepted.debate_id, "requester", "retry-idempotent")

    first = await app.retry_debate(command)
    repeated = await app.retry_debate(command)

    assert repeated == first
    assert repository.current[accepted.debate_id].state.attempt_id == first.attempt_id


@pytest.mark.asyncio
async def test_retry_requires_authorized_actor_and_failed_state(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    accepted = await app.accept_debate(request())

    with pytest.raises(RequestNotAllowed):
        await app.retry_debate(RetryDebateCommand(accepted.debate_id, "other", "retry-other"))
    with pytest.raises(InvalidApplicationOperation):
        await app.retry_debate(
            RetryDebateCommand(accepted.debate_id, "requester", "retry-not-failed")
        )


@pytest.mark.asyncio
async def test_panel_retry_rejects_a_different_source_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    clock = dependencies[0]
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    source = repository.current[accepted.debate_id]
    failed = replace(
        source,
        state=source.state.transition_to(DebatePhase.FAILED, at=clock.now()),
        error_code="test_failure",
    )
    await repository.replace(expected=source, updated=failed)

    with pytest.raises(InvalidApplicationOperation, match="another attempt"):
        await app.retry_debate(
            RetryDebateCommand(
                accepted.debate_id,
                "requester",
                "retry-stale-panel",
                expected_attempt_id=dependencies[1].new_attempt_id(),
            )
        )


@pytest.mark.asyncio
async def test_phase_timeout_marks_attempt_failed(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    evidence = dependencies[4]
    repository = dependencies[6]
    evidence.delay = 0.05
    app = make_application(dependencies, phase_timeout=0.001)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.PREPARING_EVIDENCE
    assert failed.error_code == "phase_deadline_exceeded"
    assert failed.escalation_assessment is None


@pytest.mark.asyncio
async def test_session_timeout_has_distinct_stable_error_code(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    dependencies[4].delay = 0.05
    repository = dependencies[6]
    app = make_application(dependencies, session_timeout=0.001, phase_timeout=1.0)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.error_code == "session_deadline_exceeded"
    assert failed.escalation_assessment is None


@pytest.mark.asyncio
async def test_task_group_cancels_siblings_and_persists_failure(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.block_initial = True
    openai.fail_initial_for = ParticipantSlot.PARTICIPANT_A
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.FAILED
    assert openai.cancelled_initial == {
        ParticipantSlot.PARTICIPANT_B,
        ParticipantSlot.PARTICIPANT_C,
    }


@pytest.mark.asyncio
async def test_external_cancellation_checkpoints_and_propagates_cancelled_error(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    dependencies[4].delay = 10.0
    repository = dependencies[6]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)
    running = asyncio.create_task(app.run_debate(accepted.debate_id))
    await dependencies[4].called.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    checkpointed = repository.current[accepted.debate_id]
    assert checkpointed.state.phase is DebatePhase.PREPARING_EVIDENCE
    assert checkpointed.state.recovery_state is RecoveryState.CHECKPOINTED


@pytest.mark.asyncio
async def test_resume_recoverable_resumes_checkpointed_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    outbox_recovery = FakeOutboxRecovery()
    app = make_application(dependencies, outbox_recovery=outbox_recovery)
    clock = dependencies[0]
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)
    source = repository.current[accepted.debate_id]
    checkpointed = replace(source, state=source.state.checkpoint(at=clock.now()))
    await repository.replace(expected=source, updated=checkpointed)
    repository.recoverable = (accepted.debate_id,)

    await app.resume_recoverable()

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    assert len(outbox_recovery.calls) == 5
    assert outbox_recovery.calls[0].state.recovery_state is RecoveryState.CHECKPOINTED
    assert outbox_recovery.calls[1].terminal_delivery is not None
    assert MetricEvent.RESUMED in {event for event, _ in dependencies[2].events}


@pytest.mark.asyncio
async def test_generation_recovery_persists_claim_before_call_and_completes_on_second_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_errors.append(asyncio.CancelledError())
    first_worker = make_application(dependencies, lease_owner="worker-1")
    accepted = await accept_bound_debate(first_worker)

    with pytest.raises(asyncio.CancelledError):
        await first_worker.run_debate(accepted.debate_id)

    persisted_claims = tuple(
        checkpoint
        for historical in repository.history[accepted.debate_id]
        for checkpoint in historical.generation_checkpoints
        if checkpoint.status is GenerationStatus.IN_FLIGHT
    )
    assert len(openai.decision_calls) == 1
    assert persisted_claims
    assert persisted_claims[0].logical_attempt == 1
    repository.recoverable = (accepted.debate_id,)

    await make_application(dependencies, lease_owner="worker-2").resume_recoverable()

    completed = repository.current[accepted.debate_id]
    checkpoint = completed.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert completed.state.phase is DebatePhase.COMPLETED
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.COMPLETED
    assert checkpoint.logical_attempt == 2
    assert len(openai.decision_calls) == 2


@pytest.mark.asyncio
async def test_generation_recovery_fails_without_a_third_provider_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_errors.extend((asyncio.CancelledError(), asyncio.CancelledError()))
    first_worker = make_application(dependencies, lease_owner="worker-1")
    accepted = await accept_bound_debate(first_worker)

    with pytest.raises(asyncio.CancelledError):
        await first_worker.run_debate(accepted.debate_id)
    repository.recoverable = (accepted.debate_id,)
    await make_application(dependencies, lease_owner="worker-2").resume_recoverable()

    second_claim = repository.current[accepted.debate_id].checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert second_claim is not None
    assert second_claim.status is GenerationStatus.IN_FLIGHT
    assert second_claim.logical_attempt == 2
    await make_application(dependencies, lease_owner="worker-3").resume_recoverable()

    failed = repository.current[accepted.debate_id]
    checkpoint = failed.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "generation_attempts_exhausted"
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.FAILED
    assert checkpoint.logical_attempt == 2
    assert len(openai.decision_calls) == 2


@pytest.mark.asyncio
async def test_generation_settlement_uses_a_heartbeat_renewed_lease(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_delay = 0.03
    app = make_application(dependencies, lease_renewal=0.005)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    checkpoint = completed.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert completed.state.phase is DebatePhase.COMPLETED
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.COMPLETED
    assert checkpoint.logical_attempt == 1
    assert len(openai.decision_calls) == 1
    assert repository.renew_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [False, True])
async def test_known_generation_provider_failure_records_one_logical_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    *,
    retryable: bool,
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_errors.append(
        GenerationProviderError(
            "openai_known_failure",
            "content-free provider failure",
            retryable=retryable,
        )
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    checkpoint = failed.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "openai_known_failure"
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.FAILED
    assert checkpoint.logical_attempt == 1
    assert len(openai.decision_calls) == 1


@pytest.mark.asyncio
async def test_winner_delivery_preflight_failure_stops_before_the_provider_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    discord = dependencies[3]
    openai = dependencies[5]
    repository = dependencies[6]
    discord.delivery_ready_results = [True] * 9 + [True, False]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    checkpoint = failed.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "discord_delivery_preflight_failed"
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.FAILED
    assert checkpoint.logical_attempt == 0
    assert openai.decision_calls == []
    assert discord.delivery_checks == [
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_A, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_C, "guild", "102"),
        (DiscordBotSlot.MODERATOR, "guild", "102"),
        (DiscordBotSlot.PARTICIPANT_B, "guild", "102"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_shape", ["wrong_participant", "duplicate"])
async def test_decision_generation_rejects_an_ambiguous_checkpoint_set_before_external_calls(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    checkpoint_shape: str,
) -> None:
    discord = dependencies[3]
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_errors.append(asyncio.CancelledError())
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    with pytest.raises(asyncio.CancelledError):
        await app.run_debate(accepted.debate_id)

    current = repository.current[accepted.debate_id]
    winner_checkpoint = current.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert winner_checkpoint is not None
    wrong_checkpoint = replace(
        winner_checkpoint,
        participant=ParticipantSlot.PARTICIPANT_A,
    )
    checkpoints = (
        (wrong_checkpoint,)
        if checkpoint_shape == "wrong_participant"
        else (
            winner_checkpoint,
            GenerationCheckpoint.planned(
                phase=DebatePhase.GENERATING_DECISION,
                participant=ParticipantSlot.PARTICIPANT_A,
                at=current.state.updated_at,
            ),
        )
    )
    repository.current[accepted.debate_id] = replace(
        current,
        generation_checkpoints=checkpoints,
    )
    provider_calls = tuple(openai.decision_calls)
    delivery_checks = tuple(discord.delivery_checks)

    await app.run_debate(accepted.debate_id)

    assert tuple(openai.decision_calls) == provider_calls
    assert tuple(discord.delivery_checks) == delivery_checks
    unchanged = repository.current[accepted.debate_id]
    assert unchanged.state.phase is DebatePhase.GENERATING_DECISION
    assert unchanged.generation_checkpoints == checkpoints
    assert unchanged.terminal_delivery is None


@pytest.mark.asyncio
async def test_cancel_settles_an_in_flight_generation_without_another_provider_call(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    openai = dependencies[5]
    repository = dependencies[6]
    openai.decision_errors.append(asyncio.CancelledError())
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)
    with pytest.raises(asyncio.CancelledError):
        await app.run_debate(accepted.debate_id)

    await app.cancel_debate(
        CancelDebateCommand(accepted.debate_id, "requester", "cancel-generation")
    )
    await app.run_debate(accepted.debate_id)

    cancelled = repository.current[accepted.debate_id]
    checkpoint = cancelled.checkpoint_for(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
    assert cancelled.state.phase is DebatePhase.CANCELLED
    assert checkpoint is not None
    assert checkpoint.status is GenerationStatus.FAILED
    assert checkpoint.error_code == "generation_cancelled"
    assert len(openai.decision_calls) == 1


@pytest.mark.asyncio
async def test_bounded_delivery_reconciles_before_abandonment(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    class BoundAtTerminalRecovery(FakeOutboxRecovery):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            self.calls.append(expected)
            if expected.terminal_delivery is not None:
                raise OutboxRecoveryAbandoned("deadline_exceeded")

    recovery = BoundAtTerminalRecovery(termination_complete=True)
    app = make_application(dependencies, outbox_recovery=recovery)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    completed = dependencies[6].current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert isinstance(completed.terminal_delivery, PhaseDeliveryPlan)
    assert completed.terminal_delivery.status is PhaseDeliveryStatus.DELIVERED
    assert len(recovery.calls) == 9
    terminating = recovery.calls[-1].terminal_delivery
    assert isinstance(terminating, PhaseDeliveryPlan)
    assert terminating.status is PhaseDeliveryStatus.TERMINATING


@pytest.mark.asyncio
async def test_completed_delivery_abandonment_converges_through_best_effort_failed_notice(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    class AbandonEveryPlan(FakeOutboxRecovery):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            self.calls.append(expected)
            if expected.terminal_delivery is not None:
                raise OutboxRecoveryAbandoned("deadline_exceeded")

    recovery = AbandonEveryPlan(termination_complete=False)
    app = make_application(dependencies, outbox_recovery=recovery)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "discord_outbox_deadline_exceeded"
    assert isinstance(failed.terminal_delivery, PhaseDeliveryPlan)
    assert failed.terminal_delivery.target_phase is DebatePhase.FAILED
    assert failed.terminal_delivery.status is PhaseDeliveryStatus.ABANDONED


@pytest.mark.asyncio
async def test_cancelled_notice_abandonment_does_not_block_terminal_convergence(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    class AbandonEveryPlan(FakeOutboxRecovery):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            self.calls.append(expected)
            if expected.terminal_delivery is not None:
                raise OutboxRecoveryAbandoned("deadline_exceeded")

    recovery = AbandonEveryPlan(termination_complete=False)
    app = make_application(dependencies, outbox_recovery=recovery)
    accepted = await accept_bound_debate(app)
    await app.cancel_debate(
        CancelDebateCommand(accepted.debate_id, "requester", "cancel-best-effort")
    )

    await app.run_debate(accepted.debate_id)

    cancelled = dependencies[6].current[accepted.debate_id]
    assert cancelled.state.phase is DebatePhase.CANCELLED
    assert isinstance(cancelled.terminal_delivery, PhaseDeliveryPlan)
    assert cancelled.terminal_delivery.target_phase is DebatePhase.CANCELLED
    assert cancelled.terminal_delivery.status is PhaseDeliveryStatus.ABANDONED


@pytest.mark.asyncio
async def test_claim_recoverable_does_not_start_phase_work(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    original = repository.current[accepted.debate_id]
    repository.recoverable = (accepted.debate_id,)

    claimed = await app.claim_recoverable()

    assert claimed == (repository.current[accepted.debate_id],)
    assert claimed[0].state.phase is DebatePhase.ACCEPTED
    assert repository.current[accepted.debate_id].state == original.state


@pytest.mark.asyncio
async def test_nonretryable_outbox_recovery_failure_preserves_required_delivery_plan(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    outbox_recovery = FakeOutboxRecovery(error=OutboxRecoveryFailed("DISCORD_OUTBOX_CONFLICT"))
    app = make_application(dependencies, outbox_recovery=outbox_recovery)
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    pending = repository.current[accepted.debate_id]
    assert pending.state.phase is DebatePhase.ACCEPTED
    assert pending.error_code == "DISCORD_OUTBOX_CONFLICT"
    assert pending.terminal_delivery is not None
    assert pending.terminal_delivery.target_phase is DebatePhase.FAILED
    assert pending.terminal_delivery.completed_at is None
    assert pending.lease is not None
    assert len(outbox_recovery.calls) == 2


@pytest.mark.asyncio
async def test_typed_outbox_transaction_conflict_retries_without_waiting_for_recovery(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ConflictOnceTerminalOutbox(FakeOutboxRecovery):
        conflicted = False

        async def drain(self, *, expected: DebateSnapshot) -> None:
            self.calls.append(expected)
            if expected.terminal_delivery is not None and not self.conflicted:
                self.conflicted = True
                raise RepositoryTransactionConflict(
                    stage=RepositoryTransactionStage.OUTBOX_CLAIM,
                    failures=(
                        (
                            RepositoryTransactionAction.OUTBOX_OPERATION,
                            RepositoryCancellationCode.TRANSACTION_CONFLICT,
                        ),
                    ),
                    reasons_complete=True,
                )

    outbox_recovery = ConflictOnceTerminalOutbox()
    app = make_application(dependencies, outbox_recovery=outbox_recovery)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    completed = dependencies[6].current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.terminal_delivery_complete
    assert len(outbox_recovery.calls) == 6
    assert MetricEvent.TERMINAL_DELIVERY_CONFLICT_RETRY in {
        event for event, _ in dependencies[2].events
    }
    event = json.loads(
        next(
            record.message
            for record in caplog.records
            if json.loads(record.message).get("event") == "terminal_delivery_conflict"
        )
    )
    assert event["transaction_stage"] == "outbox_claim"
    assert event["failed_action_kinds"] == ["outbox_operation"]
    assert event["failure_codes"] == ["TransactionConflict"]
    assert event["cancellation_reasons_complete"] is True
    assert event["retryable"] is True


@pytest.mark.asyncio
async def test_terminal_attempt_cas_conflict_logs_action_and_retries(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = dependencies[6]
    repository.terminal_finalize_errors.append(
        RepositoryTransactionConflict(
            stage=RepositoryTransactionStage.TERMINAL_FINALIZE,
            failures=(
                (
                    RepositoryTransactionAction.ATTEMPT_CAS,
                    RepositoryCancellationCode.CONDITIONAL_CHECK_FAILED,
                ),
            ),
            reasons_complete=True,
        )
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    event = json.loads(
        next(
            record.message
            for record in caplog.records
            if json.loads(record.message).get("event") == "terminal_delivery_conflict"
        )
    )
    assert event == {
        "cancellation_reasons_complete": True,
        "debate_id": str(accepted.debate_id),
        "event": "terminal_delivery_conflict",
        "failed_action_kinds": ["attempt_cas"],
        "failure_codes": ["ConditionalCheckFailed"],
        "retry_delay_seconds": 0.0,
        "retry_number": 1,
        "retryable": True,
        "severity": "WARNING",
        "transaction_stage": "terminal_finalize",
    }


@pytest.mark.asyncio
async def test_terminal_outbox_condition_conflict_fails_closed_without_hot_retry(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = dependencies[6]
    repository.terminal_finalize_errors.append(
        RepositoryTransactionConflict(
            stage=RepositoryTransactionStage.TERMINAL_FINALIZE,
            failures=(
                (
                    RepositoryTransactionAction.OUTBOX_SENT_CHECK,
                    RepositoryCancellationCode.CONDITIONAL_CHECK_FAILED,
                ),
            ),
            reasons_complete=True,
        )
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    with (
        caplog.at_level(logging.WARNING, logger="shittim_chest"),
        pytest.raises(RuntimeError, match="not retryable"),
    ):
        await app.run_debate(accepted.debate_id)

    assert repository.terminal_finalize_errors == []
    assert repository.terminal_finalizations == []
    assert MetricEvent.TERMINAL_DELIVERY_CONFLICT_RETRY not in {
        event for event, _ in dependencies[2].events
    }
    event = json.loads(caplog.records[-1].message)
    assert event["failed_action_kinds"] == ["outbox_sent_check"]
    assert event["failure_codes"] == ["ConditionalCheckFailed"]
    assert event["retryable"] is False
    assert event["retry_number"] is None


@pytest.mark.asyncio
async def test_terminal_delivery_conflict_exhaustion_remains_durable_and_is_not_hidden(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    class ConflictingTerminalOutbox(FakeOutboxRecovery):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            self.calls.append(expected)
            if expected.terminal_delivery is not None:
                raise RepositoryConflict("simulated persistent terminal delivery race")

    outbox_recovery = ConflictingTerminalOutbox()
    app = make_application(dependencies, outbox_recovery=outbox_recovery)
    accepted = await accept_bound_debate(app)

    with pytest.raises(RuntimeError, match="not retryable"):
        await app.run_debate(accepted.debate_id)

    pending = dependencies[6].current[accepted.debate_id]
    assert not pending.state.phase.is_terminal
    assert pending.terminal_delivery is not None
    assert pending.terminal_delivery.completed_at is None
    assert len(outbox_recovery.calls) == 2
    assert MetricEvent.TERMINAL_DELIVERY_CONFLICT_RETRY not in {
        event for event, _ in dependencies[2].events
    }


@pytest.mark.asyncio
async def test_outbox_recovery_wait_is_outside_session_deadline_and_renews_lease(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    outbox_recovery = FakeOutboxRecovery(delay=0.03)
    app = make_application(
        dependencies,
        outbox_recovery=outbox_recovery,
        session_timeout=0.01,
        lease_renewal=0.005,
    )
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    assert repository.renew_calls


@pytest.mark.asyncio
async def test_outbox_fencing_conflict_does_not_terminalize_the_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(
        dependencies,
        outbox_recovery=FakeOutboxRecovery(error=RepositoryConflict("lost fencing")),
    )
    repository = dependencies[6]
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    current = repository.current[accepted.debate_id]
    assert current.state.phase is DebatePhase.ACCEPTED
    assert current.error_code is None
    assert MetricEvent.TERMINAL_DELIVERY_CONFLICT_RETRY not in {
        event for event, _ in dependencies[2].events
    }


@pytest.mark.asyncio
async def test_recovery_reuses_every_completed_phase_artifact(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)
    clock, ids, _, _, evidence_service, openai, repository, orderer = dependencies
    debate_id = ids.new_debate_id()
    attempt_id = ids.new_attempt_id()
    state = (
        DebateState.accepted(debate_id, attempt_id, at=clock.now())
        .transition_to(DebatePhase.SCORING_AFFECTION, at=clock.now())
        .transition_to(DebatePhase.PREPARING_EVIDENCE, at=clock.now())
    )
    evidence = EvidenceBundle()
    opinions = tuple(InitialOpinion(slot, "summary", "proposal") for slot in PARTICIPANTS)
    proposals = tuple(FinalProposal(slot, "title", "proposal") for slot in PARTICIPANTS)
    votes = (
        Vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_C, 3, 3, 3, "reason"),
        Vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A, 3, 3, 3, "reason"),
        Vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B, 3, 3, 3, "reason"),
    )
    snapshot = DebateSnapshot(
        state=state,
        question="cached question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="100",
        created_at=state.updated_at,
        attempt_created_at=state.updated_at,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        evidence=evidence,
        initial_opinions=opinions,
        final_proposals=proposals,
        votes=votes,
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "cached decision",
            (),
            (),
        ),
    )
    await repository.create(snapshot, operation_id="cached-create", lease_owner="worker-1")

    await app.run_debate(debate_id)

    assert repository.current[debate_id].state.phase is DebatePhase.COMPLETED
    assert evidence_service.calls == []
    assert openai.initial_calls == []
    assert openai.proposal_calls == []
    assert openai.vote_calls == []
    assert openai.decision_calls == []
    assert orderer.calls == []


@pytest.mark.asyncio
async def test_not_found_and_invalid_timeout_configuration(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    app = make_application(dependencies)

    with pytest.raises(DebateNotFound):
        await app.run_debate(dependencies[1].new_debate_id())
    with pytest.raises(ValueError, match="timeouts"):
        make_application(dependencies, session_timeout=0)


@pytest.mark.asyncio
async def test_corrupt_candidate_order_fails_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    dependencies[7].corrupt = True
    repository = dependencies[6]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.FAILED


@pytest.mark.asyncio
async def test_duplicate_candidate_order_fails_attempt(
    dependencies: tuple[
        FakeClock,
        FakeIds,
        FakeMetrics,
        FakeDiscord,
        FakeEvidence,
        FakeOpenAI,
        FakeRepository,
        FakeCandidateOrderer,
    ],
) -> None:
    dependencies[7].duplicate = True
    repository = dependencies[6]
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.FAILED
