"""Requester-scoped affection scores and atomic question assessments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from re import fullmatch
from typing import Final

from shittim_chest.domain.debate_content import PARTICIPANTS, ParticipantSlot
from shittim_chest.domain.identifiers import DebateId

AFFECTION_RULES_VERSION: Final = "affection-rubric-v1"
DEFAULT_AFFECTION_SCORE: Final = 500
MIN_AFFECTION_SCORE: Final = 0
MAX_AFFECTION_SCORE: Final = 1_000
MIN_QUESTION_SCORE: Final = -100
MAX_QUESTION_SCORE: Final = 100
OPAQUE_REQUESTER_KEY_PATTERN: Final = r"[A-Za-z0-9_-]{43}"


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
class MemorialUnlock:
    """The participant selected when one affection cycle first reaches its maximum."""

    participant: ParticipantSlot
    unlocked_at: datetime
    debate_id: DebateId
    requester_display_name: str
    memorial_cycle: int
    retroactive: bool = False

    def __post_init__(self) -> None:
        _require_utc(self.unlocked_at, label="memorial unlock timestamp")
        if not self.requester_display_name.strip():
            raise ValueError("memorial unlock display name must not be empty")
        if isinstance(self.memorial_cycle, bool) or self.memorial_cycle < 1:
            raise ValueError("memorial cycle must be positive")
        if not isinstance(self.retroactive, bool):
            raise ValueError("memorial retroactive provenance must be boolean")


@dataclass(frozen=True, slots=True)
class AffectionAssessment:
    """Durable all-or-none result of scoring one debate question."""

    status: AffectionAssessmentStatus
    rules_version: str
    participants: tuple[ParticipantAffection, ...]
    assessed_at: datetime
    memorial_unlock: MemorialUnlock | None = None

    def __post_init__(self) -> None:
        if not self.rules_version.strip():
            raise ValueError("affection rules version must not be empty")
        _require_utc(self.assessed_at, label="affection assessment timestamp")
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
        if self.memorial_unlock is not None:
            if self.status is not AffectionAssessmentStatus.APPLIED:
                raise ValueError("only an applied affection assessment may unlock a memorial")
            selected = next(
                item
                for item in self.participants
                if item.participant is self.memorial_unlock.participant
            )
            if self.memorial_unlock.retroactive:
                if selected.before != MAX_AFFECTION_SCORE:
                    raise ValueError("retroactive memorial participant must already be maximum")
            elif selected.before >= MAX_AFFECTION_SCORE or selected.after != MAX_AFFECTION_SCORE:
                raise ValueError("memorial participant must newly reach maximum affection")
            if self.memorial_unlock.unlocked_at != self.assessed_at:
                raise ValueError("memorial unlock must share the assessment timestamp")

    def score_for(self, participant: ParticipantSlot) -> int:
        """Return the post-assessment score for one participant."""

        return next(item.after for item in self.participants if item.participant is participant)


@dataclass(frozen=True, slots=True)
class AffectionProfile:
    """Current scores for one private requester identity."""

    requester_key: str
    requester_username: str
    requester_display_name: str
    scores: tuple[int, int, int]
    version: int
    updated_at: datetime
    reset_count: int = 0
    memorial_cycle: int = 1
    memorial_unlock: MemorialUnlock | None = None

    def __post_init__(self) -> None:
        if fullmatch(OPAQUE_REQUESTER_KEY_PATTERN, self.requester_key) is None:
            raise ValueError("affection requester key must be an opaque HMAC identity")
        if not self.requester_username.strip() or not self.requester_display_name.strip():
            raise ValueError("affection requester display identity must not be empty")
        if len(self.scores) != len(PARTICIPANTS) or any(
            isinstance(score, bool) or not MIN_AFFECTION_SCORE <= score <= MAX_AFFECTION_SCORE
            for score in self.scores
        ):
            raise ValueError("affection profile must contain three scores between 0 and 1000")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("affection profile version must be non-negative")
        _require_utc(self.updated_at, label="affection profile timestamp")
        if isinstance(self.reset_count, bool) or self.reset_count < 0:
            raise ValueError("affection reset count must be non-negative")
        if isinstance(self.memorial_cycle, bool) or self.memorial_cycle < 1:
            raise ValueError("affection memorial cycle must be positive")
        if self.memorial_cycle != self.reset_count + 1:
            raise ValueError("affection memorial cycle must follow the reset count")
        if (
            self.memorial_unlock is not None
            and self.memorial_unlock.memorial_cycle != self.memorial_cycle
        ):
            raise ValueError("memorial unlock must belong to the current cycle")

    @classmethod
    def initial(
        cls,
        *,
        requester_key: str,
        requester_username: str,
        requester_display_name: str,
        at: datetime,
    ) -> AffectionProfile:
        """Create the implicit 500-point profile used before the first assessment."""

        return cls(
            requester_key=requester_key,
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
    debate_id: DebateId | None = None,
    operation_seed: str | None = None,
    allow_existing_max_unlock: bool = False,
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
    memorial_unlock = profile.memorial_unlock
    new_candidates = tuple(
        item
        for item in entries
        if (item.before < MAX_AFFECTION_SCORE and item.after == MAX_AFFECTION_SCORE)
        or (allow_existing_max_unlock and item.before == MAX_AFFECTION_SCORE)
    )
    assessment_unlock: MemorialUnlock | None = None
    if memorial_unlock is None and new_candidates:
        if debate_id is None or operation_seed is None or not operation_seed.strip():
            raise ValueError("memorial unlock requires a debate ID and operation seed")
        selected = _select_memorial_candidate(new_candidates, operation_seed=operation_seed)
        assessment_unlock = MemorialUnlock(
            participant=selected.participant,
            unlocked_at=assessed_at,
            debate_id=debate_id,
            requester_display_name=profile.requester_display_name,
            memorial_cycle=profile.memorial_cycle,
            retroactive=selected.before == MAX_AFFECTION_SCORE,
        )
        memorial_unlock = assessment_unlock
    updated = AffectionProfile(
        requester_key=profile.requester_key,
        requester_username=profile.requester_username,
        requester_display_name=profile.requester_display_name,
        scores=(updated_scores[0], updated_scores[1], updated_scores[2]),
        version=profile.version + 1,
        updated_at=assessed_at,
        reset_count=profile.reset_count,
        memorial_cycle=profile.memorial_cycle,
        memorial_unlock=memorial_unlock,
    )
    return updated, AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=tuple(entries),
        assessed_at=assessed_at,
        memorial_unlock=assessment_unlock,
    )


def _select_memorial_candidate(
    candidates: tuple[ParticipantAffection, ...],
    *,
    operation_seed: str,
) -> ParticipantAffection:
    highest_before = max(item.before for item in candidates)
    finalists = tuple(item for item in candidates if item.before == highest_before)
    highest_question_score = max(
        item.question_score for item in finalists if item.question_score is not None
    )
    finalists = tuple(item for item in finalists if item.question_score == highest_question_score)
    if len(finalists) == 1:
        return finalists[0]
    stable_identity = ",".join(item.participant.value for item in finalists)
    digest = hashlib.sha256(f"{operation_seed}:{stable_identity}".encode()).digest()
    return finalists[int.from_bytes(digest, "big") % len(finalists)]


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be UTC")
