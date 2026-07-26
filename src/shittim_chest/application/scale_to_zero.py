"""SDK-independent ingress and scale-to-zero runtime contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, unique

from shittim_chest.domain import AttemptId, DebateId

INGRESS_QUEUE_LIMIT = 20
INGRESS_CLAIM_SECONDS = 120
STARTUP_TIMEOUT = timedelta(minutes=3)
TERMINAL_TIMEOUT = timedelta(minutes=15)
IDLE_TIMEOUT = timedelta(minutes=30)


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


def _require_optional_utc(value: datetime | None, *, label: str) -> None:
    if value is not None:
        _require_utc(value, label=label)


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
    FAILED = "failed"


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
        {RuntimeStatus.READY, RuntimeStatus.BUSY, RuntimeStatus.DEGRADED}
    ),
    RuntimeStatus.READY: frozenset({RuntimeStatus.BUSY, RuntimeStatus.IDLE}),
    RuntimeStatus.BUSY: frozenset({RuntimeStatus.IDLE, RuntimeStatus.DEGRADED}),
    RuntimeStatus.IDLE: frozenset({RuntimeStatus.STARTING, RuntimeStatus.STOPPING}),
    RuntimeStatus.STOPPING: frozenset({RuntimeStatus.STOPPED, RuntimeStatus.STARTING}),
    RuntimeStatus.DEGRADED: frozenset(
        {
            RuntimeStatus.STARTING,
            RuntimeStatus.READY,
            RuntimeStatus.BUSY,
            RuntimeStatus.IDLE,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class IngressRequest:
    """One durable Discord interaction without its short-lived interaction token."""

    interaction_id: str
    operation_id: str
    kind: IngressKind
    requester_id: str
    requester_username: str
    requester_display_name: str
    guild_id: str
    channel_id: str
    status_channel_id: str
    status: IngressStatus
    status_message_state: StatusMessageState
    created_at: datetime
    updated_at: datetime
    startup_deadline_at: datetime
    terminal_deadline_at: datetime
    command_name: str | None = None
    custom_id: str | None = None
    question: str | None = None
    source_message_id: str | None = None
    source_thread_id: str | None = None
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
            ("requester ID", self.requester_id),
            ("requester username", self.requester_username),
            ("requester display name", self.requester_display_name),
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
            ("status channel ID", self.status_channel_id),
        ):
            _require_text(value, label=label)
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
        ):
            _require_optional_utc(value, label=label)
        if self.updated_at < self.created_at:
            raise ValueError("request update cannot precede creation")
        if self.startup_deadline_at != self.created_at + STARTUP_TIMEOUT:
            raise ValueError("startup deadline must be exactly three minutes after creation")
        if self.terminal_deadline_at != self.created_at + TERMINAL_TIMEOUT:
            raise ValueError("terminal deadline must be exactly fifteen minutes after creation")
        if self.kind is IngressKind.NEW_DEBATE:
            if self.question is None or not 1 <= len(self.question) <= 1000:
                raise ValueError("new debate requires a 1-1000 character question")
            if not self.question.strip():
                raise ValueError("new debate question must not be blank")
        elif self.question is not None:
            raise ValueError("only a new debate may contain a question")
        if self.kind is IngressKind.NEW_DEBATE and self.command_name is None:
            raise ValueError("new debate requires a command name")
        if self.kind is not IngressKind.NEW_DEBATE and self.custom_id is None:
            raise ValueError("control operation requires a custom ID")
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
            requester_id=requester_id,
            requester_username=requester_username,
            requester_display_name=requester_display_name,
            guild_id=guild_id,
            channel_id=channel_id,
            status_channel_id=channel_id,
            status=IngressStatus.PENDING,
            status_message_state=StatusMessageState.PENDING,
            created_at=created_at,
            updated_at=created_at,
            startup_deadline_at=created_at + STARTUP_TIMEOUT,
            terminal_deadline_at=created_at + TERMINAL_TIMEOUT,
            command_name=command_name,
            question=question,
        )


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class EnqueuedIngress:
    """Return one persisted request and whether this call created it."""

    request: IngressRequest
    operation: IngressOperationResult
    created: bool


@dataclass(frozen=True, slots=True)
class RuntimeActivity:
    """Complete activity snapshot used to decide whether IDLE is safe."""

    pending_ingress: int = 0
    claimed_ingress: int = 0
    retrying_ingress: int = 0
    application_tasks: int = 0
    active_leases: int = 0
    recovery_tasks: int = 0
    pending_outbox: int = 0
    claimed_outbox: int = 0
    pending_status_updates: int = 0
    checkpoint_tasks: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.pending_ingress,
            self.claimed_ingress,
            self.retrying_ingress,
            self.application_tasks,
            self.active_leases,
            self.recovery_tasks,
            self.pending_outbox,
            self.claimed_outbox,
            self.pending_status_updates,
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
                self.application_tasks,
                self.active_leases,
                self.recovery_tasks,
                self.pending_outbox,
                self.claimed_outbox,
                self.pending_status_updates,
                self.checkpoint_tasks,
            )
        )


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


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Versioned aggregate control state, not a replacement for debate ownership."""

    status: RuntimeStatus
    generation: int
    desired_count: int
    version: int
    updated_at: datetime
    runtime_instance_id: str | None = None
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
        if self.status is not RuntimeStatus.IDLE and self.idle_since is not None:
            raise ValueError("only IDLE state may retain idle timestamps")
        if self.status is RuntimeStatus.STOPPING and self.stopping_at is None:
            raise ValueError("STOPPING state requires a stopping timestamp")
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
            values["ready_at"] = at
        elif next_status is RuntimeStatus.BUSY:
            values["busy_since"] = at
        elif next_status is RuntimeStatus.STARTING:
            values["desired_count"] = 1
            values["wake_started_at"] = at
            values["stopping_at"] = None
            values["stopped_at"] = None
        elif next_status is RuntimeStatus.STOPPING:
            values["desired_count"] = 0
            values["stopping_at"] = at
        elif next_status is RuntimeStatus.STOPPED:
            values["desired_count"] = 0
            values["stopped_at"] = at
            values["runtime_instance_id"] = None
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
