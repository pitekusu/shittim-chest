"""Requester-scoped affection scores and atomic question assessments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Final

from shittim_chest.domain.debate_content import PARTICIPANTS, ParticipantSlot

AFFECTION_RULES_VERSION: Final = "affection-rubric-v1"
DEFAULT_AFFECTION_SCORE: Final = 500
MIN_AFFECTION_SCORE: Final = 0
MAX_AFFECTION_SCORE: Final = 1_000
MIN_QUESTION_SCORE: Final = -100
MAX_QUESTION_SCORE: Final = 100


@unique
class AffectionAssessmentStatus(StrEnum):
    """Whether all three question scores were available and applied."""

    APPLIED = "applied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ParticipantAffection:
    """One participant's before/after values for one debate."""

    participant: ParticipantSlot
    before: int
    question_score: int | None
    applied_delta: int
    after: int

    def __post_init__(self) -> None:
        for label, value in (("before", self.before), ("after", self.after)):
            if isinstance(value, bool) or not MIN_AFFECTION_SCORE <= value <= MAX_AFFECTION_SCORE:
                raise ValueError(f"affection {label} must be between 0 and 1000")
        if self.question_score is not None and (
            isinstance(self.question_score, bool)
            or not MIN_QUESTION_SCORE <= self.question_score <= MAX_QUESTION_SCORE
        ):
            raise ValueError("question score must be between -100 and 100")
        if isinstance(self.applied_delta, bool) or not (
            MIN_QUESTION_SCORE <= self.applied_delta <= MAX_QUESTION_SCORE
        ):
            raise ValueError("applied affection delta must be between -100 and 100")
        if self.after - self.before != self.applied_delta:
            raise ValueError("applied affection delta must equal the effective score change")


@dataclass(frozen=True, slots=True)
class AffectionAssessment:
    """Durable all-or-none result of scoring one debate question."""

    status: AffectionAssessmentStatus
    rules_version: str
    participants: tuple[ParticipantAffection, ...]
    assessed_at: datetime

    def __post_init__(self) -> None:
        if not self.rules_version.strip():
            raise ValueError("affection rules version must not be empty")
        if self.assessed_at.tzinfo is None or self.assessed_at.utcoffset() is None:
            raise ValueError("affection assessment timestamp must be timezone-aware")
        if self.assessed_at.utcoffset() != timedelta(0):
            raise ValueError("affection assessment timestamp must be UTC")
        if tuple(item.participant for item in self.participants) != PARTICIPANTS:
            raise ValueError("affection assessment must contain the three fixed participants")
        if self.status is AffectionAssessmentStatus.APPLIED:
            if any(item.question_score is None for item in self.participants):
                raise ValueError("applied affection assessment requires all three question scores")
        elif any(
            item.question_score is not None or item.applied_delta != 0 or item.after != item.before
            for item in self.participants
        ):
            raise ValueError("unavailable affection assessment must not change scores")

    def score_for(self, participant: ParticipantSlot) -> int:
        """Return the post-assessment score for one participant."""

        return next(item.after for item in self.participants if item.participant is participant)


@dataclass(frozen=True, slots=True)
class AffectionProfile:
    """Current scores for one private requester identity."""

    requester_id: str
    requester_username: str
    requester_display_name: str
    scores: tuple[int, int, int]
    version: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.requester_id.strip():
            raise ValueError("affection requester ID must not be empty")
        if not self.requester_username.strip() or not self.requester_display_name.strip():
            raise ValueError("affection requester display identity must not be empty")
        if len(self.scores) != len(PARTICIPANTS) or any(
            isinstance(score, bool) or not MIN_AFFECTION_SCORE <= score <= MAX_AFFECTION_SCORE
            for score in self.scores
        ):
            raise ValueError("affection profile must contain three scores between 0 and 1000")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("affection profile version must be non-negative")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("affection profile timestamp must be timezone-aware")
        if self.updated_at.utcoffset() != timedelta(0):
            raise ValueError("affection profile timestamp must be UTC")

    @classmethod
    def initial(
        cls,
        *,
        requester_id: str,
        requester_username: str,
        requester_display_name: str,
        at: datetime,
    ) -> AffectionProfile:
        """Create the implicit 500-point profile used before the first assessment."""

        return cls(
            requester_id=requester_id,
            requester_username=requester_username,
            requester_display_name=requester_display_name,
            scores=(
                DEFAULT_AFFECTION_SCORE,
                DEFAULT_AFFECTION_SCORE,
                DEFAULT_AFFECTION_SCORE,
            ),
            version=0,
            updated_at=at,
        )

    def score_for(self, participant: ParticipantSlot) -> int:
        return self.scores[PARTICIPANTS.index(participant)]


def assess_affection(
    profile: AffectionProfile,
    *,
    scores: tuple[int, int, int] | None,
    assessed_at: datetime,
) -> tuple[AffectionProfile, AffectionAssessment]:
    """Apply one complete score set, or persist an all-or-none unavailable result."""

    if scores is not None and any(
        isinstance(score, bool) or not MIN_QUESTION_SCORE <= score <= MAX_QUESTION_SCORE
        for score in scores
    ):
        raise ValueError("question scores must be between -100 and 100")
    if scores is None:
        entries = tuple(
            ParticipantAffection(participant, before, None, 0, before)
            for participant, before in zip(PARTICIPANTS, profile.scores, strict=True)
        )
        return profile, AffectionAssessment(
            status=AffectionAssessmentStatus.UNAVAILABLE,
            rules_version=AFFECTION_RULES_VERSION,
            participants=entries,
            assessed_at=assessed_at,
        )

    entries: list[ParticipantAffection] = []
    updated_scores: list[int] = []
    for participant, before, question_score in zip(
        PARTICIPANTS, profile.scores, scores, strict=True
    ):
        after = min(MAX_AFFECTION_SCORE, max(MIN_AFFECTION_SCORE, before + question_score))
        updated_scores.append(after)
        entries.append(
            ParticipantAffection(
                participant=participant,
                before=before,
                question_score=question_score,
                applied_delta=after - before,
                after=after,
            )
        )
    updated = AffectionProfile(
        requester_id=profile.requester_id,
        requester_username=profile.requester_username,
        requester_display_name=profile.requester_display_name,
        scores=(updated_scores[0], updated_scores[1], updated_scores[2]),
        version=profile.version + 1,
        updated_at=assessed_at,
    )
    return updated, AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=tuple(entries),
        assessed_at=assessed_at,
    )
