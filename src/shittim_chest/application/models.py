"""Immutable application input, output, and persistence-boundary models."""

from __future__ import annotations

from dataclasses import dataclass, replace
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
    ParticipantSlot,
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


@unique
class GenerationStatus(StrEnum):
    """Durable state of one logical model generation."""

    PLANNED = "planned"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GenerationCheckpoint:
    """Fence at most two provider calls for one phase/participant output."""

    phase: DebatePhase
    participant: ParticipantSlot
    status: GenerationStatus
    logical_attempt: int
    planned_at: datetime
    claim_owner: str | None = None
    claim_slot: int | None = None
    claim_fencing_token: int | None = None
    claimed_at: datetime | None = None
    settled_at: datetime | None = None
    error_code: str | None = None
    record_schema_version: int = 1

    def __post_init__(self) -> None:
        if self.phase.is_terminal:
            raise ValueError("generation checkpoint phase must be active")
        if self.record_schema_version != 1:
            raise ValueError("unsupported generation checkpoint record schema")
        if (
            isinstance(self.logical_attempt, bool)
            or not isinstance(self.logical_attempt, int)
            or not 0 <= self.logical_attempt <= 2
        ):
            raise ValueError("generation logical attempt must be between zero and two")
        _require_utc(self.planned_at, label="generation planning timestamp")
        claim_values = (
            self.claim_owner,
            self.claim_slot,
            self.claim_fencing_token,
            self.claimed_at,
        )
        if any(value is None for value in claim_values) != all(
            value is None for value in claim_values
        ):
            raise ValueError("generation claim identity must be complete or absent")
        if self.claim_owner is not None:
            _require_identifier(self.claim_owner, label="generation claim owner")
            if (
                isinstance(self.claim_slot, bool)
                or not isinstance(self.claim_slot, int)
                or not 0 <= self.claim_slot <= 2
            ):
                raise ValueError("generation claim slot must be between zero and two")
            if (
                isinstance(self.claim_fencing_token, bool)
                or not isinstance(self.claim_fencing_token, int)
                or self.claim_fencing_token < 1
            ):
                raise ValueError("generation fencing token must be positive")
            if self.claimed_at is None:  # pragma: no cover - complete identity narrows this
                raise AssertionError("generation claim timestamp disappeared")
            _require_utc(self.claimed_at, label="generation claim timestamp")
            if self.claimed_at < self.planned_at:
                raise ValueError("generation claim cannot precede planning")
        if self.settled_at is not None:
            _require_utc(self.settled_at, label="generation settlement timestamp")
            earliest_settlement = self.claimed_at or self.planned_at
            if self.settled_at < earliest_settlement:
                raise ValueError("generation settlement cannot move backwards")
        if self.error_code is not None:
            _require_identifier(self.error_code, label="generation error code")
        if self.status is GenerationStatus.PLANNED:
            if self.logical_attempt != 0 or any(value is not None for value in claim_values):
                raise ValueError("planned generation cannot contain a provider claim")
            if self.settled_at is not None or self.error_code is not None:
                raise ValueError("planned generation cannot be settled")
        elif self.status is GenerationStatus.IN_FLIGHT:
            if self.logical_attempt not in {1, 2} or self.claim_owner is None:
                raise ValueError("in-flight generation requires one exact claim")
            if self.settled_at is not None or self.error_code is not None:
                raise ValueError("in-flight generation cannot be settled")
        elif self.status is GenerationStatus.COMPLETED:
            if self.settled_at is None:
                raise ValueError("completed generation requires a settlement")
            if self.logical_attempt == 0 and any(value is not None for value in claim_values):
                raise ValueError("reused generation cannot contain a provider claim")
            if self.logical_attempt > 0 and self.claim_owner is None:
                raise ValueError("called completed generation requires its provider claim")
            if self.error_code is not None:
                raise ValueError("completed generation cannot contain an error")
        elif self.status is GenerationStatus.FAILED:
            if self.logical_attempt not in {0, 1, 2} or self.settled_at is None:
                raise ValueError("failed generation requires a settlement")
            if self.logical_attempt == 0 and any(value is not None for value in claim_values):
                raise ValueError("uncalled failed generation cannot contain a provider claim")
            if self.logical_attempt > 0 and self.claim_owner is None:
                raise ValueError("called failed generation requires its provider claim")
            if self.error_code is None:
                raise ValueError("failed generation requires an error code")

    @classmethod
    def planned(
        cls,
        *,
        phase: DebatePhase,
        participant: ParticipantSlot,
        at: datetime,
    ) -> GenerationCheckpoint:
        return cls(
            phase=phase,
            participant=participant,
            status=GenerationStatus.PLANNED,
            logical_attempt=0,
            planned_at=at,
        )

    @classmethod
    def reused(
        cls,
        *,
        phase: DebatePhase,
        participant: ParticipantSlot,
        at: datetime,
    ) -> GenerationCheckpoint:
        """Bind one already durable output to a retry without another provider call."""

        return cls(
            phase=phase,
            participant=participant,
            status=GenerationStatus.COMPLETED,
            logical_attempt=0,
            planned_at=at,
            settled_at=at,
        )

    def claim(self, *, lease: LeaseGrant, at: datetime) -> GenerationCheckpoint:
        """Claim the first call or one successor-fenced recovery call."""

        _require_utc(at, label="generation claim timestamp")
        if self.status is GenerationStatus.PLANNED:
            logical_attempt = 1
        elif self.status is GenerationStatus.IN_FLIGHT and self.logical_attempt == 1:
            identity = (lease.owner_id, lease.slot, lease.fencing_token)
            current_identity = (self.claim_owner, self.claim_slot, self.claim_fencing_token)
            if identity == current_identity:
                raise ValueError("the same lease cannot claim a generation twice")
            logical_attempt = 2
        else:
            raise ValueError("generation checkpoint is not claimable")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=GenerationStatus.IN_FLIGHT,
            logical_attempt=logical_attempt,
            planned_at=self.planned_at,
            claim_owner=lease.owner_id,
            claim_slot=lease.slot,
            claim_fencing_token=lease.fencing_token,
            claimed_at=at,
        )

    def complete(self, *, lease: LeaseGrant, at: datetime) -> GenerationCheckpoint:
        return self._settle(lease=lease, at=at, error_code=None)

    def fail(
        self,
        *,
        lease: LeaseGrant,
        at: datetime,
        error_code: str,
    ) -> GenerationCheckpoint:
        return self._settle(lease=lease, at=at, error_code=error_code)

    def fail_before_call(self, *, at: datetime, error_code: str) -> GenerationCheckpoint:
        """Settle planned work without falsely recording a provider call."""

        if self.status is not GenerationStatus.PLANNED:
            raise ValueError("only planned generation may fail before a provider call")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=GenerationStatus.FAILED,
            logical_attempt=0,
            planned_at=self.planned_at,
            settled_at=at,
            error_code=error_code,
        )

    def cancel(self, *, at: datetime) -> GenerationCheckpoint:
        """Settle unfinished generation while preserving its observed call count."""

        if self.status in {GenerationStatus.COMPLETED, GenerationStatus.FAILED}:
            return self
        if self.status is GenerationStatus.PLANNED:
            return self.fail_before_call(at=at, error_code="generation_cancelled")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=GenerationStatus.FAILED,
            logical_attempt=self.logical_attempt,
            planned_at=self.planned_at,
            claim_owner=self.claim_owner,
            claim_slot=self.claim_slot,
            claim_fencing_token=self.claim_fencing_token,
            claimed_at=self.claimed_at,
            settled_at=at,
            error_code="generation_cancelled",
        )

    def exhaust_after_recovery(
        self,
        *,
        lease: LeaseGrant,
        at: datetime,
        error_code: str,
    ) -> GenerationCheckpoint:
        """Fail without a third call after two ambiguous worker losses."""

        if self.status is not GenerationStatus.IN_FLIGHT or self.logical_attempt != 2:
            raise ValueError("only a second in-flight call may be exhausted")
        identity = (lease.owner_id, lease.slot, lease.fencing_token)
        if identity == (self.claim_owner, self.claim_slot, self.claim_fencing_token):
            raise ValueError("generation exhaustion requires a successor lease")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=GenerationStatus.FAILED,
            logical_attempt=2,
            planned_at=self.planned_at,
            claim_owner=self.claim_owner,
            claim_slot=self.claim_slot,
            claim_fencing_token=self.claim_fencing_token,
            claimed_at=self.claimed_at,
            settled_at=at,
            error_code=error_code,
        )

    def fail_before_recovery_call(
        self,
        *,
        lease: LeaseGrant,
        at: datetime,
        error_code: str,
    ) -> GenerationCheckpoint:
        """Settle ambiguous prior work without claiming an unmade provider call."""

        if self.status is not GenerationStatus.IN_FLIGHT:
            raise ValueError("only in-flight generation may fail before recovery call")
        identity = (lease.owner_id, lease.slot, lease.fencing_token)
        if identity == (self.claim_owner, self.claim_slot, self.claim_fencing_token):
            raise ValueError("generation recovery failure requires a successor lease")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=GenerationStatus.FAILED,
            logical_attempt=self.logical_attempt,
            planned_at=self.planned_at,
            claim_owner=self.claim_owner,
            claim_slot=self.claim_slot,
            claim_fencing_token=self.claim_fencing_token,
            claimed_at=self.claimed_at,
            settled_at=at,
            error_code=error_code,
        )

    def _settle(
        self,
        *,
        lease: LeaseGrant,
        at: datetime,
        error_code: str | None,
    ) -> GenerationCheckpoint:
        if self.status is not GenerationStatus.IN_FLIGHT:
            raise ValueError("only an in-flight generation may settle")
        identity = (lease.owner_id, lease.slot, lease.fencing_token)
        if identity != (self.claim_owner, self.claim_slot, self.claim_fencing_token):
            raise ValueError("generation settlement lost its lease fence")
        return GenerationCheckpoint(
            phase=self.phase,
            participant=self.participant,
            status=(GenerationStatus.COMPLETED if error_code is None else GenerationStatus.FAILED),
            logical_attempt=self.logical_attempt,
            planned_at=self.planned_at,
            claim_owner=self.claim_owner,
            claim_slot=self.claim_slot,
            claim_fencing_token=self.claim_fencing_token,
            claimed_at=self.claimed_at,
            settled_at=at,
            error_code=error_code,
        )


