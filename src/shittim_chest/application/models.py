"""Immutable application input, output, and persistence-boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class AcceptDebateRequest:
    """A validated Discord-independent request to accept a debate."""

    question: str
    requester_id: str
    guild_id: str
    channel_id: str
    operation_id: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.question) <= 1000 or not self.question.strip():
            raise ValueError("question must contain between 1 and 1000 characters")
        _require_identifier(self.requester_id, label="requester ID")
        _require_identifier(self.guild_id, label="guild ID")
        _require_identifier(self.channel_id, label="channel ID")
        _require_identifier(self.operation_id, label="operation ID")


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
    operation_id: str
    can_manage_messages: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.actor_id, label="actor ID")
        _require_identifier(self.operation_id, label="operation ID")


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
    operation_id: str
    can_manage_messages: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.actor_id, label="actor ID")
        _require_identifier(self.operation_id, label="operation ID")


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """One fenced ownership grant for an active attempt."""

    owner_id: str
    slot: int
    fencing_token: int
    expires_at: datetime

    def __post_init__(self) -> None:
        _require_identifier(self.owner_id, label="lease owner ID")
        if isinstance(self.slot, bool) or not isinstance(self.slot, int) or not 0 <= self.slot <= 2:
            raise ValueError("lease slot must be between 0 and 2")
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("fencing token must be a positive integer")
        _require_utc(self.expires_at, label="lease expiry")


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
    guild_id: str
    channel_id: str
    created_at: datetime
    attempt_created_at: datetime
    starter_message_id: str | None = None
    thread_id: str | None = None
    lease: LeaseGrant | None = None
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
        _require_identifier(self.guild_id, label="snapshot Guild ID")
        _require_identifier(self.channel_id, label="snapshot channel ID")
        _require_utc(self.created_at, label="snapshot creation timestamp")
        _require_utc(self.attempt_created_at, label="attempt creation timestamp")
        if self.attempt_created_at < self.created_at:
            raise ValueError("attempt creation timestamp cannot precede debate creation")
        if self.state.updated_at < self.attempt_created_at:
            raise ValueError("state timestamp cannot precede attempt creation")
        if self.starter_message_id is not None:
            _require_identifier(self.starter_message_id, label="starter message ID")
        if self.thread_id is not None:
            _require_identifier(self.thread_id, label="thread ID")
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
