"""SDK-independent ingress and scale-to-zero runtime contracts."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, unique

from shittim_chest.domain import AttemptId, DebateId

INGRESS_QUEUE_LIMIT = 20
INGRESS_CLAIM_SECONDS = 120
STATUS_PUBLICATION_CLAIM_SECONDS = 180
STARTUP_TIMEOUT = timedelta(minutes=3)
TERMINAL_TIMEOUT = timedelta(minutes=15)
IDLE_TIMEOUT = timedelta(minutes=30)
RUNTIME_PROMPT_REVISION_PATTERN = r"^r[0-9a-hjkmnp-tv-z]{26}$"


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


def _require_optional_utc(value: datetime | None, *, label: str) -> None:
    if value is not None:
        _require_utc(value, label=label)


def _require_canonical_snowflake(value: str, *, label: str) -> None:
    if (
        re.fullmatch(r"[0-9]{1,20}", value) is None
        or not 0 < int(value) < 2**64
        or str(int(value)) != value
    ):
        raise ValueError(f"{label} must be a canonical Discord snowflake")


@unique
class IngressKind(StrEnum):
    """Operations accepted through Discord's HTTP interaction endpoint."""

    NEW_DEBATE = "new_debate"
    RETRY = "retry"
    CANCEL = "cancel"


@unique
class IngressStatus(StrEnum):
    """Durable lifecycle of one HTTP interaction operation."""

    PENDING = "pending"
    CLAIMED = "claimed"
    RETRYING = "retrying"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def counts_toward_queue_limit(self) -> bool:
        """Return whether this state consumes one of the 20 FIFO entries."""

        return self in {
            IngressStatus.PENDING,
            IngressStatus.CLAIMED,
            IngressStatus.RETRYING,
        }

    @property
    def is_terminal(self) -> bool:
        """Return whether no further request processing is allowed."""

        return self in {
            IngressStatus.COMPLETED,
            IngressStatus.REJECTED,
            IngressStatus.FAILED,
        }


@unique
class StatusMessageState(StrEnum):
    """Public, content-free state selected by the status publisher."""

    PENDING = "pending"
    STARTING = "starting"
    READY = "ready"
    STARTUP_TIMEOUT = "startup_timeout"
    RECOVERED = "recovered"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TERMINAL_FAILED = "terminal_failed"


@unique
class StatusPublicationState(StrEnum):
    """Delivery lifecycle for one durable public status publication."""

    PREPARED = "prepared"
    CLAIMED = "claimed"
    RETRYING = "retrying"
    DELIVERED = "delivered"
    FAILED = "failed"

    @property
    def counts_as_pending(self) -> bool:
        """Return whether this publication prevents an idle runtime."""

        return self in {
            StatusPublicationState.PREPARED,
            StatusPublicationState.CLAIMED,
            StatusPublicationState.RETRYING,
        }


