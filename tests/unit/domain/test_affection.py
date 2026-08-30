"""Affection score bounds, clamping, and all-or-none assessment tests."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from shittim_chest.domain import (
    AFFECTION_RULES_VERSION,
    PARTICIPANTS,
    AffectionAssessment,
    AffectionAssessmentStatus,
    AffectionProfile,
    ParticipantAffection,
    ParticipantSlot,
    assess_affection,
)

NOW = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)


def profile(*, scores: tuple[int, int, int] = (500, 500, 500)) -> AffectionProfile:
    return AffectionProfile(
        requester_id="private-requester",
        requester_username="requester",
        requester_display_name="Requester",
        scores=scores,
        version=4,
        updated_at=NOW,
    )


def test_initial_profile_uses_the_fixed_three_participant_default() -> None:
    value = AffectionProfile.initial(
        requester_id="private-requester",
        requester_username="requester",
        requester_display_name="Requester",
        at=NOW,
    )

    assert value.scores == (500, 500, 500)
    assert value.version == 0
    assert tuple(value.score_for(participant) for participant in PARTICIPANTS) == value.scores


def test_complete_scores_apply_in_fixed_order_and_report_effective_clamped_delta() -> None:
    updated, assessment = assess_affection(
        profile(scores=(950, 55, 500)),
        scores=(100, -100, 0),
        assessed_at=NOW,
    )

    assert assessment.status is AffectionAssessmentStatus.APPLIED
    assert assessment.rules_version == AFFECTION_RULES_VERSION
    assert updated.scores == (1000, 0, 500)
    assert updated.version == 5
    assert tuple(item.before for item in assessment.participants) == (950, 55, 500)
    assert tuple(item.question_score for item in assessment.participants) == (100, -100, 0)
    assert tuple(item.applied_delta for item in assessment.participants) == (50, -55, 0)
    assert tuple(item.after for item in assessment.participants) == updated.scores


def test_unavailable_assessment_changes_nobody_and_retains_profile_version() -> None:
    source = profile(scores=(625, 55, 987))
    updated, assessment = assess_affection(source, scores=None, assessed_at=NOW)

    assert updated is source
    assert assessment.status is AffectionAssessmentStatus.UNAVAILABLE
    assert tuple(item.question_score for item in assessment.participants) == (None, None, None)
    assert tuple(item.applied_delta for item in assessment.participants) == (0, 0, 0)
    assert tuple(item.after for item in assessment.participants) == source.scores


@pytest.mark.parametrize("invalid", [-101, 101, True])
def test_question_score_outside_the_code_owned_bounds_is_rejected(invalid: int) -> None:
    with pytest.raises(ValueError, match="between -100 and 100"):
        assess_affection(profile(), scores=(invalid, 0, 0), assessed_at=NOW)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"before": -1}, "affection before"),
        ({"after": 1001, "applied_delta": 501}, "affection after"),
        ({"question_score": True}, "question score"),
        ({"applied_delta": True}, "applied affection delta"),
        ({"after": 501}, "effective score change"),
    ],
)
def test_participant_affection_rejects_inconsistent_values(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "participant": ParticipantSlot.PARTICIPANT_A,
        "before": 500,
        "question_score": 0,
        "applied_delta": 0,
        "after": 500,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        ParticipantAffection(**values)  # type: ignore[arg-type]


def _assessment_entries(*, available: bool = True) -> tuple[ParticipantAffection, ...]:
    question_score = 0 if available else None
    return tuple(
        ParticipantAffection(participant, 500, question_score, 0, 500)
        for participant in PARTICIPANTS
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"rules_version": " "}, "rules version"),
        ({"assessed_at": datetime(2026, 8, 30, 1, 0)}, "timezone-aware"),
        (
            {"assessed_at": datetime(2026, 8, 30, 10, 0, tzinfo=timezone(timedelta(hours=9)))},
            "must be UTC",
        ),
        ({"participants": _assessment_entries()[:2]}, "three fixed participants"),
        (
            {
                "status": AffectionAssessmentStatus.APPLIED,
                "participants": _assessment_entries(available=False),
            },
            "requires all three",
        ),
        (
            {
                "status": AffectionAssessmentStatus.UNAVAILABLE,
                "participants": _assessment_entries(),
            },
            "must not change scores",
        ),
    ],
)
def test_affection_assessment_rejects_invalid_state(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "status": AffectionAssessmentStatus.APPLIED,
        "rules_version": AFFECTION_RULES_VERSION,
        "participants": _assessment_entries(),
        "assessed_at": NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        AffectionAssessment(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"requester_id": " "}, "requester ID"),
        ({"requester_username": " "}, "display identity"),
        ({"requester_display_name": " "}, "display identity"),
        ({"scores": (500, 500)}, "three scores"),
        ({"scores": (500, True, 500)}, "three scores"),
        ({"version": True}, "version"),
        ({"updated_at": datetime(2026, 8, 30, 1, 0)}, "timezone-aware"),
        (
            {"updated_at": datetime(2026, 8, 30, 10, 0, tzinfo=timezone(timedelta(hours=9)))},
            "must be UTC",
        ),
    ],
)
def test_affection_profile_rejects_invalid_identity_scores_and_version(
    changes: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "requester_id": "private-requester",
        "requester_username": "requester",
        "requester_display_name": "Requester",
        "scores": (500, 500, 500),
        "version": 4,
        "updated_at": NOW,
    }
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        AffectionProfile(**values)  # type: ignore[arg-type]
