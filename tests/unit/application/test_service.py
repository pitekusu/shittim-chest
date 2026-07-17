"""Tests for the SDK-independent debate application service."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from shittim_chest.application import (
    AcceptDebateRequest,
    CancelDebateCommand,
    DebateApplication,
    DebateNotFound,
    InvalidApplicationOperation,
    RequestNotAllowed,
    RequiredEvidenceUnavailable,
    RetryDebateCommand,
    RuntimeNotReady,
)
from shittim_chest.application.models import DebateSnapshot, MetricEvent
from shittim_chest.domain import (
    PARTICIPANTS,
    DebatePhase,
    DebateState,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
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
        lease_owner="worker-1",
        session_timeout_seconds=session_timeout,
        phase_timeout_seconds=phase_timeout,
        lease_renewal_seconds=lease_renewal,
    )


def request(*, requester_id: str = "requester") -> AcceptDebateRequest:
    return AcceptDebateRequest(
        question="What should we eat?",
        requester_id=requester_id,
        guild_id="guild",
        channel_id="channel",
        operation_id="accept-operation",
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

    accepted = await app.accept_debate(request())
    await app.run_debate(accepted.debate_id)

    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.final_decision is not None
    assert completed.final_decision.winner is ParticipantSlot.PARTICIPANT_B
    assert evidence.calls == ["What should we eat?"]
    assert set(openai.initial_calls) == set(ParticipantSlot)
    assert set(openai.proposal_calls) == set(ParticipantSlot)
    assert len(openai.vote_calls) == 3
    assert len(orderer.calls) == 3
    assert all(voter not in candidates for voter, candidates in orderer.calls)
    assert MetricEvent.COMPLETED in {event for event, _ in metrics.events}
    assert [item.state.phase for item in repository.history[accepted.debate_id]] == list(
        DebatePhase
    )[:8]


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

    accepted = await app.accept_debate(request())
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
    with pytest.raises(InvalidApplicationOperation, match="another request"):
        await app.accept_debate(replace(request(), question="A different question"))


@pytest.mark.parametrize("question", ["", " ", "x" * 1001])
def test_accept_request_rejects_invalid_question(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        AcceptDebateRequest(question, "requester", "guild", "channel", "operation")


@pytest.mark.parametrize("field", ["requester", "guild", "channel"])
def test_accept_request_rejects_empty_identifiers(field: str) -> None:
    values = {"requester": "requester", "guild": "guild", "channel": "channel"}
    values[field] = " "
    with pytest.raises(ValueError, match="must not be empty"):
        AcceptDebateRequest(
            "question",
            values["requester"],
            values["guild"],
            values["channel"],
            "operation",
        )


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
    accepted = await app.accept_debate(request())

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
    accepted = await app.accept_debate(request())
    await app.run_debate(accepted.debate_id)

    with pytest.raises(InvalidApplicationOperation):
        await app.cancel_debate(
            CancelDebateCommand(accepted.debate_id, "requester", "cancel-completed")
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
    accepted = await app.accept_debate(request())

    await app.run_debate(accepted.debate_id)

    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.PREPARING_EVIDENCE
    assert failed.error_code == "phase_deadline_exceeded"


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
    accepted = await app.accept_debate(request())

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].error_code == "session_deadline_exceeded"


@pytest.mark.asyncio
async def test_required_evidence_failure_has_distinct_stable_error_code(
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
    dependencies[4].error = RequiredEvidenceUnavailable("search failed")
    repository = dependencies[6]
    app = make_application(dependencies)
    accepted = await app.accept_debate(request())

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].error_code == "required_evidence_unavailable"


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
    accepted = await app.accept_debate(request())

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
    accepted = await app.accept_debate(request())
    running = asyncio.create_task(app.run_debate(accepted.debate_id))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

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
    app = make_application(dependencies)
    clock = dependencies[0]
    repository = dependencies[6]
    accepted = await app.accept_debate(request())
    source = repository.current[accepted.debate_id]
    checkpointed = replace(source, state=source.state.checkpoint(at=clock.now()))
    await repository.replace(expected=source, updated=checkpointed)
    repository.recoverable = (accepted.debate_id,)

    await app.resume_recoverable()

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    assert MetricEvent.RESUMED in {event for event, _ in dependencies[2].events}


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
    state = DebateState.accepted(debate_id, attempt_id, at=clock.now()).transition_to(
        DebatePhase.PREPARING_EVIDENCE,
        at=clock.now(),
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
        guild_id="guild",
        channel_id="channel",
        created_at=state.updated_at,
        attempt_created_at=state.updated_at,
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
    accepted = await app.accept_debate(request())

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
    accepted = await app.accept_debate(request())

    await app.run_debate(accepted.debate_id)

    assert repository.current[accepted.debate_id].state.phase is DebatePhase.FAILED
