"""Tests for immutable debate content and deterministic voting."""

from dataclasses import FrozenInstanceError
from itertools import permutations
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shittim_chest.domain import (
    PARTICIPANTS,
    STABLE_TIE_BREAK_ORDER,
    EvidenceItem,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
    select_winner,
)
from shittim_chest.domain.debate_content import InvalidVote


def make_vote(
    voter: ParticipantSlot,
    candidate: ParticipantSlot,
    *,
    accuracy: int = 3,
    usefulness: int = 3,
    safety: int = 3,
) -> Vote:
    return Vote(
        voter=voter,
        candidate=candidate,
        accuracy_score=accuracy,
        usefulness_score=usefulness,
        safety_score=safety,
        reason="validated reason",
    )


def test_majority_vote_selects_the_only_highest_count() -> None:
    votes = (
        make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B),
        make_vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A),
        make_vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B),
    )

    result = select_winner(votes)

    assert result.winner is ParticipantSlot.PARTICIPANT_B
    assert result.votes == votes


def test_circular_tie_uses_total_then_documented_score_priority() -> None:
    votes = (
        make_vote(
            ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_B,
            accuracy=4,
            usefulness=3,
            safety=3,
        ),
        make_vote(
            ParticipantSlot.PARTICIPANT_B,
            ParticipantSlot.PARTICIPANT_C,
            accuracy=5,
            usefulness=2,
            safety=4,
        ),
        make_vote(
            ParticipantSlot.PARTICIPANT_C,
            ParticipantSlot.PARTICIPANT_A,
            accuracy=5,
            usefulness=1,
            safety=3,
        ),
    )

    assert select_winner(votes).winner is ParticipantSlot.PARTICIPANT_C


def test_complete_tie_uses_stable_participant_order() -> None:
    votes = (
        make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_C),
        make_vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A),
        make_vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B),
    )

    assert STABLE_TIE_BREAK_ORDER[0] is ParticipantSlot.PARTICIPANT_B
    assert select_winner(votes).winner is ParticipantSlot.PARTICIPANT_B


@pytest.mark.parametrize("score", [0, 6, -1, True, 1.5, "3"])
def test_vote_scores_are_inclusive_integers_from_one_to_five(score: object) -> None:
    with pytest.raises(InvalidVote) as raised:
        Vote(
            voter=ParticipantSlot.PARTICIPANT_A,
            candidate=ParticipantSlot.PARTICIPANT_B,
            accuracy_score=score,  # type: ignore[arg-type]
            usefulness_score=3,
            safety_score=3,
            reason="reason",
        )

    assert raised.value.code == "score_out_of_range"


def test_self_vote_and_long_or_empty_reasons_are_rejected() -> None:
    with pytest.raises(InvalidVote, match="cannot vote") as self_vote:
        make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_A)
    with pytest.raises(ValueError, match="must not be empty"):
        Vote(
            ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_B,
            3,
            3,
            3,
            " ",
        )
    with pytest.raises(InvalidVote) as long_reason:
        Vote(
            ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_B,
            3,
            3,
            3,
            "x" * 501,
        )

    assert self_vote.value.code == "self_vote"
    assert long_reason.value.code == "reason_too_long"


def test_incomplete_duplicate_and_unknown_voter_ballots_are_rejected() -> None:
    valid_a = make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B)
    valid_b = make_vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_C)
    duplicate_a = make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_C)

    with pytest.raises(InvalidVote) as incomplete:
        select_winner((valid_a, valid_b))
    with pytest.raises(InvalidVote) as duplicate:
        select_winner((valid_a, duplicate_a, valid_b))

    assert incomplete.value.code == "incomplete_ballot"
    assert duplicate.value.code == "duplicate_voter"

    with pytest.raises(InvalidVote) as unknown_voter:
        make_vote(cast(ParticipantSlot, "participant-x"), ParticipantSlot.PARTICIPANT_A)
    with pytest.raises(InvalidVote) as unknown_candidate:
        make_vote(ParticipantSlot.PARTICIPANT_A, cast(ParticipantSlot, "participant-x"))

    assert unknown_voter.value.code == "unknown_voter"
    assert unknown_candidate.value.code == "unknown_candidate"


@given(st.permutations(PARTICIPANTS))
def test_ballot_order_never_changes_the_winner(order: list[ParticipantSlot]) -> None:
    by_voter = {
        ParticipantSlot.PARTICIPANT_A: make_vote(
            ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B
        ),
        ParticipantSlot.PARTICIPANT_B: make_vote(
            ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A
        ),
        ParticipantSlot.PARTICIPANT_C: make_vote(
            ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B
        ),
    }

    assert select_winner(tuple(by_voter[voter] for voter in order)).winner is (
        ParticipantSlot.PARTICIPANT_B
    )


def test_vote_is_frozen_slotted_and_total_score_is_derived() -> None:
    vote = make_vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B)

    assert vote.total_score == 9
    assert not hasattr(vote, "__dict__")
    with pytest.raises(FrozenInstanceError):
        vote.reason = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("field", ["source_url", "title", "retrieved_at", "content_hash"])
def test_evidence_item_requires_non_empty_identity_fields(field: str) -> None:
    values = {
        "source_url": "https://example.invalid",
        "title": "title",
        "source_metadata": "metadata",
        "retrieved_at": "2026-07-17T00:00:00Z",
        "content_hash": "sha256:example",
    }
    values[field] = " "

    with pytest.raises(ValueError, match="must not be empty"):
        EvidenceItem(**values)


@pytest.mark.parametrize("model", ["opinion", "proposal", "decision"])
def test_content_models_reject_unknown_participant_slots(model: str) -> None:
    unknown = cast(ParticipantSlot, "participant-x")

    with pytest.raises(ValueError, match="known participant"):
        if model == "opinion":
            InitialOpinion(unknown, "summary", "proposal")
        elif model == "proposal":
            FinalProposal(unknown, "title", "proposal")
        else:
            FinalDecision(unknown, "decision", (), ())


def test_all_derangement_ballots_are_valid_circular_ties() -> None:
    for candidates in permutations(PARTICIPANTS):
        if all(
            voter is not candidate
            for voter, candidate in zip(PARTICIPANTS, candidates, strict=True)
        ):
            result = select_winner(
                make_vote(voter, candidate)
                for voter, candidate in zip(PARTICIPANTS, candidates, strict=True)
            )
            assert result.winner in PARTICIPANTS
