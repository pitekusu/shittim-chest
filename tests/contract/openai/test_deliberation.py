"""Real SDK request isolation for the private preparation and public speech."""

import json

import pytest

from shittim_chest.adapters.openai import OpenAIIncompleteResponse
from shittim_chest.adapters.openai.prompts import PERSONAL_CHOICE_RULES, final_proposal_input
from shittim_chest.domain import (
    PARTICIPANTS,
    EvidenceBundle,
    FinalProposal,
    InitialOpinion,
    PreferenceFrame,
)
from tests.contract.openai.test_responses_service import response_with, service_for


def test_final_separates_own_speech_without_private_preparation() -> None:
    a, b, c = PARTICIPANTS
    opinions = tuple(InitialOpinion(slot, "summary", "proposal") for slot in (a, b, c))
    payload = json.loads(final_proposal_input("fixture", EvidenceBundle(), opinions, participant=b))
    assert payload["own_initial_opinion"]["participant"] == b
    assert {item["participant"] for item in payload["other_initial_opinions"]} == {a, c}
    assert "private_decision_data" not in payload
    with pytest.raises(ValueError, match="exactly one"):
        final_proposal_input("fixture", EvidenceBundle(), (opinions[0],) * 3, participant=b)


@pytest.mark.asyncio
async def test_private_selection_is_carried_forward_without_peer_profiles() -> None:
    service, server, observer, client = await service_for(
        [
            response_with(
                {
                    "priorities": ["quiet"],
                    "avoidances": ["crowds"],
                    "compromise_condition": "short visit",
                }
            ),
            response_with(
                {
                    "candidates": [
                        {"proposal": "park", "fit": "quiet", "tradeoff": "weather"},
                        {"proposal": "museum", "fit": "calm", "tradeoff": "crowds"},
                    ]
                }
            ),
            response_with({"summary": "park", "proposal": "walk"}),
            response_with({"title": "park", "proposal": "short walk"}),
            response_with(
                {
                    "candidate_id": "participant-b",
                    "accuracy_score": 3,
                    "usefulness_score": 4,
                    "safety_score": 5,
                    "reason": "quiet",
                }
            ),
        ]
    )
    a, b, c = PARTICIPANTS
    evidence = EvidenceBundle()
    try:
        frame = await service.form_preferences(participant=a, question="weekend")
        plan = await service.select_candidates(
            participant=a, question="weekend", evidence=evidence, preference_frame=frame
        )
        initial = await service.generate_initial_opinion(
            participant=a,
            question="weekend",
            evidence=evidence,
            preference_frame=frame,
            candidate_plan=plan,
        )
        await service.generate_final_proposal(
            participant=a,
            question="weekend",
            evidence=evidence,
            initial_opinions=(
                initial,
                InitialOpinion(b, "other-b", "cafe"),
                InitialOpinion(c, "other-c", "mall"),
            ),
            preference_frame=frame,
            candidate_plan=plan,
        )
        await service.cast_vote(
            voter=a,
            question="weekend",
            evidence=evidence,
            candidates=(FinalProposal(b, "cafe", "cafe"), FinalProposal(c, "mall", "mall")),
            preference_frame=frame,
        )
    finally:
        await client.aclose()
    assert plan.selected.proposal == "park"
    assert json.loads(server.requests[0]["input"]) == {
        "task": "form_preferences",
        "question": "weekend",
    }
    for request in server.requests:
        assert request["store"] is False
        assert request["tools"] == []
        assert "persona for participant-b" not in request["instructions"]
        assert "persona for participant-c" not in request["instructions"]
    assert [request["max_output_tokens"] for request in server.requests[:2]] == [2_000, 4_000]
    assert all(PERSONAL_CHOICE_RULES in request["instructions"] for request in server.requests[:4])
    initial_input = json.loads(server.requests[2]["input"])
    assert initial_input["private_decision_data"]["selected_candidate"]["proposal"] == "park"
    final_input = json.loads(server.requests[3]["input"])
    assert final_input["own_initial_opinion"]["participant"] == a
    assert {item["participant"] for item in final_input["other_initial_opinions"]} == {b, c}
    vote_input = json.loads(server.requests[4]["input"])
    assert "selected_candidate" not in vote_input["private_decision_data"]
    assert len(observer.usages) == 5


