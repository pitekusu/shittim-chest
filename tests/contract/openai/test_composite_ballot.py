import json

import pytest

from shittim_chest.adapters.openai.composite_ballot import score_composite_ballot
from shittim_chest.domain import PARTICIPANTS, EvidenceBundle, FinalProposal, InitialOpinion
from tests.contract.openai.test_responses_service import response_with, service_for


@pytest.mark.asyncio
@pytest.mark.parametrize("voter", PARTICIPANTS)
async def test_both_scores_are_mapped_back_without_provider_selecting_a_winner(voter):
    first = dict(
        entertainment=5,
        character=4,
        originality=3,
        responsiveness=2,
        interaction=3,
        reason="fixture",
    )
    second = {**first, "entertainment": 1}
    service, server, _observer, client = await service_for(
        [response_with({"first": first, "second": second})]
    )
    c, b = tuple(slot for slot in reversed(PARTICIPANTS) if slot != voter)
    try:
        ballot = await score_composite_ballot(
            service,
            voter=voter,
            question="fixture",
            evidence=EvidenceBundle(),
            candidates=(
                FinalProposal(c, "C title", "C proposal"),
                FinalProposal(b, "B title", "B proposal"),
            ),
            initial_opinions=tuple(
                InitialOpinion(slot, "earlier summary", "earlier proposal") for slot in PARTICIPANTS
            ),
        )
        assert [item.candidate for item in ballot.assessments] == [c, b]
        assert ballot.assessments[0].entertainment == 5
        assert ballot.assessments[1].entertainment == 1
        assert [item.reason for item in ballot.assessments] == ["fixture", "fixture"]
        assert len(server.requests) == 1
        request = server.requests[0]
        payload = json.loads(request["input"])
        assert set(payload["candidates"]) == {"first", "second"}
        assert all(slot.value not in request["input"] for slot in PARTICIPANTS)
        assert set(payload["initial_opinions"]) == {"first", "second", "voter"}
        # The current voter speaks; no other persona or internal preparation is supplied.
        assert service.profiles.for_participant(voter).system_prompt in request["instructions"]
        assert all(
            service.profiles.for_participant(slot).system_prompt not in request["instructions"]
            for slot in PARTICIPANTS
            if slot != voter
        )
        assert "preference_frame" not in payload
        assert "candidate_plan" not in payload
        assert request["store"] is False
        assert "candidate_id" not in request["text"]["format"]["schema"]["properties"]
    finally:
        await client.aclose()
