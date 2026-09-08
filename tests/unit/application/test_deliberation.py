"""Private decision preparation, recovery and the absolute attempt deadline."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from shittim_chest.adapters.dynamodb.serializer import deserialize_snapshot, serialize_snapshot
from shittim_chest.application import GenerationProviderError, RetryDebateCommand
from shittim_chest.application.models import DebateSnapshot
from shittim_chest.domain import PARTICIPANTS, DebatePhase, ParticipantSlot
from tests.unit.application.fakes import FakeOutboxRecovery
from tests.unit.application.test_service import (
    Dependencies,
    accept_bound_debate,
    make_application,
)
from tests.unit.application.test_service import (
    dependencies as dependencies,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preference", "candidate"])
async def test_private_results_survive_retry_without_reselecting_successes(
    dependencies: Dependencies,
    stage: str,
) -> None:
    app = make_application(dependencies)
    openai, repository = dependencies[5:7]
    errors = openai.preference_errors if stage == "preference" else openai.candidate_errors
    errors[ParticipantSlot.PARTICIPANT_B] = GenerationProviderError(
        "openai_unavailable",
        "unavailable",
        retryable=True,
    )
    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)
    failed = repository.current[accepted.debate_id]
    assert failed.state.phase is DebatePhase.FAILED
    assert len(failed.preference_frames if stage == "preference" else failed.candidate_plans) == 2
    errors.clear()
    await app.retry_debate(
        RetryDebateCommand(
            debate_id=accepted.debate_id,
            actor_id=failed.requester_id,
            operation_id="retry-private",
        )
    )
    await app.run_debate(accepted.debate_id)
    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    calls = openai.preference_calls if stage == "preference" else openai.candidate_calls
    assert Counter(calls) == {
        slot: (2 if slot is ParticipantSlot.PARTICIPANT_B else 1) for slot in PARTICIPANTS
    }
    assert len(completed.preference_frames) == len(completed.candidate_plans) == 3
    # Agreement and a single feasible option are allowed, not rerolled for diversity.
    assert {plan.selected.proposal for plan in completed.candidate_plans} == {"proposal"}
    assert deserialize_snapshot(serialize_snapshot(completed)) == completed
    assert all(
        "compromise" not in operation.content
        for operation in repository.terminal_operations.values()
    )


@pytest.mark.asyncio
async def test_legacy_attempt_does_not_retroactively_generate_preferences(
    dependencies: Dependencies,
) -> None:
    app = make_application(dependencies)
    openai, repository = dependencies[5:7]
    accepted = await accept_bound_debate(app)
    repository.current[accepted.debate_id] = replace(
        repository.current[accepted.debate_id], deliberation_version=0
    )
    await app.run_debate(accepted.debate_id)
    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    assert not openai.preference_calls and not openai.candidate_calls


@pytest.mark.asyncio
async def test_phase_delivery_does_not_restart_generation_deadline(
    dependencies: Dependencies,
) -> None:
    clock = dependencies[0]

    class SlowPhaseDelivery(FakeOutboxRecovery):
        async def drain(self, *, expected: DebateSnapshot) -> None:
            await super().drain(expected=expected)
            if expected.state.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS:
                clock.current += timedelta(seconds=1)

    app = make_application(dependencies, outbox_recovery=SlowPhaseDelivery(), session_timeout=0.5)
    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)
    assert not dependencies[5].proposal_calls
    assert dependencies[6].current[accepted.debate_id].error_code == "session_deadline_exceeded"
