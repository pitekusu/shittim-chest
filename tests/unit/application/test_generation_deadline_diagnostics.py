"""Generation deadlines identify the affected work without recording private content."""

import asyncio
import json
import logging
from dataclasses import replace

import pytest

from shittim_chest.application.models import GenerationStatus
from shittim_chest.domain import (
    PARTICIPANTS,
    CandidatePlan,
    DebatePhase,
    EvidenceBundle,
    ParticipantSlot,
    PreferenceFrame,
    RecoveryState,
)
from shittim_chest.domain.attachments import ImageInput
from tests.unit.application.fakes import FakeOpenAI
from tests.unit.application.test_service import (
    Dependencies,
    accept_bound_debate,
    make_application,
    request,
)
from tests.unit.application.test_service import (
    dependencies as dependencies,
)


class BlockedCandidateOpenAI(FakeOpenAI):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def select_candidates(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        images: tuple[ImageInput, ...] = (),
        evidence: EvidenceBundle,
        preference_frame: PreferenceFrame,
    ) -> CandidatePlan:
        if participant is ParticipantSlot.PARTICIPANT_C:
            self.candidate_calls.append(participant)
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.add_note("private-provider-exception-sentinel")
                raise
        return await super().select_candidates(
            participant=participant,
            question=question,
            images=images,
            evidence=evidence,
            preference_frame=preference_frame,
        )


def deadline_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    events = [json.loads(record.message) for record in caplog.records]
    return [event for event in events if event.get("event") == "generation_deadline_exceeded"]


@pytest.mark.asyncio
async def test_candidate_deadline_logs_only_metadata_and_preserves_completed_siblings(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    openai = BlockedCandidateOpenAI()
    dependencies = (*dependencies[:5], openai, *dependencies[6:])
    timeout_seconds = 0.1
    app = make_application(
        dependencies,
        phase_timeout=timeout_seconds,
        participant_display_names={slot: f"private-persona-{slot.value}" for slot in PARTICIPANTS},
    )
    accepted = await accept_bound_debate(
        app,
        replace(
            request(),
            question="private-question-sentinel",
            requester_id=str(9 * 10**17 + 1),
            guild_id=str(9 * 10**17 + 2),
            channel_id=str(9 * 10**17 + 3),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.SELECTING_CANDIDATES
    assert failed.error_code == "generation_deadline_exceeded"
    assert {plan.participant for plan in failed.candidate_plans} == set(PARTICIPANTS[:2])
    for participant in PARTICIPANTS:
        checkpoint = failed.checkpoint_for(
            phase=DebatePhase.SELECTING_CANDIDATES, participant=participant
        )
        assert checkpoint is not None
        assert checkpoint.status is (
            GenerationStatus.FAILED
            if participant is ParticipantSlot.PARTICIPANT_C
            else GenerationStatus.COMPLETED
        )
        assert checkpoint.logical_attempt == 1
    assert not openai.initial_calls
    events = deadline_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert set(event) == {
        "severity",
        "event",
        "debate_id",
        "phase",
        "participant",
        "timeout_seconds",
        "elapsed_ms",
    }
    assert event["severity"] == "WARNING"
    assert event["debate_id"] == str(accepted.debate_id)
    assert event["phase"] == DebatePhase.SELECTING_CANDIDATES.value
    assert event["participant"] == ParticipantSlot.PARTICIPANT_C.value
    assert event["timeout_seconds"] == timeout_seconds
    assert isinstance(event["elapsed_ms"], int)
    assert event["elapsed_ms"] >= 0
    assert "private-" not in caplog.text
    assert all(
        identifier not in caplog.text
        for identifier in (failed.requester_id, failed.guild_id, failed.channel_id)
    )
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_external_cancellation_does_not_emit_generation_deadline_diagnostic(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    openai = BlockedCandidateOpenAI()
    dependencies = (*dependencies[:5], openai, *dependencies[6:])
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        running = asyncio.create_task(app.run_debate(accepted.debate_id))
        try:
            async with asyncio.timeout(1):
                await openai.started.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    checkpointed = dependencies[6].current[accepted.debate_id]
    assert checkpointed.state.phase is DebatePhase.SELECTING_CANDIDATES
    assert checkpointed.state.recovery_state is RecoveryState.CHECKPOINTED
    assert checkpointed.error_code is None
    assert deadline_events(caplog) == []
    assert "private-" not in caplog.text


@pytest.mark.asyncio
async def test_session_deadline_does_not_emit_generation_deadline_diagnostic(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    openai = BlockedCandidateOpenAI()
    dependencies = (*dependencies[:5], openai, *dependencies[6:])
    app = make_application(dependencies, session_timeout=0.1, phase_timeout=1.0)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.error_code == "session_deadline_exceeded"
    assert openai.started.is_set()
    assert deadline_events(caplog) == []
    assert "private-" not in caplog.text


@pytest.mark.asyncio
async def test_evidence_deadline_uses_null_participant_and_preserves_existing_failure_code(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dependencies[4].delay = 1.0
    app = make_application(dependencies, phase_timeout=0.1)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.PREPARING_EVIDENCE
    assert failed.error_code == "phase_deadline_exceeded"
    events = deadline_events(caplog)
    assert len(events) == 1
    assert events[0]["phase"] == DebatePhase.PREPARING_EVIDENCE.value
    assert events[0]["debate_id"] == str(accepted.debate_id)
    assert events[0]["participant"] is None


@pytest.mark.asyncio
async def test_provider_timeout_does_not_emit_an_application_deadline_diagnostic(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dependencies[5].candidate_errors[ParticipantSlot.PARTICIPANT_C] = TimeoutError(
        "private-provider-timeout-sentinel"
    )
    app = make_application(dependencies)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.SELECTING_CANDIDATES
    assert failed.error_code == "generation_deadline_exceeded"
    assert deadline_events(caplog) == []
    assert "private-" not in caplog.text


@pytest.mark.asyncio
async def test_shared_classification_deadline_does_not_blame_a_checkpoint_participant(
    dependencies: Dependencies,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BlockedClassificationOpenAI(FakeOpenAI):
        async def find_overlap_targets(self, **kwargs: object) -> tuple[ParticipantSlot, ...]:
            await asyncio.Event().wait()
            return ()

    openai = BlockedClassificationOpenAI()
    dependencies = (*dependencies[:5], openai, *dependencies[6:])
    app = make_application(dependencies, phase_timeout=0.1)
    accepted = await accept_bound_debate(app)

    with caplog.at_level(logging.WARNING, logger="shittim_chest"):
        await app.run_debate(accepted.debate_id)

    failed = dependencies[6].current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert failed.state.failed_from_phase is DebatePhase.SELECTING_CANDIDATES
    assert failed.error_code == "generation_deadline_exceeded"
    assert {plan.participant for plan in failed.candidate_plans} == set(PARTICIPANTS)
    assert failed.candidate_coordination is not None
    assert failed.candidate_coordination.step == "classify"
    checkpoint = failed.candidate_coordination.checkpoint
    assert checkpoint is not None
    assert checkpoint.participant in PARTICIPANTS
    assert not openai.initial_calls
    events = deadline_events(caplog)
    assert len(events) == 1
    assert events[0]["phase"] == DebatePhase.SELECTING_CANDIDATES.value
    assert events[0]["debate_id"] == str(accepted.debate_id)
    assert events[0]["participant"] is None