@unique
class RuntimeStatus(StrEnum):
    """Aggregate state of the singleton ECS runtime."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    STOPPING = "stopping"
    DEGRADED = "degraded"


_ALLOWED_RUNTIME_TRANSITIONS: dict[RuntimeStatus, frozenset[RuntimeStatus]] = {
    RuntimeStatus.STOPPED: frozenset({RuntimeStatus.STARTING}),
    RuntimeStatus.STARTING: frozenset(
        {
            RuntimeStatus.READY,
            RuntimeStatus.BUSY,
            RuntimeStatus.STOPPING,
            RuntimeStatus.DEGRADED,
        }
    ),
    RuntimeStatus.READY: frozenset({RuntimeStatus.BUSY, RuntimeStatus.IDLE}),
    RuntimeStatus.BUSY: frozenset({RuntimeStatus.IDLE, RuntimeStatus.DEGRADED}),
    RuntimeStatus.IDLE: frozenset(
        {RuntimeStatus.STARTING, RuntimeStatus.READY, RuntimeStatus.STOPPING}
    ),
    RuntimeStatus.STOPPING: frozenset({RuntimeStatus.STOPPED, RuntimeStatus.STARTING}),
    RuntimeStatus.DEGRADED: frozenset(
        {
            RuntimeStatus.STARTING,
            RuntimeStatus.READY,
            RuntimeStatus.BUSY,
            RuntimeStatus.IDLE,
            RuntimeStatus.STOPPING,
        }
    ),
}


@dataclass(frozen=True, slots=True, repr=False)
class IngressRequest:
    """One durable Discord interaction without its short-lived interaction token."""

    interaction_id: str
    operation_id: str
    kind: IngressKind
    application_id: str
    requester_id: str
    requester_username: str
    requester_display_name: str
    requester_can_manage_messages: bool
    guild_id: str
    channel_id: str
    status_channel_id: str
    status: IngressStatus
    status_message_state: StatusMessageState
    created_at: datetime
    updated_at: datetime
    startup_deadline_at: datetime
    terminal_deadline_at: datetime
    processing_started_at: datetime | None = None
    command_name: str | None = None
    custom_id: str | None = None
    question: str | None = None
    parent_channel_id: str | None = None
    source_message_id: str | None = None
    source_thread_id: str | None = None
    target_debate_id: DebateId | None = None
    expected_attempt_id: AttemptId | None = None
    status_message_id: str | None = None
    status_message_updated_at: datetime | None = None
    next_attempt_at: datetime | None = None
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    delivery_attempt: int = 0
    error_code: str | None = None
    error_detail_code: str | None = None
    accepted_debate_id: DebateId | None = None
    accepted_attempt_id: AttemptId | None = None
    completed_at: datetime | None = None
    ttl: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (
            ("interaction ID", self.interaction_id),
            ("operation ID", self.operation_id),
            ("application ID", self.application_id),
            ("requester ID", self.requester_id),
            ("requester username", self.requester_username),
            ("requester display name", self.requester_display_name),
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
            ("status channel ID", self.status_channel_id),
        ):
            _require_text(value, label=label)
        if not isinstance(self.requester_can_manage_messages, bool):
            raise ValueError("requester manage-messages permission must be a boolean")
        for label, value in (
            ("creation timestamp", self.created_at),
            ("update timestamp", self.updated_at),
            ("startup deadline", self.startup_deadline_at),
            ("terminal deadline", self.terminal_deadline_at),
        ):
            _require_utc(value, label=label)
        for label, value in (
            ("status message timestamp", self.status_message_updated_at),
            ("next attempt timestamp", self.next_attempt_at),
            ("claim expiry", self.claim_expires_at),
            ("completion timestamp", self.completed_at),
            ("processing start timestamp", self.processing_started_at),
        ):
            _require_optional_utc(value, label=label)
        if (self.status_message_id is None) is not (self.status_message_updated_at is None):
            raise ValueError("status message ID and timestamp must be set together")
        if self.updated_at < self.created_at:
            raise ValueError("request update cannot precede creation")
        if self.startup_deadline_at != self.created_at + STARTUP_TIMEOUT:
            raise ValueError("startup deadline must be exactly three minutes after creation")
        if self.terminal_deadline_at != self.created_at + TERMINAL_TIMEOUT:
            raise ValueError("terminal deadline must be exactly fifteen minutes after creation")
        if self.processing_started_at is not None:
            if not self.created_at <= self.processing_started_at < self.terminal_deadline_at:
                raise ValueError("processing must start before the terminal deadline")
            if self.status is IngressStatus.PENDING:
                raise ValueError("pending ingress cannot have started processing")
        if self.kind is IngressKind.NEW_DEBATE:
            if self.question is None or not 1 <= len(self.question) <= 1000:
                raise ValueError("new debate requires a 1-1000 character question")
            if not self.question.strip():
                raise ValueError("new debate question must not be blank")
        elif self.question is not None:
            raise ValueError("only a new debate may contain a question")
        if self.kind is IngressKind.NEW_DEBATE and self.command_name is None:
            raise ValueError("new debate requires a command name")
        if self.kind is IngressKind.NEW_DEBATE:
            if self.custom_id is not None:
                raise ValueError("new debate cannot contain a component custom ID")
            if self.requester_can_manage_messages:
                raise ValueError("new debate cannot persist manage-messages permission")
            if any(
                value is not None
                for value in (
                    self.parent_channel_id,
                    self.source_message_id,
                    self.source_thread_id,
                    self.target_debate_id,
                    self.expected_attempt_id,
                )
            ):
                raise ValueError("new debate cannot contain component context")
        else:
            if self.command_name is not None or self.question is not None:
                raise ValueError("control operation cannot contain command input")
            for label, value in (
                ("component custom ID", self.custom_id),
                ("parent channel ID", self.parent_channel_id),
                ("source message ID", self.source_message_id),
                ("source thread ID", self.source_thread_id),
            ):
                if value is None:
                    raise ValueError(f"control operation requires a {label}")
                _require_text(value, label=label)
            if self.target_debate_id is None or self.expected_attempt_id is None:
                raise ValueError("control operation requires target debate and attempt IDs")
        if (self.claim_owner is None) is not (self.claim_expires_at is None):
            raise ValueError("claim owner and expiry must be set together")
        if self.status is IngressStatus.CLAIMED and self.claim_owner is None:
            raise ValueError("claimed request requires a claim owner and expiry")
        if self.status is not IngressStatus.CLAIMED and self.claim_owner is not None:
            raise ValueError("only a claimed request may retain a claim")
        if isinstance(self.delivery_attempt, bool) or self.delivery_attempt < 0:
            raise ValueError("delivery attempt must be a non-negative integer")
        if (self.accepted_debate_id is None) is not (self.accepted_attempt_id is None):
            raise ValueError("accepted debate and attempt IDs must be set together")
        if self.status is IngressStatus.ACCEPTED and self.accepted_debate_id is None:
            raise ValueError("accepted request requires debate and attempt IDs")
        if self.status.is_terminal and self.completed_at is None:
            raise ValueError("terminal request requires a completion timestamp")
        if not self.status.is_terminal and self.completed_at is not None:
            raise ValueError("non-terminal request cannot have a completion timestamp")
        if self.ttl is not None and (isinstance(self.ttl, bool) or self.ttl < 0):
            raise ValueError("TTL must be a non-negative Unix timestamp")
        if self.schema_version != 1:
            raise ValueError("unsupported ingress schema version")

    @classmethod
    def new_debate(
        cls,
        *,
        interaction_id: str,
        operation_id: str,
        application_id: str,
        question: str,
        requester_id: str,
        requester_username: str,
        requester_display_name: str,
        guild_id: str,
        channel_id: str,
        command_name: str,
        created_at: datetime,
    ) -> IngressRequest:
        """Construct one pending command using per-request deadline anchors."""

        return cls(
            interaction_id=interaction_id,
            operation_id=operation_id,
            kind=IngressKind.NEW_DEBATE,
            application_id=application_id,
            requester_id=requester_id,
            requester_username=requester_username,
            requester_display_name=requester_display_name,
            requester_can_manage_messages=False,
            guild_id=guild_id,
            channel_id=channel_id,
            status_channel_id=channel_id,
            status=IngressStatus.PENDING,
            status_message_state=StatusMessageState.STARTING,
            created_at=created_at,
            updated_at=created_at,
            startup_deadline_at=created_at + STARTUP_TIMEOUT,
            terminal_deadline_at=created_at + TERMINAL_TIMEOUT,
            command_name=command_name,
            question=question,
        )

    @classmethod
    def control_operation(
        cls,
        *,
        interaction_id: str,
        operation_id: str,
        kind: IngressKind,
        application_id: str,
        requester_id: str,
        requester_username: str,
        requester_display_name: str,
        requester_can_manage_messages: bool,
        guild_id: str,
        channel_id: str,
        parent_channel_id: str,
        source_message_id: str,
        source_thread_id: str,
        target_debate_id: DebateId,
        expected_attempt_id: AttemptId,
        custom_id: str,
        created_at: datetime,
    ) -> IngressRequest:
        """Construct a pending retry or cancellation bound to immutable context."""

        if kind is IngressKind.NEW_DEBATE:
            raise ValueError("control operation kind must be retry or cancel")
        return cls(
            interaction_id=interaction_id,
            operation_id=operation_id,
            kind=kind,
            application_id=application_id,
            requester_id=requester_id,
            requester_username=requester_username,
            requester_display_name=requester_display_name,
            requester_can_manage_messages=requester_can_manage_messages,
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_channel_id,
            status_channel_id=channel_id,
            status=IngressStatus.PENDING,
            status_message_state=StatusMessageState.STARTING,
            created_at=created_at,
            updated_at=created_at,
            startup_deadline_at=created_at + STARTUP_TIMEOUT,
            terminal_deadline_at=created_at + TERMINAL_TIMEOUT,
            custom_id=custom_id,
            source_message_id=source_message_id,
            source_thread_id=source_thread_id,
            target_debate_id=target_debate_id,
            expected_attempt_id=expected_attempt_id,
        )


@dataclass(frozen=True, slots=True, repr=False)
class IngressClaimFence:
    """PII-free identity of one exact durable ingress claim generation."""

    interaction_id: str
    operation_id: str
    kind: IngressKind
    created_at: datetime
    terminal_deadline_at: datetime
    claim_owner: str
    claim_expires_at: datetime
    delivery_attempt: int
    write_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IngressKind):
            raise ValueError("ingress claim kind must be an IngressKind")
        for label, value in (
            ("interaction ID", self.interaction_id),
            ("operation ID", self.operation_id),
            ("claim owner", self.claim_owner),
        ):
            _require_text(value, label=label)
        for label, value in (
            ("creation timestamp", self.created_at),
            ("terminal deadline", self.terminal_deadline_at),
            ("claim expiry", self.claim_expires_at),
            ("write timestamp", self.write_at),
        ):
            _require_utc(value, label=label)
        if self.claim_expires_at <= self.created_at:
            raise ValueError("claim expiry must follow creation")
        if self.write_at < self.created_at:
            raise ValueError("claim write timestamp cannot precede creation")
        if self.terminal_deadline_at != self.created_at + TERMINAL_TIMEOUT:
            raise ValueError(
                "claim terminal deadline must be exactly fifteen minutes after creation"
            )
        if self.claim_expires_at <= self.write_at:
            raise ValueError("ingress claim must remain live at write time")
        if isinstance(self.delivery_attempt, bool) or self.delivery_attempt <= 0:
            raise ValueError("delivery attempt must be a positive integer")
        if self.schema_version != 1:
            raise ValueError("unsupported ingress claim schema version")

    @classmethod
    def from_claimed_request(
        cls,
        request: IngressRequest,
        *,
        claim_owner: str,
        write_at: datetime,
    ) -> IngressClaimFence:
        """Capture an already-validated request without copying user content."""

        if (
            request.status is not IngressStatus.CLAIMED
            or request.claim_owner != claim_owner
            or request.claim_expires_at is None
        ):
            raise ValueError("request is not owned by the supplied ingress claimant")
        return cls(
            interaction_id=request.interaction_id,
            operation_id=request.operation_id,
            kind=request.kind,
            created_at=request.created_at,
            terminal_deadline_at=request.terminal_deadline_at,
            claim_owner=claim_owner,
            claim_expires_at=request.claim_expires_at,
            delivery_attempt=request.delivery_attempt,
            write_at=write_at,
            schema_version=request.schema_version,
        )

    def for_write_at(self, write_at: datetime) -> IngressClaimFence:
        """Revalidate the same claim generation at a later mutation timestamp."""

        return replace(self, write_at=write_at)


@dataclass(frozen=True, slots=True, repr=False)
class IngressOperationResult:
    """Strongly consistent replay result for one Discord interaction."""

    operation_id: str
    interaction_id: str
    request_sort_key: str
    status: IngressStatus
    created_at: datetime
    updated_at: datetime
    accepted_debate_id: DebateId | None = None
    accepted_attempt_id: AttemptId | None = None
    error_code: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (
            ("operation ID", self.operation_id),
            ("interaction ID", self.interaction_id),
            ("request sort key", self.request_sort_key),
        ):
            _require_text(value, label=label)
        _require_utc(self.created_at, label="operation creation timestamp")
        _require_utc(self.updated_at, label="operation update timestamp")
        if self.updated_at < self.created_at:
            raise ValueError("operation update cannot precede creation")
        if (self.accepted_debate_id is None) is not (self.accepted_attempt_id is None):
            raise ValueError("operation debate and attempt IDs must be set together")
        if self.schema_version != 1:
            raise ValueError("unsupported ingress operation schema version")


@dataclass(frozen=True, slots=True, repr=False)
class IngressSemanticOperationBinding:
    """Bind a component's semantic operation to its first Interaction ID."""

    operation_id: str
    canonical_interaction_id: str
    request_sort_key: str
    created_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        for label, value in (
            ("operation ID", self.operation_id),
            ("canonical interaction ID", self.canonical_interaction_id),
            ("request sort key", self.request_sort_key),
        ):
            _require_text(value, label=label)
        _require_utc(self.created_at, label="semantic binding creation timestamp")
        if self.schema_version != 1:
            raise ValueError("unsupported ingress semantic binding schema version")


