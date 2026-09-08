"""Exercise the real experiment's forwarding and anonymous order reversal."""

from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from tools import evaluate_speech_quality as experiment

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.domain import (
    PARTICIPANTS,
    Candidate,
    CandidatePlan,
    FinalProposal,
    InitialOpinion,
    PreferenceFrame,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prepared", "direct", "spontaneous", "shared"])
async def test_speech_forwards_own_plan_without_inventing_a_frame(mode):
    plans = [CandidatePlan(slot, (Candidate("choice", "fit", "cost"),)) for slot in PARTICIPANTS]
    frames = [PreferenceFrame(slot, ("priority",), (), "condition") for slot in PARTICIPANTS]
    service = SimpleNamespace(
        form_preferences=AsyncMock(side_effect=frames),
        select_candidates=AsyncMock(side_effect=plans),
        generate_initial_opinion=AsyncMock(
            side_effect=[InitialOpinion(slot, "choice", "choice") for slot in PARTICIPANTS]
        ),
        generate_final_proposal=AsyncMock(
            side_effect=[FinalProposal(slot, "choice", "choice") for slot in PARTICIPANTS]
        ),
    )
    result = await experiment.generate(
        cast(OpenAIResponsesService, service),
        "fixture",
        direct=mode != "prepared",
        spontaneous=mode == "spontaneous",
        prepared=(tuple(frames), tuple(plans)) if mode == "shared" else None,
    )
    assert service.form_preferences.await_count == (3 if mode == "prepared" else 0)
    assert service.select_candidates.await_count == (0 if mode in ("spontaneous", "shared") else 3)
    for method in (service.generate_initial_opinion, service.generate_final_proposal):
        assert method.await_count == 3
        for index, call in enumerate(method.await_args_list):
            assert call.kwargs["candidate_plan"] is (
                None if mode == "spontaneous" else plans[index]
            )
            assert call.kwargs["preference_frame"] == (
                frames[index] if mode in ("prepared", "shared") else None
            )
    assert set(result) == {"initial", "final"}


@pytest.mark.asyncio
@pytest.mark.parametrize("flip_assessment", [False, True])
@pytest.mark.parametrize("released", [False, True])
async def test_judgment_reverses_same_outputs_and_does_not_certify_quality(
    monkeypatch, flip_assessment, released
):
    assessment = experiment.Assessment(
        answers_request="yes",
        persona_contradiction="absent",
        positions="shared_but_personal",
        final_preserves_personal_stances="yes",
        forced_disagreement="absent",
    )
    positions = tuple(
        experiment.PublicPosition(participant=slot, initial="summary", final="summary")
        for slot in PARTICIPANTS
    )
    judgments = [
        experiment.Judgment(
            left=assessment,
            right=assessment,
            preferred=side,
            basis="persona_expression",
            left_positions=positions,
            right_positions=positions,
        )
        for side in ("right", "left")
    ]
    if flip_assessment:
        judgments[1] = judgments[1].model_copy(
            update={"left": assessment.model_copy(update={"persona_contradiction": "present"})}
        )
    service = SimpleNamespace(
        profiles=SimpleNamespace(
            for_participant=lambda _: SimpleNamespace(system_prompt="private persona")
        ),
        _parse=AsyncMock(side_effect=judgments),
    )
    generate = AsyncMock(side_effect=[{"branch": "prepared"}, {"branch": "direct"}])
    monkeypatch.setattr(experiment, "generate", generate)
    legacy = ModuleType("fixture") if released else None
    old_generate = AsyncMock(return_value={"branch": "released"})
    monkeypatch.setattr(experiment, "generate_speech", old_generate)
    result = await experiment.compare(
        cast(OpenAIResponsesService, service),
        experiment.Case(label="fixture", question="private question"),
        0,
        baseline=legacy,
    )
    assert generate.await_count == (1 if released else 2)
    assert old_generate.await_count == int(released)
    if released:
        assert old_generate.await_args is not None
        assert old_generate.await_args.args[2] is legacy
    variant = "released" if released else "direct"
    assert result["preference_agrees"] is True
    assert result["assessment_disagreements"] == (
        [f"{variant}.persona_contradiction"] if flip_assessment else []
    )
    assert result["stable_observation"] is (not flip_assessment)
    assert result["quality_certified"] is False
    observations = cast(list[dict[str, object]], result["observations"])
    assert all(item["preferred"] == variant for item in observations)
    assert "private" not in str(result)
