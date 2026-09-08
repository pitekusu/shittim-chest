"""New choices, genuine agreement, sequential peers and private human-readable output."""

import json
from itertools import combinations
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tools import compare_for_humans as report
from tools import reconsideration_v2 as revised
from tools.evaluate_overlap import Overlap, Pair

from shittim_chest.adapters.openai import OpenAIResponsesService
from shittim_chest.adapters.openai.schemas import CandidateOutputV1, OpinionOutputV1
from shittim_chest.domain import (
    PARTICIPANTS,
    Candidate,
    CandidatePlan,
    FinalProposal,
    InitialOpinion,
    PreferenceFrame,
)


def test_new_candidate_can_replace_original_but_keep_is_valid():
    plan = CandidatePlan(PARTICIPANTS[0], (Candidate("original", "fit", "cost"),))
    exploration = revised.Exploration(
        alternatives=(CandidateOutputV1(proposal="new", fit="fit", tradeoff="cost"),)
    )
    assert (
        revised.choose_plan(
            plan, exploration, revised.Selection(selected_index=1)
        ).selected.proposal
        == "new"
    )
    assert (
        revised.choose_plan(plan, exploration, revised.Selection(selected_index=0)).selected
        == plan.selected
    )
    with pytest.raises(ValueError, match="selection"):
        revised.choose_plan(plan, exploration, revised.Selection(selected_index=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["open_choice", "fixed_answer", "uncertain"])
async def test_sequential_rethink_and_unchanged_conclusion_rewrite(monkeypatch, kind):
    frames = [PreferenceFrame(x, ("priority",), (), "cost") for x in PARTICIPANTS]
    plans = [CandidatePlan(x, (Candidate("same", "fit", "cost"),)) for x in PARTICIPANTS]
    initials = [InitialOpinion(x, "original", "same") for x in PARTICIPANTS]
    overlap = Overlap(
        request_kind=kind,
        pairs=tuple(
            Pair(left=a, right=b, relation="shared_conclusion_distinct_position")
            for a, b in combinations(PARTICIPANTS, 2)
        ),
    )
    rewrite_outputs = [
        OpinionOutputV1(summary=f"personal-{x}", proposal="same") for x in PARTICIPANTS
    ]
    service = SimpleNamespace(
        form_preferences=AsyncMock(side_effect=frames),
        select_candidates=AsyncMock(side_effect=plans),
        generate_initial_opinion=AsyncMock(side_effect=initials),
        generate_final_proposal=AsyncMock(
            side_effect=[FinalProposal(x, "title", "final") for x in PARTICIPANTS]
        ),
        _parse=AsyncMock(side_effect=rewrite_outputs),
        config=SimpleNamespace(initial_opinion=None),
    )
    seen = []

    async def explore(service, question, frame, plan, peers):
        seen.append(peers)
        return plan

    monkeypatch.setattr(revised, "classify", AsyncMock(return_value=overlap))
    monkeypatch.setattr(revised, "explore_and_select", explore)
    monkeypatch.setattr(revised, "participant_instructions", lambda *args, **kwargs: "identity")
    service.profiles = None
    service.system_prompt = None
    outputs, meta = await revised.generate(cast(OpenAIResponsesService, service), "fixture", 0)
    if kind != "open_choice":
        assert not seen
        service._parse.assert_not_awaited()
        assert not meta["rewritten"]
    else:
        assert len(seen) == 5  # Two initial coordinations and three shared-conclusion revisions.
        assert meta["reconsideration_changed"] == []
        assert meta["rewritten"] == list(PARTICIPANTS)
        assert seen[3][0]["summary"] == "personal-participant-a"
        initial = cast(list[dict[str, object]], outputs["initial"])
        assert initial[0]["summary"] == "personal-participant-a"
        final_call = service.generate_final_proposal.await_args_list[0]
        assert final_call.kwargs["initial_opinions"][0].summary == "personal-participant-a"


def test_ten_distinct_records_reproducible_without_topic_selection():
    catalog = [{"PK": {"S": str(i)}} for i in range(30)]
    picked = report.select_ten(catalog, 45)
    assert len(picked) == len({x["PK"]["S"] for x in picked}) == 10
    assert picked == report.select_ten(list(reversed(catalog)), 45)


def test_full_report_escapes_untrusted_text_and_checkpoints(tmp_path):
    speech = "<script>untrusted</script>\n" + "full text " * 400
    data: dict[str, Any] = {
        "state": "test",
        "completed": 1,
        "model": "fixture",
        "baseline_commit": "fixture",
        "cases": [
            {
                "case": 1,
                "date": "2026-09-08",
                "question": speech,
                "seconds": {},
                "runs": {
                    "released": {
                        "initial": [
                            {
                                "participant": "participant-a",
                                "summary": "summary",
                                "proposal": speech,
                            }
                        ]
                    }
                },
            }
        ],
    }
    report.save_report(tmp_path, data)
    rendered = (tmp_path / "comparison.html").read_text()
    assert "<script>" not in rendered
    assert "&lt;script&gt;untrusted&lt;/script&gt;" in rendered
    assert rendered.count("full text " * 400) == 2  # Both question and speech are complete.
    assert json.loads((tmp_path / "answers.json").read_text()) == data
    assert (tmp_path / "answers.json").stat().st_mode & 0o777 == 0o600
    data["state"] = "complete"
    report.save_report(tmp_path, data)
    assert json.loads((tmp_path / "answers.json").read_text())["state"] == "complete"
    assert not list(tmp_path.glob("*.pending"))
