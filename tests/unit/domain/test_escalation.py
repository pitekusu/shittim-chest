"""Tests for deterministic shadow escalation assessment."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

import pytest

from shittim_chest.domain import (
    ESCALATION_RULES_VERSION,
    DebatePhase,
    EscalationAssessment,
    ParticipantSlot,
    Vote,
    assess_escalation,
    select_winner,
)

NOW = datetime(2026, 7, 17, tzinfo=UTC)


def vote(
    voter: ParticipantSlot,
    candidate: ParticipantSlot,
    scores: tuple[int, int, int] = (4, 4, 4),
) -> Vote:
    return Vote(voter, candidate, *scores, "reason")


def test_split_vote_and_low_signals_are_recorded_independently() -> None:
    ballot = (
        vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_C, (2, 3, 3)),
        vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A, (2, 2, 3)),
        vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B, (2, 2, 2)),
    )

    assessment = assess_escalation(select_winner(ballot), assessed_at=NOW)

    assert assessment.rules_version == ESCALATION_RULES_VERSION
    assert assessment.split_vote is True
    assert assessment.winning_axis_low is True
    assert assessment.winning_average_low is True
    assert assessment.has_signal is True
    assert assessment.recommended_restart_phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
    assert assessment.executed is False
    assert assessment.execution_count == 0
    assert assessment.executed_policy_id is None


def test_clear_high_scoring_winner_has_no_signal() -> None:
    ballot = (
        vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B),
        vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A),
        vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B),
    )

    assessment = assess_escalation(select_winner(ballot), assessed_at=NOW)

    assert assessment.split_vote is False
    assert assessment.winning_axis_low is False
    assert assessment.winning_average_low is False
    assert assessment.has_signal is False


def test_ballot_input_order_does_not_change_signals() -> None:
    ballot = (
        vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B, (2, 5, 5)),
        vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A),
        vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B, (4, 4, 4)),
    )
    results = {
        assess_escalation(select_winner(order), assessed_at=NOW) for order in permutations(ballot)
    }

    assert len(results) == 1


def test_assessment_invariants_fail_closed() -> None:
    with pytest.raises(ValueError, match="rules version"):
        assessment(rules_version=" ")
    with pytest.raises(ValueError, match="UTC"):
        assessment(assessed_at=datetime(2026, 7, 17))
    with pytest.raises(ValueError, match="only from final proposal"):
        assessment(recommended_restart_phase=DebatePhase.GENERATING_DECISION)
    with pytest.raises(ValueError, match="at most once"):
        assessment(executed=True, executed_policy_id="terra", execution_count=2)
    with pytest.raises(ValueError, match="flag and count"):
        assessment(executed=True, executed_policy_id="terra")
    with pytest.raises(ValueError, match="exactly one policy"):
        assessment(executed=True, execution_count=1)


def assessment(
    *,
    rules_version: str = ESCALATION_RULES_VERSION,
    assessed_at: datetime = NOW,
    recommended_restart_phase: DebatePhase = DebatePhase.COLLECTING_FINAL_PROPOSALS,
    executed: bool = False,
    executed_policy_id: str | None = None,
    execution_count: int = 0,
) -> EscalationAssessment:
    return EscalationAssessment(
        rules_version=rules_version,
        split_vote=False,
        winning_axis_low=False,
        winning_average_low=False,
        assessed_at=assessed_at,
        recommended_restart_phase=recommended_restart_phase,
        executed=executed,
        executed_policy_id=executed_policy_id,
        execution_count=execution_count,
    )
