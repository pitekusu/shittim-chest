"""V2 request boundaries and routing through the real Responses SDK."""

import json
from itertools import combinations

import pytest

from shittim_chest.adapters.openai.errors import OpenAIInvalidOutput
from shittim_chest.adapters.openai.prompts import affection_response_instructions
from shittim_chest.adapters.openai.reconsideration import Overlap, Pair, route_targets
from shittim_chest.domain import (
    PARTICIPANTS,
    Candidate,
    CandidatePlan,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSearchStatus,
    InitialOpinion,
    PreferenceFrame,
    SearchRequirement,
)
from tests.contract.openai.test_responses_service import response_with, service_for


@pytest.mark.parametrize(
    "kind", ["open_choice", "open_advice", "fixed_answer", "other", "uncertain"]
)
@pytest.mark.parametrize("rotation", [0, 1, 2])
def test_routing_preserves_anchor_only_for_open_requests(kind, rotation):
    mapping = Overlap(
        request_kind=kind,
        pairs=tuple(
            Pair(left=a, right=b, relation="shared_conclusion_distinct_position")
            for a, b in combinations(PARTICIPANTS, 2)
        ),
    )
    order = PARTICIPANTS[rotation:] + PARTICIPANTS[:rotation]
    assert route_targets(mapping, rotation, True) == (order[1:] if kind.startswith("open_") else ())
    assert route_targets(mapping, rotation, False) == (order if kind.startswith("open_") else ())


def test_incomplete_pair_coverage_cannot_route():
    pair = Pair(left=PARTICIPANTS[0], right=PARTICIPANTS[1], relation="duplicate")
    with pytest.raises(OpenAIInvalidOutput):
        route_targets(Overlap(request_kind="open_choice", pairs=(pair, pair, pair)), 0, True)


@pytest.mark.asyncio
async def test_real_requests_keep_original_and_rewrite_with_actual_affection_and_evidence():
    service, server, observer, client = await service_for(
        [
            response_with(
                {
                    "request_kind": "open_choice",
                    "pairs": [
                        {"left": a.value, "right": b.value, "relation": "duplicate"}
                        for a, b in combinations(PARTICIPANTS, 2)
                    ],
                }
            ),
            response_with(
                {"alternatives": [{"proposal": "new", "fit": "own appeal", "tradeoff": "cost"}]}
            ),
            response_with({"selected_index": 0}),
            response_with({"summary": "own perspective", "proposal": "same conclusion"}),
        ]
    )
    a = PARTICIPANTS[0]
    frame = PreferenceFrame(a, ("priority",), (), "condition")
    plan = CandidatePlan(a, (Candidate("original", "own fit", "own cost"),))
    opinions = tuple(InitialOpinion(x, "public summary", "public proposal") for x in PARTICIPANTS)
    evidence = EvidenceBundle(
        summary="fixture evidence",
        items=(EvidenceItem("https://example.com", "fixture", "fixture", "2026-09-08", "fixture"),),
        search_requirement=SearchRequirement.OPTIONAL,
        search_status=EvidenceSearchStatus.COMPLETED,
        search_response_id="fixture",
    )
    try:
        assert (
            await service.find_overlap_targets(
                question="fixture",
                positions=opinions,
                rotation=0,
                coordination=True,
            )
            == PARTICIPANTS[1:]
        )
        alternatives = await service.explore_alternatives(
            question="fixture",
            frame=frame,
            plan=plan,
            peers=opinions[1:],
            evidence=evidence,
        )
        selected = await service.select_alternative(
            question="fixture",
            frame=frame,
            plan=plan,
            peers=opinions[1:],
            evidence=evidence,
            alternatives=alternatives,
        )
        assert selected.selected == plan.selected
        revised = await service.revise_initial_opinion(
            question="fixture",
            frame=frame,
            plan=selected,
            opinions=opinions,
            evidence=evidence,
            affection_score=123,
        )
        assert revised.summary == "own perspective"
        for request in server.requests:
            assert request["store"] is False
            assert request["tools"] == []
        for request in server.requests[1:]:
            assert service.profiles.for_participant(a).system_prompt in request["instructions"]
            assert all(
                service.profiles.for_participant(x).system_prompt not in request["instructions"]
                for x in PARTICIPANTS[1:]
            )
            assert json.loads(request["input"])["evidence"]["summary"] == "fixture evidence"
        assert affection_response_instructions(123) in server.requests[-1]["instructions"]
        payload = json.loads(server.requests[-1]["input"])
        assert payload["private_decision_data"]["selected_candidate"]["proposal"] == "original"
        assert {x["participant"] for x in payload["peers"]} == {x.value for x in PARTICIPANTS[1:]}
        assert len(observer.usages) == 4
    finally:
        await client.aclose()
