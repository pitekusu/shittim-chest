"""Observable scoring, complete ballots, deterministic ties and legacy isolation."""

from dataclasses import replace
from itertools import permutations

import pytest

from shittim_chest.domain.composite_voting import (
    CandidateAssessment,
    CompositeBallot,
    select_composite_winner,
)
from shittim_chest.domain.debate_content import PARTICIPANTS, InvalidVote, Vote, select_winner

A, B, C = PARTICIPANTS


def assessment(candidate, score):
    return CandidateAssessment(candidate, score, score, score, score, score, "fixture")


def ballot(voter, scores):
    return CompositeBallot(
        voter, tuple(assessment(candidate, score) for candidate, score in scores)
    )


def test_exact_weights_and_score_boundaries():
    value = CandidateAssessment(A, 5, 4, 3, 2, 1, "fixture")
    assert value.total == 67
    assert assessment(A, 0).total == 0
    assert assessment(A, 5).total == 100
    for invalid in (-1, 6, True, 2.5):
        with pytest.raises(InvalidVote):
            replace(value, entertainment=invalid)


def test_majority_wins_without_overriding_with_a_high_score():
    ballots = (
        ballot(A, ((B, 3), (C, 2))),
        ballot(B, ((A, 5), (C, 0))),
        ballot(C, ((A, 2), (B, 3))),
    )
    result = select_composite_winner(ballots, debate_key="fixture")
    assert result.winner is B
    assert result.decided_by == "majority"
    assert [item.score_sum for item in result.totals] == [140, 120, 40]


def test_circular_votes_use_both_assessments_not_only_received_votes():
    ballots = (
        ballot(A, ((B, 4), (C, 3))),
        ballot(B, ((A, 3), (C, 4))),
        ballot(C, ((A, 5), (B, 2))),
    )
    result = select_composite_winner(ballots, debate_key="fixture")
    assert result.winner is A
    assert result.decided_by == "composite_score"


def test_complete_ties_are_reproducible_but_not_fixed_to_one_participant():
    ballots = (
        ballot(A, ((B, 5), (C, 4))),
        ballot(B, ((A, 4), (C, 5))),
        ballot(C, ((A, 5), (B, 4))),
    )
    results = [select_composite_winner(ballots, debate_key=f"fixture-{i}") for i in range(60)]
    assert {result.winner for result in results} == set(PARTICIPANTS)
    assert all(result.decided_by == "tie_lottery" for result in results)
    for order in permutations(ballots):
        reversed_options = tuple(
            replace(item, assessments=tuple(reversed(item.assessments))) for item in order
        )
        assert select_composite_winner(reversed_options, debate_key="fixture-0") == results[0]


def test_invalid_coverage_and_legacy_votes_are_not_reinterpreted():
    with pytest.raises(InvalidVote):
        ballot(A, ((A, 4), (B, 4)))
    with pytest.raises(InvalidVote):
        ballot(A, ((B, 4), (B, 4)))
    vote = ballot(A, ((B, 4), (C, 4)))
    with pytest.raises(InvalidVote):
        select_composite_winner((vote, vote, vote), debate_key="fixture")
    old = (
        Vote(A, B, 5, 5, 5, "fixture"),
        Vote(B, C, 5, 5, 5, "fixture"),
        Vote(C, A, 5, 5, 5, "fixture"),
    )
    assert select_winner(old).winner is B
    with pytest.raises(InvalidVote):
        select_composite_winner(old, debate_key="fixture")  # ty: ignore[invalid-argument-type]
