"""Bound reconsideration; retain genuine agreement and forward revised choices."""

import json
from itertools import combinations
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from tools import evaluate_overlap as experiment

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.adapters.openai.schemas import OpinionOutputV1
from shittim_chest.domain import (
    PARTICIPANTS,
    Candidate,
    CandidatePlan,
    FinalProposal,
    InitialOpinion,
    PreferenceFrame,
)


def pairs(first="duplicate"):
    return tuple(
        experiment.Pair(left=left, right=right, relation=first if index == 0 else "different")
        for index, (left, right) in enumerate(combinations(PARTICIPANTS, 2))
    )


@pytest.mark.parametrize(
    "kind,relation,include_shared,expected",
    [
        ("open_choice", "duplicate", False, PARTICIPANTS[:2]),
        ("open_advice", "duplicate", False, PARTICIPANTS[:2]),
        ("fixed_answer", "duplicate", True, ()),
        ("open_advice", "shared_conclusion_distinct_position", False, ()),
        ("open_advice", "shared_conclusion_distinct_position", True, PARTICIPANTS[:2]),
        ("fixed_answer", "shared_conclusion_distinct_position", True, ()),
        ("uncertain", "shared_conclusion_distinct_position", True, ()),
        ("open_advice", "uncertain", True, ()),
    ],
)
def test_reconsider_only_matching_open_choices(kind, relation, include_shared, expected):
    result = experiment.Overlap(request_kind=kind, pairs=pairs(relation))
    assert (
        experiment.reconsider_targets(result, include_shared_conclusions=include_shared) == expected
    )


def test_duplicate_or_self_pairs_are_not_accepted():
    pair = pairs()[0]
    with pytest.raises(ValueError, match="coverage"):
        experiment.reconsider_targets(
            experiment.Overlap(request_kind="open_choice", pairs=(pair,) * 3)
        )


def test_keep_does_not_rewrite_original_and_invalid_candidate_is_rejected():
    slot = PARTICIPANTS[0]
    plan = CandidatePlan(slot, (Candidate("original", "fit", "cost"),))
    original = InitialOpinion(slot, "original", "original")
    opinion = OpinionOutputV1(summary="replacement", proposal="replacement")
    kept = experiment.adopt_reconsideration(
        experiment.Reconsidered(selected_index=0, opinion=opinion), plan, original
    )
    assert kept[0] is plan and kept[1] is original
    with pytest.raises(ValueError, match="candidate"):
        experiment.adopt_reconsideration(
            experiment.Reconsidered(selected_index=1, opinion=opinion), plan, original
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["open_advice", "fixed_answer"])
async def test_branch_reuses_preparation_and_forwards_revised_plan(monkeypatch, kind):
    frames = [PreferenceFrame(slot, ("priority",), (), "condition") for slot in PARTICIPANTS]
    plans = [
        CandidatePlan(
            slot, (Candidate("original", "fit", "cost"), Candidate("alternative", "fit", "cost"))
        )
        for slot in PARTICIPANTS
    ]
    opinions = [InitialOpinion(slot, "original", "original") for slot in PARTICIPANTS]
    choices = tuple(
        experiment.Choices(
            participant=slot,
            initial_choice="original",
            final_choice="original",
            answers_request="yes",
        )
        for slot in PARTICIPANTS
    )
    report = experiment.Report(
        left=choices, right=choices, left_final_pairs=pairs(), right_final_pairs=pairs()
    )
    diagnosis = experiment.Diagnosis(
        participants=tuple(
            experiment.StageDiagnosis(
                participant=slot,
                candidate_options="materially_different",
                initial_follows_selected="yes",
                reconsidered_selection_vs_original="different",
                reconsidered_speech_follows_selected="yes",
                final_follows_reconsidered_selection="yes",
            )
            for slot in PARTICIPANTS
        )
    )
    service = SimpleNamespace(
        form_preferences=AsyncMock(side_effect=frames),
        select_candidates=AsyncMock(side_effect=plans),
        generate_initial_opinion=AsyncMock(side_effect=opinions),
        generate_final_proposal=AsyncMock(
            side_effect=[
                FinalProposal(slot, "title", "body") for slot in (*PARTICIPANTS, *PARTICIPANTS)
            ]
        ),
        _parse=AsyncMock(
            side_effect=[experiment.Overlap(request_kind=kind, pairs=pairs()), report, diagnosis]
        ),
    )

    async def rethink(service, question, frame, plan, original_opinions):
        original = next(item for item in original_opinions if item.participant is plan.participant)
        return experiment.adopt_reconsideration(
            experiment.Reconsidered(
                selected_index=1,
                opinion=OpinionOutputV1(summary="alternative", proposal="alternative"),
            ),
            plan,
            original,
        )

    reconsider = AsyncMock(side_effect=rethink)
    monkeypatch.setattr(experiment, "reconsider", reconsider)
    result = await experiment.compare_case(
        cast(OpenAIResponsesService, service), "private fixture", 0, diagnose=kind == "open_advice"
    )
    assert service.form_preferences.await_count == service.select_candidates.await_count == 3
    assert service.generate_initial_opinion.await_count == 3
    if kind == "fixed_answer":
        reconsider.assert_not_awaited()
        assert service.generate_final_proposal.await_count == 3
        assert result["final_reused_without_change"] is True
        assert result["candidate_index_changed_participants"] == []
    else:
        assert reconsider.await_count == 2
        assert result["candidate_index_changed_participants"] == list(PARTICIPANTS[:2])
        calls = service.generate_final_proposal.await_args_list
        assert len(calls) == 6
        for call in calls[3:5]:
            assert call.kwargs["candidate_plan"].selected.proposal == "alternative"
            own = next(
                item
                for item in call.kwargs["initial_opinions"]
                if item.participant is call.kwargs["participant"]
            )
            assert own.proposal == "alternative"
        assert calls[5].kwargs["candidate_plan"] is plans[2]
        assert result["stage_diagnosis"] == diagnosis.model_dump()
        diagnostic_input = json.loads(service._parse.await_args.kwargs["input_text"])
        assert "priority" not in str(diagnostic_input)
        assert "fit" not in str(diagnostic_input)
        assert "private fixture" in diagnostic_input["question"]
    assert "private fixture" not in str(result)
