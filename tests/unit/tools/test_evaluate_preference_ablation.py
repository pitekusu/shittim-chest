"""Keep the selection ablation paired, bounded and free of private output."""

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from tools import evaluate_preference_ablation as experiment

from shittim_chest.adapters.openai import (
    OpenAIResponsesService,
    ParticipantProfile,
    ParticipantProfiles,
)
from shittim_chest.application.generation_policy import PhaseBudget, ReasoningEffort
from shittim_chest.domain import PARTICIPANTS, PreferenceFrame


def test_selection_must_cover_all_candidates_once():
    experiment.validate_selection(experiment.Selection(ranked_ids=(2, 0, 1)), 3)
    for ids in ((0, 0, 2), (0, 1, 3), (0, 1, 2, 3)):
        with pytest.raises(ValueError, match="coverage"):
            experiment.validate_selection(experiment.Selection(ranked_ids=ids), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("trial", [0, 1])
async def test_only_prior_frame_differs_and_private_text_is_not_returned(trial):
    frames = [
        PreferenceFrame(slot, ("private priority",), (), "private compromise")
        for slot in PARTICIPANTS
    ]
    profiles = ParticipantProfiles(
        {
            slot: ParticipantProfile(f"name-{index}", f"private persona {index}")
            for index, slot in enumerate(PARTICIPANTS)
        }
    )
    observed = experiment.Observation(
        participants=tuple(
            experiment.PersonaFit(participant=slot, left="supported", right="supported")
            for slot in PARTICIPANTS
        )
    )

    async def parse(**kwargs):
        if kwargs["operation"] == "frame_ablation_observe":
            return observed
        return experiment.Selection(ranked_ids=(1, 2, 0))

    service = SimpleNamespace(
        profiles=profiles,
        system_prompt="private system",
        config=SimpleNamespace(
            policy=SimpleNamespace(candidates=PhaseBudget(ReasoningEffort.MEDIUM, 4_000))
        ),
        form_preferences=AsyncMock(side_effect=frames),
        _parse=AsyncMock(side_effect=parse),
    )
    case = experiment.Case(
        topic="work", question="private question", options=("first", "second", "third")
    )
    result = await experiment.compare(cast(OpenAIResponsesService, service), case, trial, 0)
    assert service.form_preferences.await_count == 3
    assert service._parse.await_count == 7
    calls = service._parse.await_args_list
    for first, second in zip(calls[:3], calls[3:6], strict=True):
        assert first.kwargs["instructions"] == second.kwargs["instructions"]
        assert first.kwargs["settings"] == second.kwargs["settings"]
        payloads = [json.loads(call.kwargs["input_text"]) for call in (first, second)]
        assert sum("private_decision_data" in item for item in payloads) == 1
        for item in payloads:
            item.pop("private_decision_data", None)
        assert payloads[0] == payloads[1]
    assert result["branch_order"] == (
        ("direct", "prepared") if trial == 0 else ("prepared", "direct")
    )
    assert result["option_order"] == ((0, 1, 2) if trial == 0 else (2, 1, 0))
    assert result["distinct_first_choices"] == {"direct": 1, "prepared": 1}
    assert "private" not in str(result)
    assert "second" not in json.dumps(result["ranked_ids"])