@dataclass(frozen=True, slots=True, repr=False)
class StatusHistoryCheckpoint:
    """Durable progress for baseline history and late-arrival gap scans."""

    history_verified_head_message_id: str
    history_cursor_message_id: str | None = None
    history_gap_cursor_message_id: str | None = None
    history_gap_upper_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_canonical_snowflake(
            self.history_verified_head_message_id,
            label="history verified-head message ID",
        )
        for label, value in (
            ("history cursor message ID", self.history_cursor_message_id),
            ("history gap cursor message ID", self.history_gap_cursor_message_id),
            ("history gap upper message ID", self.history_gap_upper_message_id),
        ):
            if value is not None:
                _require_canonical_snowflake(value, label=label)
        if (self.history_gap_cursor_message_id is None) is not (
            self.history_gap_upper_message_id is None
        ):
            raise ValueError("history gap cursor and upper message IDs must be set together")
        verified_head = int(self.history_verified_head_message_id)
        if (
            self.history_cursor_message_id is not None
            and int(self.history_cursor_message_id) > verified_head
        ):
            raise ValueError("history cursor cannot follow the verified head")
        if self.history_gap_cursor_message_id is not None:
            gap_cursor = int(self.history_gap_cursor_message_id)
            gap_upper = int(self.history_gap_upper_message_id or "0")
            if gap_cursor <= verified_head:
                raise ValueError("history gap cursor must follow the verified head")
            if gap_cursor > gap_upper:
                raise ValueError("history gap cursor cannot follow the gap upper bound")


