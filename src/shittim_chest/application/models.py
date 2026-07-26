"""Immutable application input, output, and persistence-boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique

from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    EscalationAssessment,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    Vote,
)


def _require_identifier(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_display_text(value: str, *, label: str) -> None:
    """Reject empty or whitespace-only display strings without mutating them."""

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
    requester_username: str
    requester_display_name: str
    guild_id: str
    channel_id: str
    operation_id: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.question) <= 1000 or not self.question.strip():
            raise ValueError("question must contain between 1 and 1000 characters")
        _require_identifier(self.requester_id, label="requester ID")
        _require_display_text(self.requester_username, label="requester username")
        _require_display_text(self.requester_display_name, label="requester display name")
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
    expected_attempt_id: AttemptId | None = None

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
    expected_attempt_id: AttemptId | None = None

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
class BindDiscordContextCommand:
    """Bind the three Discord resources created for one accepted debate."""

    debate_id: DebateId
    starter_message_id: str
    thread_id: str
    control_panel_message_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.starter_message_id, label="starter message ID")
        _require_identifier(self.thread_id, label="thread ID")
        _require_identifier(self.control_panel_message_id, label="control panel message ID")


@dataclass(frozen=True, slots=True)
class BoundDiscordContext:
    """The durable Discord resources associated with one debate."""

    debate_id: DebateId
    starter_message_id: str
    thread_id: str
    control_panel_message_id: str


@dataclass(frozen=True, slots=True)
class DebateAuthorizationSnapshot:
    """Minimal persisted context needed to authorize one HTTP component."""

    debate_id: DebateId
    attempt_id: AttemptId
    phase: DebatePhase
    requester_id: str
    guild_id: str
    channel_id: str
    thread_id: str | None
    control_panel_message_id: str | None

    def __post_init__(self) -> None:
        for label, value in (
            ("requester ID", self.requester_id),
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
        ):
            _require_identifier(value, label=f"authorization {label}")
        if self.thread_id is not None:
            _require_identifier(self.thread_id, label="authorization thread ID")
        if self.control_panel_message_id is not None:
            _require_identifier(
                self.control_panel_message_id,
                label="authorization control panel message ID",
            )


@unique
class PanelRefreshState(StrEnum):
    """Durable delivery state derived from one panel refresh version."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    DELIVERED = "delivered"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class DebateSnapshot:
    """Application aggregate transferred through the repository Protocol."""

    state: DebateState
    question: str
    requester_id: str
    requester_username: str
    requester_display_name: str
    guild_id: str
    channel_id: str
    created_at: datetime
    attempt_created_at: datetime
    origin_ingress_interaction_id: str | None = None
    starter_message_id: str | None = None
    thread_id: str | None = None
    control_panel_message_id: str | None = None
    lease: LeaseGrant | None = None
    panel_refresh_required_at: datetime | None = None
    panel_refreshed_at: datetime | None = None
    panel_refresh_claim_owner: str | None = None
    panel_refresh_claim_expires_at: datetime | None = None
    panel_refresh_next_attempt_at: datetime | None = None
    panel_refresh_delivery_attempt: int = 0
    panel_refresh_failed_at: datetime | None = None
    panel_refresh_error_code: str | None = None
    evidence: EvidenceBundle | None = None
    initial_opinions: tuple[InitialOpinion, ...] = ()
    final_proposals: tuple[FinalProposal, ...] = ()
    votes: tuple[Vote, ...] = ()
    final_decision: FinalDecision | None = None
    escalation_assessment: EscalationAssessment | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.question) <= 1000 or not self.question.strip():
            raise ValueError("snapshot question must contain between 1 and 1000 characters")
        _require_identifier(self.requester_id, label="snapshot requester ID")
        _require_display_text(self.requester_username, label="snapshot requester username")
        _require_display_text(
            self.requester_display_name,
            label="snapshot requester display name",
        )
        _require_identifier(self.guild_id, label="snapshot Guild ID")
        _require_identifier(self.channel_id, label="snapshot channel ID")
        _require_utc(self.created_at, label="snapshot creation timestamp")
        _require_utc(self.attempt_created_at, label="attempt creation timestamp")
        if self.attempt_created_at < self.created_at:
            raise ValueError("attempt creation timestamp cannot precede debate creation")
        if self.state.updated_at < self.attempt_created_at:
            raise ValueError("state timestamp cannot precede attempt creation")
        if self.origin_ingress_interaction_id is not None:
            _require_identifier(
                self.origin_ingress_interaction_id,
                label="origin ingress interaction ID",
            )
        if self.starter_message_id is not None:
            _require_identifier(self.starter_message_id, label="starter message ID")
        if self.thread_id is not None:
            _require_identifier(self.thread_id, label="thread ID")
        if self.control_panel_message_id is not None:
            _require_identifier(
                self.control_panel_message_id,
                label="control panel message ID",
            )
        for label, timestamp in (
            ("panel refresh requirement", self.panel_refresh_required_at),
            ("panel refresh completion", self.panel_refreshed_at),
            ("panel refresh claim expiry", self.panel_refresh_claim_expires_at),
            ("panel refresh next attempt", self.panel_refresh_next_attempt_at),
            ("panel refresh failure", self.panel_refresh_failed_at),
        ):
            if timestamp is not None:
                _require_utc(timestamp, label=label)
        if (self.panel_refresh_claim_owner is None) is not (
            self.panel_refresh_claim_expires_at is None
        ):
            raise ValueError("panel refresh claim owner and expiry must be set together")
        if self.panel_refresh_claim_owner is not None:
            _require_identifier(
                self.panel_refresh_claim_owner,
                label="panel refresh claim owner",
            )
        if isinstance(self.panel_refresh_delivery_attempt, bool) or not isinstance(
            self.panel_refresh_delivery_attempt,
            int,
        ):
            raise TypeError("panel refresh delivery attempt must be an integer")
        if self.panel_refresh_delivery_attempt < 0:
            raise ValueError("panel refresh delivery attempt must be non-negative")
        if self.panel_refreshed_at is not None and self.panel_refresh_required_at is None:
            raise ValueError("panel refresh completion requires a requirement timestamp")
        if (self.panel_refresh_failed_at is None) is not (self.panel_refresh_error_code is None):
            raise ValueError("panel refresh failure timestamp and error code must be set together")
        if self.panel_refresh_error_code is not None:
            if not self.panel_refresh_error_code.strip():
                raise ValueError("panel refresh error code must be non-empty")
            if len(self.panel_refresh_error_code) > 100:
                raise ValueError("panel refresh error code must be at most 100 characters")
        if self.panel_refresh_failed_at is not None:
            required_at = self.panel_refresh_required_at
            if required_at is None:
                raise ValueError("panel refresh failure requires a requirement timestamp")
            if self.panel_refresh_failed_at < required_at:
                raise ValueError("panel refresh failure cannot precede its requirement")
            if self.panel_refreshed_at is not None and self.panel_refreshed_at >= required_at:
                raise ValueError("delivered panel refresh cannot also be abandoned")
        if self.panel_refresh_state is PanelRefreshState.PENDING:
            if self.thread_id is None or self.control_panel_message_id is None:
                raise ValueError("pending panel refresh requires a complete panel binding")
        elif (
            self.panel_refresh_claim_owner is not None
            or self.panel_refresh_next_attempt_at is not None
        ):
            raise ValueError("settled panel refresh cannot retain retry or claim state")
        if self.error_code is not None and not self.error_code.strip():
            raise ValueError("error code must be non-empty when present")

    @property
    def panel_refresh_pending(self) -> bool:
        """Return whether Discord has not acknowledged the latest desired panel state."""

        return self.panel_refresh_state is PanelRefreshState.PENDING

    @property
    def panel_refresh_state(self) -> PanelRefreshState:
        """Derive the durable outcome without persisting a redundant status field."""

        if self.panel_refresh_failed_at is not None:
            return PanelRefreshState.ABANDONED
        required_at = self.panel_refresh_required_at
        if required_at is None:
            return PanelRefreshState.NOT_REQUIRED
        if self.panel_refreshed_at is not None and self.panel_refreshed_at >= required_at:
            return PanelRefreshState.DELIVERED
        return PanelRefreshState.PENDING


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
    OUTBOX_RECOVERED = "discord_outbox_recovered"
    OUTBOX_RETRY_SCHEDULED = "discord_outbox_retry_scheduled"
    PANEL_REFRESH_FAILED = "discord_panel_refresh_failed"
