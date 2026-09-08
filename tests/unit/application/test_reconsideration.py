"""Production entry point: sequential decisions, durable resume and private delivery."""

from collections import Counter
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from shittim_chest.adapters.dynamodb.serializer import deserialize_snapshot, serialize_snapshot
from shittim_chest.application import GenerationProviderError, RetryDebateCommand
from shittim_chest.domain import PARTICIPANTS, Candidate, CandidatePlan, DebatePhase, InitialOpinion
from tests.unit.application.test_service import (
    Dependencies,
    accept_bound_debate,
    make_application,
)
from tests.unit.application.test_service import dependencies as dependencies


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_step", [None, "classify", "explore", "select", "rewrite"])
async def test_v2_runs_before_delivery_and_resumes_each_saved_step(
    dependencies: Dependencies,
    monkeypatch,
    fail_step,
):
    app = make_application(dependencies)
    openai, repository = dependencies[5:7]
    calls = []
    failed_once = False
    seen = []
    original_replace = repository.replace

    async def persist(*, expected, updated):
        # Exercise the real persistence codec at every sub-step, not just completion.
        return await original_replace(
            expected=expected, updated=deserialize_snapshot(serialize_snapshot(updated))
        )

    monkeypatch.setattr(repository, "replace", persist)

    def mark(step):
        nonlocal failed_once
        calls.append(step)
        if step == fail_step and not failed_once:
            failed_once = True
            raise GenerationProviderError("openai_unavailable", "fixture", retryable=True)

    async def classify(**kwargs):
        mark("classify")
        return PARTICIPANTS[1:] if kwargs["coordination"] else PARTICIPANTS

    async def explore(**kwargs):
        mark("explore")
        seen.append((kwargs["plan"].participant, kwargs["peers"]))
        return (Candidate("new choice", "own appeal", "acceptable cost"),)

    async def select(**kwargs):
        mark("select")
        # Keep the same conclusion during reconsideration; still retain the rewrite.
        return CandidatePlan(kwargs["plan"].participant, (kwargs["alternatives"][0],))

    async def rewrite(**kwargs):
        mark("rewrite")
        current = next(iter(repository.current.values()))
        assert current.terminal_delivery is None
        assert current.state.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
        assert kwargs["evidence"] == current.evidence
        slot = kwargs["plan"].participant
        assert current.affection_assessment is not None
        assert kwargs["affection_score"] == current.affection_assessment.score_for(slot)
        return InitialOpinion(slot, f"revised-{slot}", kwargs["plan"].selected.proposal)

    monkeypatch.setattr(openai, "find_overlap_targets", classify)
    monkeypatch.setattr(openai, "explore_alternatives", explore)
    monkeypatch.setattr(openai, "select_alternative", select)
    monkeypatch.setattr(openai, "revise_initial_opinion", rewrite)
    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)
    if fail_step:
        failed = repository.current[accepted.debate_id]
        assert failed.state.phase is DebatePhase.FAILED
        await app.retry_debate(
            RetryDebateCommand(
                debate_id=accepted.debate_id,
                actor_id=failed.requester_id,
                operation_id="retry-v2",
            )
        )
        await app.run_debate(accepted.debate_id)
    completed = repository.current[accepted.debate_id]
    assert completed.state.phase is DebatePhase.COMPLETED
    assert completed.candidate_coordination is not None
    assert completed.opinion_reconsideration is not None
    assert (
        completed.candidate_coordination.step
        == completed.opinion_reconsideration.step
        == "complete"
    )
    assert Counter(calls) == {
        step: count + int(step == fail_step)
        for step, count in {"classify": 2, "explore": 5, "select": 5, "rewrite": 3}.items()
    }
    assert seen[1][1][1].proposal == "new choice"  # C sees B's updated candidate.
    assert seen[3][1][0].summary == "revised-participant-a"  # B sees A's rewrite.
    assert all(x.summary == f"revised-{x.participant}" for x in completed.initial_opinions)
    assert len(openai.initial_calls) == 3
    assert len(openai.proposal_calls) == 3
    assert deserialize_snapshot(serialize_snapshot(completed)) == completed
    assert all("own appeal" not in x.content for x in repository.terminal_operations.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 1])
async def test_legacy_attempts_never_add_a_v2_pass(
    dependencies: Dependencies, monkeypatch, version
):
    app = make_application(dependencies)
    openai, repository = dependencies[5:7]
    classify = AsyncMock()
    monkeypatch.setattr(openai, "find_overlap_targets", classify)
    accepted = await accept_bound_debate(app)
    repository.current[accepted.debate_id] = replace(
        repository.current[accepted.debate_id], deliberation_version=version
    )
    await app.run_debate(accepted.debate_id)
    assert repository.current[accepted.debate_id].state.phase is DebatePhase.COMPLETED
    classify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_crash_recovery_uses_successor_lease_and_bounds_calls(
    dependencies: Dependencies,
    monkeypatch,
    exhausted,
):
    app = make_application(dependencies)
    openai, repository = dependencies[5:7]
    captured = []

    async def interrupted(**kwargs):
        captured.append(next(iter(repository.current.values())))
        raise GenerationProviderError("openai_unavailable", "fixture", retryable=True)

    monkeypatch.setattr(openai, "find_overlap_targets", interrupted)
    accepted = await accept_bound_debate(app)
    await app.run_debate(accepted.debate_id)
    # Restore the durable IN_FLIGHT state a successor would see after a hard process exit.
    before_exit = captured[0]
    assert before_exit.lease is not None
    progress = before_exit.candidate_coordination
    assert progress is not None and progress.checkpoint is not None
    checkpoint = replace(progress.checkpoint, logical_attempt=2 if exhausted else 1)
    recovered = replace(
        before_exit,
        lease=replace(before_exit.lease, fencing_token=before_exit.lease.fencing_token + 1),
        candidate_coordination=replace(progress, checkpoint=checkpoint),
    )
    repository.current[accepted.debate_id] = recovered
    # A hard exit has not staged the simulated failure notice.
    repository.terminal_operations.clear()
    classify = AsyncMock(return_value=())
    monkeypatch.setattr(openai, "find_overlap_targets", classify)
    result = await app._run_reconsideration(recovered, coordination=True)
    if exhausted:
        assert result is None
        assert repository.current[accepted.debate_id].error_code == "generation_attempts_exhausted"
        classify.assert_not_awaited()
    else:
        assert result is not None and result.candidate_coordination is not None
        assert result.candidate_coordination.step == "complete"
        classify.assert_awaited_once()
        assert len(openai.candidate_calls) == 3
