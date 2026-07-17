"""Immutable application input, output, and persistence-boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique

from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebateState,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    Vote,
)


def _require_identifier(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


@dataclass(frozen=True, slots=True)
class AcceptDebateRequest:
    """A validated Discord-independent request to accept a debate."""

    question: str
    requester_id: str
    guild_id: str
    channel_id: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.question) <= 1000 or not self.question.strip():
            raise ValueError("question must contain between 1 and 1000 characters")
        _require_identifier(self.requester_id, label="requester ID")
        _require_identifier(self.guild_id, label="guild ID")
        _require_identifier(self.channel_id, label="channel ID")


@dataclass(frozen=True, slots=True)
class AcceptedDebate:
    """The stable identity returned after atomic acceptance."""

    debate_id: DebateId
    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class CancelDebateCommand:
    """Request cancellation from the original user or a moderator."""

    debate_id: DebateId
    actor_id: str
    can_manage_messages: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.actor_id, label="actor ID")


@dataclass(frozen=True, slots=True)
class CancelledDebate:
    """A debate that reached the immutable cancelled terminal state."""

    debate_id: DebateId
    attempt_id: AttemptId


@dataclass(frozen=True, slots=True)
class RetryDebateCommand:
    """Request a new immutable attempt for a failed debate."""

    debate_id: DebateId
    actor_id: str
    can_manage_messages: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.actor_id, label="actor ID")


@dataclass(frozen=True, slots=True)
class AcceptedRetry:
    """The identity of a newly persisted retry attempt."""

    debate_id: DebateId
    attempt_id: AttemptId
    retry_of: AttemptId


@dataclass(frozen=True, slots=True)
class DebateSnapshot:
    """Application aggregate transferred through the repository Protocol."""

    state: DebateState
    question: str
    requester_id: str
    evidence: EvidenceBundle | None = None
    initial_opinions: tuple[InitialOpinion, ...] = ()
    final_proposals: tuple[FinalProposal, ...] = ()
    votes: tuple[Vote, ...] = ()
    final_decision: FinalDecision | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.question) <= 1000 or not self.question.strip():
            raise ValueError("snapshot question must contain between 1 and 1000 characters")
        _require_identifier(self.requester_id, label="snapshot requester ID")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("error code must be non-empty when present")


@unique
class MetricEvent(StrEnum):
    """Stable low-cardinality application metric events."""

    ACCEPTED = "debate_accepted"
    PHASE_COMPLETED = "debate_phase_completed"
    COMPLETED = "debate_completed"
    CANCELLED = "debate_cancelled"
    FAILED = "debate_failed"
    CHECKPOINTED = "debate_checkpointed"
    RESUMED = "debate_resumed"
    RETRIED = "debate_retried"