@dataclass(frozen=True, slots=True, repr=False)
class IngressStatusPublication:
    """Durable desired and delivered state for one public status message."""

    canonical_interaction_id: str
    request_sort_key: str
    status_channel_id: str
    desired_state: StatusMessageState
    state: StatusPublicationState
    nonce: str
    content: str
    content_hash: str
    created_at: datetime
    updated_at: datetime
    delivered_state: StatusMessageState | None = None
    status_message_id: str | None = None
    status_message_updated_at: datetime | None = None
    history_checkpoint: StatusHistoryCheckpoint | None = None
    history_reconciliation_required: bool = False
    next_attempt_at: datetime | None = None
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    delivery_attempt: int = 0
    incarnation: int = 0
    error_code: str | None = None
    schema_version: int = 3

    def __post_init__(self) -> None:
        for label, value in (
            ("canonical interaction ID", self.canonical_interaction_id),
            ("request sort key", self.request_sort_key),
            ("status channel ID", self.status_channel_id),
        ):
            _require_text(value, label=label)
        if re.fullmatch(r"[A-Za-z0-9_-]{22}", self.nonce) is None:
            raise ValueError("status publication nonce must be 22 base64url characters")
        _require_text(self.content, label="status publication content")
        if len(self.content) > 2_000:
            raise ValueError("status publication content must be at most 2000 characters")
        expected_content_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash != expected_content_hash:
            raise ValueError("status publication content hash does not match its content")
        for label, value in (
            ("creation timestamp", self.created_at),
            ("update timestamp", self.updated_at),
        ):
            _require_utc(value, label=f"status publication {label}")
        for label, value in (
            ("status message timestamp", self.status_message_updated_at),
            ("next attempt timestamp", self.next_attempt_at),
            ("claim expiry", self.claim_expires_at),
        ):
            _require_optional_utc(value, label=f"status publication {label}")
        if (self.status_message_id is None) is not (self.status_message_updated_at is None):
            raise ValueError("status publication message ID and timestamp must be set together")
        if not isinstance(self.history_reconciliation_required, bool):
            raise ValueError("status publication history reconciliation flag must be a boolean")
        if self.status_message_id is not None and self.history_reconciliation_required:
            raise ValueError("known status message cannot require history reconciliation")
        if self.history_checkpoint is not None:
            if not self.history_reconciliation_required or self.status_message_id is not None:
                raise ValueError("history checkpoint requires an unresolved status message scan")
            _require_canonical_snowflake(
                self.canonical_interaction_id,
                label="history checkpoint interaction ID",
            )
            interaction_id = int(self.canonical_interaction_id)
            verified_head = int(self.history_checkpoint.history_verified_head_message_id)
            if verified_head <= interaction_id:
                raise ValueError("history verified head must follow the interaction")
            history_cursor = self.history_checkpoint.history_cursor_message_id
            if history_cursor is not None and int(history_cursor) <= interaction_id:
                raise ValueError("history cursor must follow the interaction")
        if self.updated_at < self.created_at:
            raise ValueError("status publication update cannot precede creation")
        if (self.claim_owner is None) is not (self.claim_expires_at is None):
            raise ValueError("status publication claim owner and expiry must be set together")
        if self.state is StatusPublicationState.CLAIMED and self.claim_owner is None:
            raise ValueError("claimed status publication requires a claim")
        if self.state is not StatusPublicationState.CLAIMED and self.claim_owner is not None:
            raise ValueError("only a claimed status publication may retain a claim")
        if (
            self.state
            in {
                StatusPublicationState.PREPARED,
                StatusPublicationState.RETRYING,
            }
            and self.next_attempt_at is None
        ):
            raise ValueError("due status publication requires a next attempt timestamp")
        if self.state is StatusPublicationState.CLAIMED and self.next_attempt_at is not None:
            raise ValueError("claimed status publication cannot retain a next attempt timestamp")
        if not self.state.counts_as_pending and self.next_attempt_at is not None:
            raise ValueError("settled status publication cannot retain a next attempt timestamp")
        if self.state is StatusPublicationState.DELIVERED:
            if self.delivered_state is not self.desired_state:
                raise ValueError("delivered status publication must match desired state")
            if self.status_message_id is None or self.status_message_updated_at is None:
                raise ValueError("delivered status publication requires message metadata")
        for label, value in (
            ("delivery attempt", self.delivery_attempt),
            ("incarnation", self.incarnation),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"status publication {label} must be non-negative")
        if self.nonce != status_publication_nonce(
            self.canonical_interaction_id,
            incarnation=self.incarnation,
        ):
            raise ValueError("status publication nonce does not match its incarnation")
        if self.schema_version != 3:
            raise ValueError("unsupported ingress status publication schema version")

    @classmethod
    def prepared(
        cls,
        request: IngressRequest,
        *,
        content: str,
    ) -> IngressStatusPublication:
        """Prepare one runtime-aware initial publication without a Discord token."""

        if request.status_message_state not in {
            StatusMessageState.PENDING,
            StatusMessageState.STARTING,
            StatusMessageState.READY,
        }:
            raise ValueError(
                "new ingress status publication must start in PENDING, STARTING, or READY"
            )
        return cls(
            canonical_interaction_id=request.interaction_id,
            request_sort_key=(
                "REQUEST#"
                f"{request.created_at.isoformat(timespec='microseconds').replace('+00:00', 'Z')}#"
                f"{request.interaction_id}"
            ),
            status_channel_id=request.status_channel_id,
            desired_state=request.status_message_state,
            state=StatusPublicationState.PREPARED,
            nonce=status_publication_nonce(request.interaction_id, incarnation=0),
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            created_at=request.created_at,
            updated_at=request.created_at,
            next_attempt_at=request.created_at,
        )


