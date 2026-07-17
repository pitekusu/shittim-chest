"""Deterministic, content-free quality escalation signals."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from shittim_chest.domain.debate_content import Vote, VotingResult
from shittim_chest.domain.debate_state import DebatePhase

ESCALATION_RULES_VERSION: Final = "escalation-shadow-v1"


@dataclass(frozen=True, slots=True)
class EscalationAssessment:
    """Persisted result of the shadow-only deterministic quality gate."""

    rules_version: str
    split_vote: bool
    winning_axis_low: bool
    winning_average_low: bool
    assessed_at: datetime
    recommended_restart_phase: DebatePhase = DebatePhase.COLLECTING_FINAL_PROPOSALS
    executed: bool = False
    executed_policy_id: str | None = None
    execution_count: int = 0

    def __post_init__(self) -> None:
        if not self.rules_version.strip():
            raise ValueError("escalation rules version must not be empty")
        if self.assessed_at.tzinfo is None or self.assessed_at.utcoffset() != timedelta(0):
            raise ValueError("escalation assessment timestamp must be timezone-aware UTC")
        if self.recommended_restart_phase is not DebatePhase.COLLECTING_FINAL_PROPOSALS:
            raise ValueError("escalation may restart only from final proposal collection")
        if self.execution_count not in (0, 1):
            raise ValueError("escalation may execute at most once")
        if self.executed != (self.execution_count == 1):
            raise ValueError("execution flag and count must agree")
        if self.executed != (self.executed_policy_id is not None):
            raise ValueError("executed escalation requires exactly one policy ID")
        if self.executed_policy_id is not None and not self.executed_policy_id.strip():
            raise ValueError("executed policy ID must not be empty")

    @property
    def has_signal(self) -> bool:
        """Return whether any independently observable shadow signal fired."""

        return self.split_vote or self.winning_axis_low or self.winning_average_low


def assess_escalation(
    voting_result: VotingResult,
    *,
    assessed_at: datetime,
) -> EscalationAssessment:
    """Assess only persisted numeric ballot data without another model call."""

    winner_votes = tuple(
        vote for vote in voting_result.votes if vote.candidate is voting_result.winner
    )
    if not winner_votes:
        raise ValueError("the selected winner must have at least one vote")
    scores = tuple(score for vote in winner_votes for score in _scores(vote))
    vote_counts = Counter(vote.candidate for vote in voting_result.votes)
    return EscalationAssessment(
        rules_version=ESCALATION_RULES_VERSION,
        split_vote=len(vote_counts) == 3 and set(vote_counts.values()) == {1},
        winning_axis_low=any(score <= 2 for score in scores),
        winning_average_low=sum(scores) < 3 * len(scores),
        assessed_at=assessed_at,
    )


def _scores(vote: Vote) -> tuple[int, int, int]:
    return vote.accuracy_score, vote.usefulness_score, vote.safety_score