@unique
class PhaseDeliveryStatus(StrEnum):
    """Durable lifecycle of one ordered phase delivery plan."""

    STAGED = "staged"
    TERMINATING = "terminating"
    DELIVERED = "delivered"
    ABANDONED = "abandoned"


@unique
class DeliveryAbandonReason(StrEnum):
    """Allowlisted reasons safe to persist and expose in diagnostics."""

    NON_RETRYABLE = "non_retryable"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    CONTENT_CONFLICT = "content_conflict"


@dataclass(frozen=True, slots=True)
class PhaseDeliveryPlan:
    """Versioned ordered Discord delivery plan retained as attempt history."""

    plan_id: str
    source_phase: DebatePhase
    target_phase: DebatePhase
    operation_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    delivery_sequences: tuple[int, ...]
    staged_at: datetime
    deadline_at: datetime
    status: PhaseDeliveryStatus = PhaseDeliveryStatus.STAGED
    settled_at: datetime | None = None
    abandon_reason: DeliveryAbandonReason | None = None
    record_schema_version: int = 2

    def __post_init__(self) -> None:
        _require_identifier(self.plan_id, label="phase delivery plan ID")
        if self.source_phase.is_terminal or self.source_phase is self.target_phase:
            raise ValueError("phase delivery must advance from one active source phase")
        if self.record_schema_version != 2:
            raise ValueError("unsupported phase delivery record schema")
        if not self.operation_ids or len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("phase delivery operation IDs must be non-empty and unique")
        if len(self.content_hashes) != len(self.operation_ids):
            raise ValueError("phase delivery hashes must match operation IDs")
        if len(self.delivery_sequences) != len(self.operation_ids):
            raise ValueError("phase delivery sequences must match operation IDs")
        if tuple(sorted(self.delivery_sequences)) != self.delivery_sequences or len(
            set(self.delivery_sequences)
        ) != len(self.delivery_sequences):
            raise ValueError("phase delivery sequences must be unique and increasing")
        for sequence in self.delivery_sequences:
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("phase delivery sequence must be a non-negative integer")
        for operation_id in self.operation_ids:
            _require_identifier(operation_id, label="phase delivery operation ID")
        for content_hash in self.content_hashes:
            if len(content_hash) != 64 or any(
                character not in "0123456789abcdef" for character in content_hash
            ):
                raise ValueError("phase delivery content hash must be lowercase SHA-256")
        _require_utc(self.staged_at, label="phase delivery staging timestamp")
        _require_utc(self.deadline_at, label="phase delivery deadline")
        if self.deadline_at != self.staged_at + timedelta(minutes=15):
            raise ValueError("phase delivery deadline must be exactly 15 minutes after staging")
        if self.settled_at is not None:
            _require_utc(self.settled_at, label="phase delivery settlement timestamp")
            if self.settled_at < self.staged_at:
                raise ValueError("phase delivery cannot settle before staging")
        if self.status is PhaseDeliveryStatus.STAGED:
            if self.settled_at is not None or self.abandon_reason is not None:
                raise ValueError("staged phase delivery cannot contain a result")
        elif self.status is PhaseDeliveryStatus.TERMINATING:
            if self.settled_at is not None or self.abandon_reason is None:
                raise ValueError("terminating phase delivery requires only its stop reason")
        elif self.status is PhaseDeliveryStatus.DELIVERED:
            if self.settled_at is None or self.abandon_reason is not None:
                raise ValueError("delivered phase plan requires only a settlement timestamp")
        elif self.status is PhaseDeliveryStatus.ABANDONED and (
            self.settled_at is None or self.abandon_reason is None
        ):
            raise ValueError("abandoned phase plan requires a timestamp and reason")

    @property
    def completed_at(self) -> datetime | None:
        """Compatibility projection used by the terminal aggregate."""

        return self.settled_at if self.status is PhaseDeliveryStatus.DELIVERED else None

    def complete(self, *, at: datetime) -> PhaseDeliveryPlan:
        if self.status is PhaseDeliveryStatus.DELIVERED:
            return self
        if self.status not in {
            PhaseDeliveryStatus.STAGED,
            PhaseDeliveryStatus.TERMINATING,
        }:
            raise ValueError("only an unsettled phase plan may be delivered")
        return PhaseDeliveryPlan(
            plan_id=self.plan_id,
            source_phase=self.source_phase,
            target_phase=self.target_phase,
            operation_ids=self.operation_ids,
            content_hashes=self.content_hashes,
            delivery_sequences=self.delivery_sequences,
            staged_at=self.staged_at,
            deadline_at=self.deadline_at,
            status=PhaseDeliveryStatus.DELIVERED,
            settled_at=at,
        )

    def terminate(self, *, reason: DeliveryAbandonReason) -> PhaseDeliveryPlan:
        if self.status is PhaseDeliveryStatus.TERMINATING:
            if self.abandon_reason is not reason:
                raise ValueError("phase delivery is terminating for another reason")
            return self
        if self.status is not PhaseDeliveryStatus.STAGED:
            raise ValueError("only a staged phase plan may terminate")
        return PhaseDeliveryPlan(
            plan_id=self.plan_id,
            source_phase=self.source_phase,
            target_phase=self.target_phase,
            operation_ids=self.operation_ids,
            content_hashes=self.content_hashes,
            delivery_sequences=self.delivery_sequences,
            staged_at=self.staged_at,
            deadline_at=self.deadline_at,
            status=PhaseDeliveryStatus.TERMINATING,
            abandon_reason=reason,
        )

    def abandon(
        self,
        *,
        at: datetime,
        reason: DeliveryAbandonReason,
    ) -> PhaseDeliveryPlan:
        if self.status is PhaseDeliveryStatus.ABANDONED:
            return self
        if self.status not in {PhaseDeliveryStatus.STAGED, PhaseDeliveryStatus.TERMINATING}:
            raise ValueError("only an unsettled phase plan may be abandoned")
        if self.status is PhaseDeliveryStatus.TERMINATING and self.abandon_reason is not reason:
            raise ValueError("phase delivery termination reason cannot change")
        return PhaseDeliveryPlan(
            plan_id=self.plan_id,
            source_phase=self.source_phase,
            target_phase=self.target_phase,
            operation_ids=self.operation_ids,
            content_hashes=self.content_hashes,
            delivery_sequences=self.delivery_sequences,
            staged_at=self.staged_at,
            deadline_at=self.deadline_at,
            status=PhaseDeliveryStatus.ABANDONED,
            settled_at=at,
            abandon_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class TerminalDeliveryPlan:
    """Durable required Discord delivery staged before a terminal transition."""

    target_phase: DebatePhase
    operation_ids: tuple[str, ...]
    content_hashes: tuple[str, ...]
    staged_at: datetime
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.target_phase.is_terminal:
            raise ValueError("terminal delivery target must be a terminal phase")
        if not self.operation_ids or len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("terminal delivery operation IDs must be non-empty and unique")
        if len(self.content_hashes) != len(self.operation_ids):
            raise ValueError("terminal delivery hashes must match operation IDs")
        for operation_id in self.operation_ids:
            _require_identifier(operation_id, label="terminal delivery operation ID")
        for content_hash in self.content_hashes:
            if len(content_hash) != 64 or any(
                character not in "0123456789abcdef" for character in content_hash
            ):
                raise ValueError("terminal delivery content hash must be lowercase SHA-256")
        _require_utc(self.staged_at, label="terminal delivery staging timestamp")
        if self.completed_at is not None:
            _require_utc(self.completed_at, label="terminal delivery completion timestamp")
            if self.completed_at < self.staged_at:
                raise ValueError("terminal delivery cannot complete before it is staged")

    def complete(self, *, at: datetime) -> TerminalDeliveryPlan:
        """Record that every required persisted operation is SENT."""

        _require_utc(at, label="terminal delivery completion timestamp")
        if at < self.staged_at:
            raise ValueError("terminal delivery cannot complete before it is staged")
        if self.completed_at is not None:
            return self
        return TerminalDeliveryPlan(
            target_phase=self.target_phase,
            operation_ids=self.operation_ids,
            content_hashes=self.content_hashes,
            staged_at=self.staged_at,
            completed_at=at,
        )


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
    generation_checkpoints: tuple[GenerationCheckpoint, ...] = ()
    error_code: str | None = None
    terminal_delivery: TerminalDeliveryPlan | PhaseDeliveryPlan | None = None

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
        checkpoint_keys = tuple(
            (checkpoint.phase, checkpoint.participant) for checkpoint in self.generation_checkpoints
        )
        if len(checkpoint_keys) != len(set(checkpoint_keys)):
            raise ValueError("generation checkpoints must be unique by phase and participant")
        for checkpoint in self.generation_checkpoints:
            if checkpoint.planned_at < self.attempt_created_at:
                raise ValueError("generation checkpoint cannot precede its attempt")
            if checkpoint.status in {GenerationStatus.PLANNED, GenerationStatus.IN_FLIGHT}:
                if self.state.phase is not checkpoint.phase:
                    raise ValueError("unsettled generation must remain at its active phase")
            elif (
                checkpoint.status is GenerationStatus.COMPLETED
                and checkpoint.phase is DebatePhase.GENERATING_DECISION
                and (
                    self.final_decision is None
                    or self.final_decision.winner is not checkpoint.participant
                )
            ):
                raise ValueError(
                    "completed decision generation requires its Python-selected output"
                )
            elif checkpoint.status is GenerationStatus.FAILED:
                cancelling = self.state.phase is DebatePhase.CANCELLED or (
                    self.terminal_delivery is not None
                    and self.terminal_delivery.target_phase is DebatePhase.CANCELLED
                )
                if not cancelling and self.error_code != checkpoint.error_code:
                    raise ValueError("failed generation and attempt error must match")
                if (
                    checkpoint.phase is DebatePhase.GENERATING_DECISION
                    and self.final_decision is not None
                ):
                    raise ValueError("failed generation cannot retain a final decision")
        initial_checkpoints = {
            checkpoint.participant: checkpoint
            for checkpoint in self.generation_checkpoints
            if checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
        }
        initial_outputs = {opinion.participant for opinion in self.initial_opinions}
        if initial_checkpoints:
            if any(
                participant not in initial_outputs
                for participant, checkpoint in initial_checkpoints.items()
                if checkpoint.status is GenerationStatus.COMPLETED
            ):
                raise ValueError("completed initial generation requires its durable output")
            if any(
                initial_checkpoints.get(participant) is None
                or initial_checkpoints[participant].status is not GenerationStatus.COMPLETED
                for participant in initial_outputs
            ):
                raise ValueError("initial output requires its completed generation checkpoint")
        final_proposal_checkpoints = {
            checkpoint.participant: checkpoint
            for checkpoint in self.generation_checkpoints
            if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
        }
        final_proposal_outputs = {proposal.participant for proposal in self.final_proposals}
        if final_proposal_checkpoints:
            if any(
                participant not in final_proposal_outputs
                for participant, checkpoint in final_proposal_checkpoints.items()
                if checkpoint.status is GenerationStatus.COMPLETED
            ):
                raise ValueError("completed final proposal generation requires its durable output")
            if any(
                final_proposal_checkpoints.get(participant) is None
                or final_proposal_checkpoints[participant].status is not GenerationStatus.COMPLETED
                for participant in final_proposal_outputs
            ):
                raise ValueError(
                    "final proposal output requires its completed generation checkpoint"
                )
        vote_checkpoints = {
            checkpoint.participant: checkpoint
            for checkpoint in self.generation_checkpoints
            if checkpoint.phase is DebatePhase.SELECTING_WINNER
        }
        vote_outputs = {vote.voter for vote in self.votes}
        if vote_checkpoints:
            if any(
                participant not in vote_outputs
                for participant, checkpoint in vote_checkpoints.items()
                if checkpoint.status is GenerationStatus.COMPLETED
            ):
                raise ValueError("completed vote generation requires its durable output")
            if any(
                vote_checkpoints.get(participant) is None
                or vote_checkpoints[participant].status is not GenerationStatus.COMPLETED
                for participant in vote_outputs
            ):
                raise ValueError("vote output requires its completed generation checkpoint")
        delivery = self.terminal_delivery
        if delivery is not None:
            if any(
                value is None
                for value in (
                    self.starter_message_id,
                    self.thread_id,
                    self.control_panel_message_id,
                )
            ):
                raise ValueError("terminal delivery requires a complete Discord binding")
            if isinstance(delivery, PhaseDeliveryPlan):
                if self.state.phase.is_terminal:
                    if delivery.status is PhaseDeliveryStatus.DELIVERED:
                        if delivery.target_phase is not self.state.phase:
                            raise ValueError("delivered plan must match the terminal phase")
                    elif delivery.status is PhaseDeliveryStatus.ABANDONED:
                        expected_phase = (
                            DebatePhase.FAILED
                            if delivery.target_phase is DebatePhase.COMPLETED
                            else delivery.target_phase
                        )
                        if self.state.phase is not expected_phase:
                            raise ValueError(
                                "abandoned plan must converge to its safe terminal phase"
                            )
                    else:
                        raise ValueError("terminal phase requires a settled phase delivery plan")
                elif delivery.status is PhaseDeliveryStatus.DELIVERED:
                    if self.state.phase is not delivery.target_phase:
                        raise ValueError("delivered active plan must match its target phase")
                elif self.state.phase is not delivery.source_phase:
                    raise ValueError("unsettled active plan must remain at its source phase")
            elif self.state.phase.is_terminal:
                if delivery.target_phase is not self.state.phase or delivery.completed_at is None:
                    raise ValueError("terminal phase requires its completed delivery plan")
            elif delivery.completed_at is not None:
                raise ValueError("active attempt cannot retain a completed terminal delivery")
            if delivery.target_phase is DebatePhase.COMPLETED:
                if self.final_decision is None:
                    raise ValueError("completed delivery requires a decision")
                if (
                    isinstance(delivery, PhaseDeliveryPlan)
                    and delivery.status is PhaseDeliveryStatus.ABANDONED
                ):
                    if self.state.phase.is_terminal and self.error_code is None:
                        raise ValueError("abandoned completed delivery requires an error")
                elif self.error_code is not None:
                    raise ValueError("completed delivery cannot retain an error")
            elif delivery.target_phase is DebatePhase.FAILED:
                if self.error_code is None:
                    raise ValueError("failed delivery requires an error code")
            elif self.error_code is not None:
                raise ValueError("cancelled delivery cannot retain an error code")

    @property
    def terminal_delivery_complete(self) -> bool:
        """Return whether the staged required delivery matches the terminal phase."""

        delivery = self.terminal_delivery
        if not self.state.phase.is_terminal or delivery is None:
            return False
        if isinstance(delivery, PhaseDeliveryPlan):
            if delivery.status is PhaseDeliveryStatus.DELIVERED:
                return delivery.target_phase is self.state.phase
            if delivery.status is PhaseDeliveryStatus.ABANDONED:
                expected_phase = (
                    DebatePhase.FAILED
                    if delivery.target_phase is DebatePhase.COMPLETED
                    else delivery.target_phase
                )
                return expected_phase is self.state.phase
            return False
        return delivery.target_phase is self.state.phase and delivery.completed_at is not None

    def checkpoint_for(
        self,
        *,
        phase: DebatePhase,
        participant: ParticipantSlot,
    ) -> GenerationCheckpoint | None:
        """Return the one generation fence for a logical output, if present."""

        return next(
            (
                checkpoint
                for checkpoint in self.generation_checkpoints
                if checkpoint.phase is phase and checkpoint.participant is participant
            ),
            None,
        )

    def with_generation_checkpoint(
        self,
        checkpoint: GenerationCheckpoint,
    ) -> DebateSnapshot:
        """Insert or replace one phase/participant checkpoint deterministically."""

        return replace(
            self,
            generation_checkpoints=self.generation_checkpoints_with(checkpoint),
        )

    def generation_checkpoints_with(
        self,
        checkpoint: GenerationCheckpoint,
    ) -> tuple[GenerationCheckpoint, ...]:
        """Build an updated checkpoint collection for an atomic aggregate write."""

        retained = tuple(
            current
            for current in self.generation_checkpoints
            if (current.phase, current.participant) != (checkpoint.phase, checkpoint.participant)
        )
        return tuple(
            sorted(
                (*retained, checkpoint),
                key=lambda current: (current.phase.value, current.participant.value),
            )
        )

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
    TERMINAL_DELIVERY_CONFLICT_RETRY = "terminal_delivery_conflict_retry"
    PANEL_REFRESH_FAILED = "discord_panel_refresh_failed"