@dataclass(frozen=True, slots=True, repr=False)
class StatusPublicationWork:
    """Strongly read request and publication claimed as one delivery unit."""

    request: IngressRequest
    publication: IngressStatusPublication

    def __post_init__(self) -> None:
        if self.request.interaction_id != self.publication.canonical_interaction_id:
            raise ValueError("status publication belongs to another request")
        expected_sort_key = (
            "REQUEST#"
            f"{self.request.created_at.isoformat(timespec='microseconds').replace('+00:00', 'Z')}#"
            f"{self.request.interaction_id}"
        )
        if self.publication.request_sort_key != expected_sort_key:
            raise ValueError("status publication request sort key is inconsistent")
        if self.request.status_channel_id != self.publication.status_channel_id:
            raise ValueError("status publication channel is inconsistent")
        if self.request.status_message_state is not self.publication.desired_state:
            raise ValueError("status publication desired state is inconsistent")
        if self.request.status_message_id != self.publication.status_message_id:
            raise ValueError("status publication message ID is inconsistent")
        if self.request.status_message_updated_at != self.publication.status_message_updated_at:
            raise ValueError("status publication message timestamp is inconsistent")


def status_publication_nonce(
    canonical_interaction_id: str,
    *,
    incarnation: int = 0,
) -> str:
    """Return a deterministic Discord-safe 128-bit nonce for one publication."""

    _require_text(canonical_interaction_id, label="canonical interaction ID")
    if isinstance(incarnation, bool) or not isinstance(incarnation, int) or incarnation < 0:
        raise ValueError("status publication incarnation must be a non-negative integer")
    digest = hashlib.sha256(f"status:{canonical_interaction_id}:{incarnation}".encode()).digest()[
        :16
    ]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True, slots=True, repr=False)
class EnqueuedIngress:
    """Return one persisted request and whether this call created it."""

    request: IngressRequest
    operation: IngressOperationResult
    created: bool


@dataclass(frozen=True, slots=True, repr=False)
class IngressWakeCandidate:
    """PII-free identity and deadline state used by the runtime reconciler."""

    interaction_id: str
    status: IngressStatus
    status_message_state: StatusMessageState
    created_at: datetime
    terminal_deadline_at: datetime
    next_attempt_at: datetime | None = None
    claim_expires_at: datetime | None = None
    processing_started_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.interaction_id, label="interaction ID")
        _require_utc(self.created_at, label="creation timestamp")
        _require_utc(self.terminal_deadline_at, label="terminal deadline")
        for label, value in (
            ("next attempt timestamp", self.next_attempt_at),
            ("claim expiry", self.claim_expires_at),
            ("processing start timestamp", self.processing_started_at),
        ):
            _require_optional_utc(value, label=label)
        if self.terminal_deadline_at != self.created_at + TERMINAL_TIMEOUT:
            raise ValueError("terminal deadline must be exactly fifteen minutes after creation")
        if not self.status.counts_toward_queue_limit:
            raise ValueError("only active ingress may become a wake candidate")
        if (self.status is IngressStatus.CLAIMED) is not (self.claim_expires_at is not None):
            raise ValueError("wake candidate claim state and expiry must match")
        if self.status is not IngressStatus.RETRYING and self.next_attempt_at is not None:
            raise ValueError("only a retrying wake candidate may have a next attempt")
        if self.processing_started_at is not None and not (
            self.created_at <= self.processing_started_at < self.terminal_deadline_at
        ):
            raise ValueError("processing must start before the terminal deadline")

    @classmethod
    def from_request(cls, request: IngressRequest) -> IngressWakeCandidate:
        """Drop all user content while preserving reconciliation facts."""

        if not request.status.counts_toward_queue_limit:
            raise ValueError("only active ingress may become a wake candidate")
        return cls(
            interaction_id=request.interaction_id,
            status=request.status,
            status_message_state=request.status_message_state,
            created_at=request.created_at,
            terminal_deadline_at=request.terminal_deadline_at,
            next_attempt_at=request.next_attempt_at,
            claim_expires_at=request.claim_expires_at,
            processing_started_at=request.processing_started_at,
        )


@dataclass(frozen=True, slots=True)
class RuntimeActivity:
    """Complete activity snapshot used to decide whether IDLE is safe."""

    pending_ingress: int = 0
    claimed_ingress: int = 0
    retrying_ingress: int = 0
    active_attempts: int = 0
    application_tasks: int = 0
    active_leases: int = 0
    recovery_tasks: int = 0
    pending_outbox: int = 0
    claimed_outbox: int = 0
    pending_status_updates: int = 0
    pending_panel_refreshes: int = 0
    checkpoint_tasks: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.pending_ingress,
            self.claimed_ingress,
            self.retrying_ingress,
            self.active_attempts,
            self.application_tasks,
            self.active_leases,
            self.recovery_tasks,
            self.pending_outbox,
            self.claimed_outbox,
            self.pending_status_updates,
            self.pending_panel_refreshes,
            self.checkpoint_tasks,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("runtime activity counts must be non-negative integers")

    @property
    def is_complete(self) -> bool:
        """Return true only when every durable and process-owned activity is clear."""

        return not any(
            (
                self.pending_ingress,
                self.claimed_ingress,
                self.retrying_ingress,
                self.active_attempts,
                self.application_tasks,
                self.active_leases,
                self.recovery_tasks,
                self.pending_outbox,
                self.claimed_outbox,
                self.pending_status_updates,
                self.pending_panel_refreshes,
                self.checkpoint_tasks,
            )
        )

    @property
    def requires_runtime(self) -> bool:
        """Return whether durable work requires the ECS/Discord process.

        Public status publication is owned by Lambda and therefore blocks the
        transition into IDLE without becoming a reason to start Fargate.
        """

        return any(
            (
                self.pending_ingress,
                self.claimed_ingress,
                self.retrying_ingress,
                self.active_attempts,
                self.application_tasks,
                self.active_leases,
                self.recovery_tasks,
                self.pending_outbox,
                self.claimed_outbox,
                self.pending_panel_refreshes,
                self.checkpoint_tasks,
            )
        )


@dataclass(frozen=True, slots=True)
class OutboxActivity:
    """Strongly consistent global Discord Outbox activity counts."""

    pending: int = 0
    claimed: int = 0

    def __post_init__(self) -> None:
        for value in (self.pending, self.claimed):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("outbox activity counts must be non-negative integers")

    @property
    def is_complete(self) -> bool:
        """Return whether every persisted Discord operation is SENT."""

        return self.pending == 0 and self.claimed == 0


