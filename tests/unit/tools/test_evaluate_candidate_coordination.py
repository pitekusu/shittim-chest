import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tools.evaluate_candidate_coordination import (
    Alternatives,
    choose_indices,
    coordinate,
    group_schema,
)

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.domain import PARTICIPANTS, Candidate, CandidatePlan, PreferenceFrame


def test_selection_uses_only_approved_options_and_avoids_unnecessary_changes():
    groups = ((0, 1), (0, 2), (0, 3))
    assert choose_indices(groups, ((0,), (0, 1), (0,)), 0) == (0, 1, 0)
    assert choose_indices(((0, 1), (1, 0), (2, 0)), ((0, 1),) * 3, 0) == (0, 0, 0)
    with pytest.raises(ValueError):
        choose_indices(groups, ((0, 2), (0,), (0,)), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fixed_answer", "uncertain", "other"])
async def test_ineligible_request_never_asks_for_alternatives(kind):
    frames = tuple(PreferenceFrame(slot, ("priority",), (), "condition") for slot in PARTICIPANTS)
    plans = tuple(
        CandidatePlan(slot, (Candidate("proposal", "private fit", "cost"),))
        for slot in PARTICIPANTS
    )
    parse = AsyncMock(
        return_value=group_schema((1, 1, 1))(
            request_kind=kind,
            p0_c0=0,
            p1_c0=0,
            p2_c0=0,
        )
    )
    result, metadata = await coordinate(
        cast(OpenAIResponsesService, SimpleNamespace(_parse=parse)), "fixture", frames, plans, 0
    )
    assert result is plans
    assert metadata["changed_participants"] == []
    assert parse.await_count == 1
    assert "private fit" not in parse.await_args.kwargs["input_text"]


def test_group_contract_requires_exact_supplied_candidate_fields():
    model = group_schema((1, 2, 3))
    data = {
        "request_kind": "open_choice",
        "p0_c0": 0,
        "p1_c0": 1,
        "p1_c1": 2,
        "p2_c0": 3,
        "p2_c1": 4,
        "p2_c2": 5,
    }
    assert model.model_validate(data).model_dump() == data
    schema = model.model_json_schema()
    assert set(schema["required"]) == set(data)
    assert schema["additionalProperties"] is False
    for invalid in (
        {k: v for k, v in data.items() if k != "p2_c2"},
        {**data, "p0_c1": 0},
        {**data, "p0_c0": 9},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(invalid)


@pytest.mark.asyncio
async def test_only_independently_approved_own_option_is_moved_to_front():
    frames = tuple(PreferenceFrame(slot, ("priority",), (), "condition") for slot in PARTICIPANTS)
    plans = tuple(
        CandidatePlan(
            slot, (Candidate(f"first-{i}", "fit", "cost"), Candidate(f"second-{i}", "fit", "cost"))
        )
        for i, slot in enumerate(PARTICIPANTS)
    )

    async def parse(**kwargs):
        if kwargs["operation"] == "group_candidate_positions":
            return kwargs["schema"](
                request_kind="open_choice", p0_c0=0, p0_c1=1, p1_c0=0, p1_c1=2, p2_c0=0, p2_c1=3
            )
        payload = json.loads(kwargs["input_text"])
        assert len(payload["options"]) == 2
        own = payload["options"][0]["proposal"][-1]
        assert all(item["proposal"].endswith(own) for item in payload["options"])
        return Alternatives(alternative_indices=(1,) if own == "1" else ())

    service = SimpleNamespace(
        _parse=AsyncMock(side_effect=parse),
        system_prompt=None,
        profiles=SimpleNamespace(
            for_participant=lambda _: SimpleNamespace(system_prompt="persona")
        ),
    )
    result, metadata = await coordinate(
        cast(OpenAIResponsesService, service), "fixture", frames, plans, 0
    )
    assert result[0] is plans[0] and result[2] is plans[2]
    assert result[1].selected is plans[1].candidates[1]
    assert metadata["groups_after"] == 2
    assert metadata["changed_participants"] == [PARTICIPANTS[1]]