@pytest.mark.asyncio
async def test_direct_candidates_reach_speech_and_reject_foreign_owner() -> None:
    service, server, observer, client = await service_for(
        [
            response_with(
                {"candidates": [{"proposal": "park", "fit": "quiet", "tradeoff": "rain"}]}
            ),
            response_with({"summary": "park", "proposal": "walk"}),
            response_with({"title": "park", "proposal": "short walk"}),
        ]
    )
    a, b, c = PARTICIPANTS
    try:
        plan = await service.select_candidates(
            participant=a, question="weekend", evidence=EvidenceBundle()
        )
        with pytest.raises(ValueError, match="candidate plan belongs"):
            await service.generate_initial_opinion(
                participant=b, question="weekend", evidence=EvidenceBundle(), candidate_plan=plan
            )
        initial = await service.generate_initial_opinion(
            participant=a, question="weekend", evidence=EvidenceBundle(), candidate_plan=plan
        )
        await service.generate_final_proposal(
            participant=a,
            question="weekend",
            evidence=EvidenceBundle(),
            candidate_plan=plan,
            initial_opinions=(
                initial,
                InitialOpinion(b, "other", "cafe"),
                InitialOpinion(c, "other", "mall"),
            ),
        )
    finally:
        await client.aclose()
    assert len(server.requests) == len(observer.usages) == 3
    assert "private_decision_data" not in json.loads(server.requests[0]["input"])
    for request in server.requests[1:]:
        decision = json.loads(request["input"])["private_decision_data"]
        assert set(decision) == {"selected_candidate"}
        assert decision["selected_candidate"]["proposal"] == "park"
        assert request["store"] is False
    final = json.loads(server.requests[2]["input"])
    assert final["own_initial_opinion"]["participant"] == a
    assert {item["participant"] for item in final["other_initial_opinions"]} == {b, c}


@pytest.mark.asyncio
@pytest.mark.parametrize("selecting", [False, True])
async def test_preparation_retries_only_token_exhaustion_with_more_room(selecting: bool) -> None:
    output: dict[str, object] = (
        {"candidates": [{"proposal": "park", "fit": "quiet", "tradeoff": "weather"}]}
        if selecting
        else {"priorities": ["quiet"], "avoidances": [], "compromise_condition": "short visit"}
    )
    incomplete = response_with(output)
    incomplete.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    service, server, observer, client = await service_for([incomplete, response_with(output)])
    try:
        if selecting:
            result = await service.select_candidates(
                participant=PARTICIPANTS[0],
                question="weekend",
                evidence=EvidenceBundle(),
                preference_frame=PreferenceFrame(PARTICIPANTS[0], ("quiet",), (), "short visit"),
            )
            assert result.selected.proposal == "park"
        else:
            result = await service.form_preferences(participant=PARTICIPANTS[0], question="weekend")
            assert result.priorities == ("quiet",)
    finally:
        await client.aclose()
    first, retry = server.requests
    assert retry == {**first, "max_output_tokens": first["max_output_tokens"] * 2}
    assert len(observer.usages) == len(observer.failures) == 1
    assert observer.failures[0].diagnostic_kind == "max_output_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter", None])
async def test_preparation_incomplete_is_bounded_and_fails_closed(reason: str | None) -> None:
    incomplete = response_with({})
    incomplete.update(
        status="incomplete", incomplete_details={"reason": reason} if reason is not None else None
    )
    expected_calls = 2 if reason == "max_output_tokens" else 1
    service, server, observer, client = await service_for(
        [incomplete.copy() for _ in range(expected_calls)]
    )
    try:
        with pytest.raises(OpenAIIncompleteResponse):
            await service.form_preferences(participant=PARTICIPANTS[0], question="weekend")
    finally:
        await client.aclose()
    assert len(server.requests) == len(observer.failures) == expected_calls
    assert observer.usages == []