@dataclass(frozen=True, slots=True)
class EcsRuntimeSnapshot:
    """SDK-neutral view of the singleton ECS service."""

    desired_count: int
    running_count: int
    pending_count: int

    def __post_init__(self) -> None:
        for value in (self.desired_count, self.running_count, self.pending_count):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1:
                raise ValueError("singleton ECS counts must be either zero or one")
        if self.running_count + self.pending_count > 1:
            raise ValueError("singleton ECS service cannot have more than one active task")


@dataclass(frozen=True, slots=True)
class RuntimeWakeResult:
    """Immutable record binding one interaction to its assigned generation."""

    interaction_id: str
    generation: int
    runtime_version: int
    recorded_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.interaction_id, label="interaction ID")
        for label, value in (
            ("runtime generation", self.generation),
            ("runtime version", self.runtime_version),
        ):
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        _require_utc(self.recorded_at, label="wake result timestamp")
        if self.schema_version != 1:
            raise ValueError("unsupported runtime wake result schema version")


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Versioned aggregate control state, not a replacement for debate ownership."""

    status: RuntimeStatus
    generation: int
    desired_count: int
    version: int
    updated_at: datetime
    runtime_instance_id: str | None = None
    runtime_prompt_revision: str | None = None
    wake_started_at: datetime | None = None
    last_request_at: datetime | None = None
    started_at: datetime | None = None
    ready_at: datetime | None = None
    busy_since: datetime | None = None
    idle_since: datetime | None = None
    stop_eligible_at: datetime | None = None
    stopping_at: datetime | None = None
    stopped_at: datetime | None = None
    last_error_code: str | None = None
    last_reconciled_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("runtime generation must be a non-negative integer")
        if self.desired_count not in {0, 1} or isinstance(self.desired_count, bool):
            raise ValueError("runtime desired count must be zero or one")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("runtime version must be a non-negative integer")
        if self.runtime_instance_id is not None:
            _require_text(self.runtime_instance_id, label="runtime instance ID")
        if self.runtime_prompt_revision is not None:
            if re.fullmatch(RUNTIME_PROMPT_REVISION_PATTERN, self.runtime_prompt_revision) is None:
                raise ValueError("runtime prompt revision is invalid")
            if self.runtime_instance_id is None:
                raise ValueError("runtime prompt revision requires a bound runtime instance")
        if self.last_error_code is not None:
            _require_text(self.last_error_code, label="runtime error code")
        _require_utc(self.updated_at, label="runtime update timestamp")
        for label, value in (
            ("wake start", self.wake_started_at),
            ("last request", self.last_request_at),
            ("runtime start", self.started_at),
            ("runtime ready", self.ready_at),
            ("runtime busy", self.busy_since),
            ("runtime idle", self.idle_since),
            ("stop eligibility", self.stop_eligible_at),
            ("runtime stopping", self.stopping_at),
            ("runtime stopped", self.stopped_at),
            ("last reconciliation", self.last_reconciled_at),
        ):
            _require_optional_utc(value, label=label)
        if (self.idle_since is None) is not (self.stop_eligible_at is None):
            raise ValueError("idle timestamp and stop eligibility must be set together")
        if self.idle_since is not None and self.stop_eligible_at != self.idle_since + IDLE_TIMEOUT:
            raise ValueError("stop eligibility must be exactly thirty minutes after idle")
        if self.status is RuntimeStatus.IDLE and self.idle_since is None:
            raise ValueError("IDLE state requires fixed idle timestamps")
        if (
            self.status not in {RuntimeStatus.IDLE, RuntimeStatus.STOPPING}
            and self.idle_since is not None
        ):
            raise ValueError("only IDLE or STOPPING state may retain idle timestamps")
        if self.status is RuntimeStatus.STOPPING and self.stopping_at is None:
            raise ValueError("STOPPING state requires a stopping timestamp")
        if self.status is RuntimeStatus.STOPPED:
            if self.desired_count != 0 or self.stopped_at is None:
                raise ValueError("STOPPED state requires desired zero and a stopped timestamp")
            if self.runtime_instance_id is not None:
                raise ValueError("STOPPED state cannot retain a runtime instance")
        if (
            self.status
            in {
                RuntimeStatus.STARTING,
                RuntimeStatus.READY,
                RuntimeStatus.BUSY,
                RuntimeStatus.IDLE,
            }
            and self.desired_count != 1
        ):
            raise ValueError("active runtime state requires desired count one")
        if self.status is RuntimeStatus.STARTING and self.wake_started_at is None:
            raise ValueError("STARTING state requires a wake timestamp")
        if self.status in {RuntimeStatus.READY, RuntimeStatus.BUSY, RuntimeStatus.IDLE}:
            if self.runtime_instance_id is None:
                raise ValueError("ready runtime state requires a runtime instance")
            if self.started_at is None:
                raise ValueError("ready runtime state requires a start timestamp")
            if self.ready_at is None:
                raise ValueError("ready runtime state requires a ready timestamp")
        if self.status is RuntimeStatus.BUSY and self.busy_since is None:
            raise ValueError("BUSY state requires a busy timestamp")
        if self.status is RuntimeStatus.STOPPING and self.desired_count != 0:
            raise ValueError("STOPPING state requires desired count zero")
        if self.status is RuntimeStatus.DEGRADED and self.last_error_code is None:
            raise ValueError("DEGRADED state requires an error code")
        if self.schema_version != 1:
            raise ValueError("unsupported runtime state schema version")

    @classmethod
    def stopped(cls, *, at: datetime) -> RuntimeState:
        """Construct the initial desired-zero runtime state."""

        return cls(
            status=RuntimeStatus.STOPPED,
            generation=0,
            desired_count=0,
            version=0,
            updated_at=at,
            stopped_at=at,
        )

    def request_wake(self, *, at: datetime) -> RuntimeState:
        """Apply one new non-duplicate request and monotonically advance generation."""

        _require_utc(at, label="wake timestamp")
        if at < self.updated_at:
            raise ValueError("wake timestamp cannot precede runtime update")
        next_status = self.status
        wake_started_at = self.wake_started_at
        if self.status in {
            RuntimeStatus.STOPPED,
            RuntimeStatus.IDLE,
            RuntimeStatus.STOPPING,
            RuntimeStatus.DEGRADED,
        }:
            next_status = RuntimeStatus.STARTING
            wake_started_at = at
        elif self.status is RuntimeStatus.STARTING and wake_started_at is None:
            wake_started_at = at
        restarting = next_status is RuntimeStatus.STARTING
        requires_fresh_instance = restarting and self.status is not RuntimeStatus.STARTING
        return replace(
            self,
            status=next_status,
            generation=self.generation + 1,
            desired_count=1,
            version=self.version + 1,
            updated_at=at,
            wake_started_at=wake_started_at,
            last_request_at=at,
            idle_since=None,
            stop_eligible_at=None,
            stopping_at=None,
            stopped_at=None,
            last_error_code=None,
            ready_at=None if restarting else self.ready_at,
            busy_since=None if restarting else self.busy_since,
            runtime_instance_id=None if requires_fresh_instance else self.runtime_instance_id,
            runtime_prompt_revision=(
                None if requires_fresh_instance else self.runtime_prompt_revision
            ),
            started_at=None if requires_fresh_instance else self.started_at,
        )

    def mark_started(
        self,
        *,
        at: datetime,
        runtime_instance_id: str,
        runtime_prompt_revision: str | None = None,
    ) -> RuntimeState:
        """Bind the STARTING generation to one physical runtime instance."""

        _require_utc(at, label="runtime start timestamp")
        _require_text(runtime_instance_id, label="runtime instance ID")
        if (
            runtime_prompt_revision is not None
            and re.fullmatch(
                RUNTIME_PROMPT_REVISION_PATTERN,
                runtime_prompt_revision,
            )
            is None
        ):
            raise ValueError("runtime prompt revision is invalid")
        if self.status is not RuntimeStatus.STARTING:
            raise ValueError("only STARTING runtime may bind an instance")
        if at < self.updated_at:
            raise ValueError("runtime start timestamp cannot precede runtime update")
        if self.runtime_instance_id not in {None, runtime_instance_id}:
            raise ValueError("runtime generation is already bound to another instance")
        if (
            self.runtime_instance_id == runtime_instance_id
            and self.started_at is not None
            and self.runtime_prompt_revision != runtime_prompt_revision
        ):
            raise ValueError("bound runtime prompt revision cannot change")
        return replace(
            self,
            runtime_instance_id=runtime_instance_id,
            runtime_prompt_revision=runtime_prompt_revision,
            started_at=self.started_at or at,
            version=self.version + 1,
            updated_at=at,
        )

    def fence_stale_instance(self, *, at: datetime) -> RuntimeState:
        """Fence a bound owner proven stale by ECS or a replacement task."""

        _require_utc(at, label="runtime repair timestamp")
        bound_starting = (
            self.status is RuntimeStatus.STARTING and self.runtime_instance_id is not None
        )
        if self.status not in {RuntimeStatus.READY, RuntimeStatus.BUSY} and not bound_starting:
            raise ValueError("only a stale bound runtime may be repaired")
        if at < self.updated_at:
            raise ValueError("runtime repair timestamp cannot precede runtime update")
        return replace(
            self,
            status=RuntimeStatus.STARTING,
            generation=self.generation + 1,
            desired_count=1,
            version=self.version + 1,
            updated_at=at,
            runtime_instance_id=None,
            runtime_prompt_revision=None,
            wake_started_at=at,
            started_at=None,
            ready_at=None,
            busy_since=None,
            idle_since=None,
            stop_eligible_at=None,
            stopping_at=None,
            stopped_at=None,
            last_error_code=None,
        )

    def resume_for_work(self, *, at: datetime) -> RuntimeState:
        """Start a fresh generation for durable work not tied to a new request."""

        _require_utc(at, label="runtime work timestamp")
        if at < self.updated_at:
            raise ValueError("runtime work timestamp cannot precede runtime update")
        if self.status in {RuntimeStatus.STARTING, RuntimeStatus.READY, RuntimeStatus.BUSY}:
            return self
        if self.status not in {
            RuntimeStatus.STOPPED,
            RuntimeStatus.IDLE,
            RuntimeStatus.STOPPING,
            RuntimeStatus.DEGRADED,
        }:
            raise ValueError("runtime state cannot resume durable work")
        return replace(
            self,
            status=RuntimeStatus.STARTING,
            generation=self.generation + 1,
            desired_count=1,
            version=self.version + 1,
            updated_at=at,
            runtime_instance_id=None,
            runtime_prompt_revision=None,
            wake_started_at=at,
            started_at=None,
            ready_at=None,
            busy_since=None,
            idle_since=None,
            stop_eligible_at=None,
            stopping_at=None,
            stopped_at=None,
            last_error_code=None,
        )

    def transition(
        self,
        next_status: RuntimeStatus,
        *,
        at: datetime,
        runtime_instance_id: str | None = None,
        error_code: str | None = None,
    ) -> RuntimeState:
        """Apply only an explicitly allowed runtime-state transition."""

        _require_utc(at, label="transition timestamp")
        if at < self.updated_at:
            raise ValueError("transition timestamp cannot precede runtime update")
        if next_status not in _ALLOWED_RUNTIME_TRANSITIONS[self.status]:
            raise ValueError(f"invalid runtime transition: {self.status} -> {next_status}")
        if runtime_instance_id is not None:
            _require_text(runtime_instance_id, label="runtime instance ID")
        if (
            runtime_instance_id is not None
            and self.runtime_instance_id is not None
            and runtime_instance_id != self.runtime_instance_id
        ):
            raise ValueError("runtime transition belongs to another instance")
        if next_status is RuntimeStatus.DEGRADED:
            if error_code is None:
                raise ValueError("DEGRADED transition requires an error code")
            _require_text(error_code, label="runtime error code")
        values: dict[str, object] = {
            "status": next_status,
            "version": self.version + 1,
            "updated_at": at,
            "runtime_instance_id": runtime_instance_id or self.runtime_instance_id,
            "idle_since": None,
            "stop_eligible_at": None,
            "last_error_code": error_code,
        }
        if next_status is RuntimeStatus.READY:
            if values["runtime_instance_id"] is None:
                raise ValueError("READY transition requires a runtime instance")
            values["started_at"] = self.started_at or at
            values["ready_at"] = at
        elif next_status is RuntimeStatus.BUSY:
            if values["runtime_instance_id"] is None:
                raise ValueError("BUSY transition requires a runtime instance")
            values["started_at"] = self.started_at or at
            values["ready_at"] = self.ready_at or at
            values["busy_since"] = at
        elif next_status is RuntimeStatus.STARTING:
            values["desired_count"] = 1
            values["wake_started_at"] = at
            values["stopping_at"] = None
            values["stopped_at"] = None
            values["runtime_instance_id"] = None
            values["runtime_prompt_revision"] = None
            values["started_at"] = None
            values["ready_at"] = None
            values["busy_since"] = None
        elif next_status is RuntimeStatus.STOPPING:
            values["desired_count"] = 0
            values["stopping_at"] = at
            if self.status is RuntimeStatus.IDLE:
                values["idle_since"] = self.idle_since
                values["stop_eligible_at"] = self.stop_eligible_at
        elif next_status is RuntimeStatus.STOPPED:
            values["desired_count"] = 0
            values["stopped_at"] = at
            values["runtime_instance_id"] = None
            values["runtime_prompt_revision"] = None
            values["stopping_at"] = None
        return replace(self, **values)

    def begin_idle(self, *, at: datetime) -> RuntimeState:
        """Enter IDLE once and preserve the original timestamps on repeated polls."""

        _require_utc(at, label="idle timestamp")
        if self.status is RuntimeStatus.IDLE:
            return self
        if RuntimeStatus.IDLE not in _ALLOWED_RUNTIME_TRANSITIONS[self.status]:
            raise ValueError(f"invalid runtime transition: {self.status} -> idle")
        if at < self.updated_at:
            raise ValueError("idle timestamp cannot precede runtime update")
        return replace(
            self,
            status=RuntimeStatus.IDLE,
            version=self.version + 1,
            updated_at=at,
            idle_since=at,
            stop_eligible_at=at + IDLE_TIMEOUT,
            busy_since=None,
            last_error_code=None,
        )

    def leave_idle_for_external_work(self, *, at: datetime) -> RuntimeState:
        """Invalidate the idle timer without starting a new ECS generation.

        Lambda-owned status publication does not require Fargate, but it still
        means the runtime is not completely idle.  The physical task remains
        bound and READY while the external work drains; a later ``begin_idle``
        starts a fresh thirty-minute interval.
        """

        _require_utc(at, label="idle interruption timestamp")
        if self.status is not RuntimeStatus.IDLE:
            raise ValueError("only IDLE runtime may leave idle for external work")
        if at < self.updated_at:
            raise ValueError("idle interruption timestamp cannot precede runtime update")
        return replace(
            self,
            status=RuntimeStatus.READY,
            version=self.version + 1,
            updated_at=at,
            idle_since=None,
            stop_eligible_at=None,
        )

    def begin_idle_stop(self, *, at: datetime) -> RuntimeState:
        """Enter STOPPING only after the fixed thirty-minute IDLE deadline."""

        _require_utc(at, label="runtime stop timestamp")
        if self.status is not RuntimeStatus.IDLE:
            raise ValueError("only IDLE runtime may begin an idle stop")
        if self.stop_eligible_at is None or at < self.stop_eligible_at:
            raise ValueError("runtime is not yet eligible to stop")
        return self.transition(RuntimeStatus.STOPPING, at=at)

    def begin_unneeded_start_stop(self, *, at: datetime) -> RuntimeState:
        """Stop an unneeded STARTING or non-ready DEGRADED runtime."""

        _require_utc(at, label="runtime stop timestamp")
        if self.status not in {RuntimeStatus.STARTING, RuntimeStatus.DEGRADED}:
            raise ValueError("only STARTING or DEGRADED runtime may stop as unneeded")
        return self.transition(RuntimeStatus.STOPPING, at=at)

    def record_reconciled(self, *, at: datetime) -> RuntimeState:
        """Record a successful reconciliation without changing generation or state."""

        _require_utc(at, label="reconciliation timestamp")
        if at < self.updated_at:
            raise ValueError("reconciliation timestamp cannot precede runtime update")
        return replace(
            self,
            version=self.version + 1,
            updated_at=at,
            last_reconciled_at=at,
        )

    def validate_replacement(self, updated: RuntimeState) -> None:
        """Validate one non-wake successor before a conditional persistence write."""

        if updated == self:
            return
        if updated.schema_version != self.schema_version:
            raise ValueError("runtime replacement cannot change schema version")
        if updated.generation != self.generation:
            repairable_runtime = self.status in {RuntimeStatus.READY, RuntimeStatus.BUSY} or (
                self.status is RuntimeStatus.STARTING and self.runtime_instance_id is not None
            )
            is_missing_task_repair = (
                repairable_runtime
                and updated.generation == self.generation + 1
                and updated == self.fence_stale_instance(at=updated.updated_at)
            )
            if is_missing_task_repair:
                return
            is_durable_work_resume = (
                self.status
                in {
                    RuntimeStatus.STOPPED,
                    RuntimeStatus.IDLE,
                    RuntimeStatus.STOPPING,
                    RuntimeStatus.DEGRADED,
                }
                and updated.generation == self.generation + 1
                and updated == self.resume_for_work(at=updated.updated_at)
            )
            if is_durable_work_resume:
                return
            raise ValueError("only wake or missing-task repair may change runtime generation")
        if updated.version != self.version + 1:
            raise ValueError("runtime replacement must increment version exactly once")
        if updated.updated_at < self.updated_at:
            raise ValueError("runtime replacement timestamp cannot move backwards")
        if (
            updated.status is not self.status
            and updated.status not in _ALLOWED_RUNTIME_TRANSITIONS[self.status]
        ):
            raise ValueError(f"invalid runtime transition: {self.status} -> {updated.status}")

    def may_stop(
        self,
        *,
        at: datetime,
        expected_generation: int,
        activity: RuntimeActivity,
    ) -> bool:
        """Check the time, generation, and complete-activity stop fence."""

        _require_utc(at, label="stop check timestamp")
        return (
            self.status is RuntimeStatus.IDLE
            and self.generation == expected_generation
            and self.stop_eligible_at is not None
            and at >= self.stop_eligible_at
            and activity.is_complete
        )
