"""Versioned entertainment ballots; legacy vote records retain their original rules."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from shittim_chest.domain.debate_content import PARTICIPANTS, InvalidVote, ParticipantSlot

COMPOSITE_VOTING_VERSION = "entertainment-v1"


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    candidate: ParticipantSlot
    entertainment: int
    character: int
    originality: int
    responsiveness: int
    interaction: int
    reason: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ParticipantSlot):
            raise InvalidVote("unknown_candidate", "unknown assessment candidate")
        if any(type(value) is not int or not 0 <= value <= 5 for value in self.axes):
            raise InvalidVote("score_out_of_range", "assessment scores must be integers in 0..5")
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 500:
            raise InvalidVote("invalid_reason", "assessment requires a bounded reason")

    @property
    def axes(self) -> tuple[int, int, int, int, int]:
        return (
            self.entertainment,
            self.character,
            self.originality,
            self.responsiveness,
            self.interaction,
        )

    @property
    def total(self) -> int:
        """Integer score out of 100, with weights 25/25/20/20/10."""
        return sum(score * weight for score, weight in zip(self.axes, (5, 5, 4, 4, 2), strict=True))


@dataclass(frozen=True, slots=True)
class CompositeBallot:
    voter: ParticipantSlot
    assessments: tuple[CandidateAssessment, ...]
    rules_version: str = field(default=COMPOSITE_VOTING_VERSION, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.voter, ParticipantSlot):
            raise InvalidVote("unknown_voter", "unknown ballot voter")
        if not isinstance(self.assessments, tuple) or len(self.assessments) != 2:
            raise InvalidVote("incomplete_assessments", "exactly two assessments required")
        if any(not isinstance(item, CandidateAssessment) for item in self.assessments):
            raise InvalidVote("invalid_assessment", "validated assessments required")
        if {item.candidate for item in self.assessments} != set(PARTICIPANTS) - {self.voter}:
            raise InvalidVote("candidate_coverage", "assess exactly the two other participants")


@dataclass(frozen=True, slots=True)
class ResolvedVote:
    ballot: CompositeBallot
    candidate: ParticipantSlot

    def __post_init__(self) -> None:
        if self.candidate not in {item.candidate for item in self.ballot.assessments}:
            raise InvalidVote("invalid_choice", "vote must select an assessed candidate")

    @property
    def voter(self) -> ParticipantSlot:
        return self.ballot.voter

    @property
    def reason(self) -> str:
        return next(
            item.reason for item in self.ballot.assessments if item.candidate is self.candidate
        )

    @property
    def total_score(self) -> int:
        return next(
            item.total for item in self.ballot.assessments if item.candidate is self.candidate
        )


@dataclass(frozen=True, slots=True)
class CandidateTotal:
    candidate: ParticipantSlot
    vote_count: int
    score_sum: int  # Two independent assessments; 0..200. Average is score_sum / 2.


@dataclass(frozen=True, slots=True)
class CompositeVotingResult:
    winner: ParticipantSlot
    votes: tuple[ResolvedVote, ...]
    totals: tuple[CandidateTotal, ...]
    decided_by: Literal["majority", "composite_score", "tie_lottery"]
    rules_version: str = field(default=COMPOSITE_VOTING_VERSION, init=False)


def _draw(candidates: tuple[ParticipantSlot, ...], debate_key: str, scope: str) -> ParticipantSlot:
    """Stable across retries/order changes, without a fixed participant preference."""

    def rank(candidate: ParticipantSlot) -> tuple[bytes, str]:
        payload = json.dumps(
            [COMPOSITE_VOTING_VERSION, debate_key, scope, candidate.value],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).digest(), candidate.value

    return min(candidates, key=rank)


def select_composite_winner(
    ballots: tuple[CompositeBallot, ...], *, debate_key: str
) -> CompositeVotingResult:
    """Resolve each voter's two scores, then majority, score sum, and a stable draw."""
    if not isinstance(debate_key, str) or not debate_key.strip():
        raise InvalidVote("missing_debate_key", "stable debate key required")
    if len(ballots) != 3 or any(not isinstance(ballot, CompositeBallot) for ballot in ballots):
        raise InvalidVote("incomplete_ballot", "exactly three composite ballots required")
    by_voter = {ballot.voter: ballot for ballot in ballots}
    if set(by_voter) != set(PARTICIPANTS):
        raise InvalidVote("duplicate_voter", "exactly one ballot per participant required")
    resolved = []
    for voter in PARTICIPANTS:
        resolved.append(resolve_composite_ballot(by_voter[voter], debate_key=debate_key))
    counts = Counter(vote.candidate for vote in resolved)
    totals = tuple(
        CandidateTotal(
            candidate,
            counts[candidate],
            sum(
                item.total
                for ballot in ballots
                for item in ballot.assessments
                if item.candidate is candidate
            ),
        )
        for candidate in PARTICIPANTS
    )
    leaders = tuple(item for item in totals if item.vote_count == max(counts.values()))
    method: Literal["majority", "composite_score", "tie_lottery"] = "majority"
    if len(leaders) > 1:
        best_total = max(item.score_sum for item in leaders)
        leaders = tuple(item for item in leaders if item.score_sum == best_total)
        method = "composite_score" if len(leaders) == 1 else "tie_lottery"
    winner = _draw(tuple(item.candidate for item in leaders), debate_key, "winner")
    return CompositeVotingResult(winner, tuple(resolved), totals, method)


def resolve_composite_ballot(ballot: CompositeBallot, *, debate_key: str) -> ResolvedVote:
    if not isinstance(debate_key, str) or not debate_key.strip():
        raise InvalidVote("missing_debate_key", "stable debate key required")
    ballot = CompositeBallot(
        ballot.voter, tuple(sorted(ballot.assessments, key=lambda item: item.candidate.value))
    )
    best = max(item.total for item in ballot.assessments)
    candidates = tuple(item.candidate for item in ballot.assessments if item.total == best)
    return ResolvedVote(ballot, _draw(candidates, debate_key, f"voter:{ballot.voter.value}"))
