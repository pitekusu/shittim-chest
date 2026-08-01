"""Drain durable Discord ingress after runtime recovery is complete."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Protocol

from shittim_chest.application.commands import AppliedIngressCommand
from shittim_chest.application.errors import (
    ApplicationError,
    DebateNotFound,
    InvalidApplicationOperation,
    RequestNotAllowed,
    RuntimeNotReady,
)
from shittim_chest.application.ports import (
    Clock,
    IngressRepository,
    RepositoryBusy,
    RepositoryConflict,
    RepositoryQuotaExceeded,
    RepositoryUnavailable,
    RuntimeStateRepository,
)
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressRequest,
    IngressStatus,
    RuntimeStatus,
)

DEFAULT_DRAIN_POLL_SECONDS = 1.0
DEFAULT_INGRESS_RETRY_SECONDS = 5.0


class _IngressCommandExecutor(Protocol):
    """Apply one request with the claim fenced into its first durable mutation.

    Implementations must make the current request claim a condition of the same
    transaction that first changes debate state.  The drainer's pre-call check
    alone is intentionally not treated as a concurrency fence.
    """

    async def apply(
        self,
        request: IngressRequest,
        *,
        claim_owner: str,
        at: datetime,
    ) -> AppliedIngressCommand: ...

    async def abort_pre_activation(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
        *,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> str: ...


class _IngressContextExecutor(Protocol):
    """Prepare durable Discord context, then activate only after settlement."""

    async def preflight(self, request: IngressRequest) -> None:
        """Validate immutable control context without changing debate state."""

        ...

    async def prepare(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        """Idempotently resolve or create thread and control-panel context."""

        ...

    async def activate(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        """Register the accepted debate in the process-owned task coordinator.

        Implementations must be idempotent.  An exception intentionally escapes
        the drainer so process recovery can enumerate the already durable debate.
        """

        ...


class _RuntimeDrainSession(Protocol):
    """Optionally project accepted work into the generation-fenced runtime state."""

    async def mark_busy(self) -> object: ...


class _RuntimeAdmission(Protocol):
    """Expose the process admission gate without importing a runtime adapter."""

    @property
    def is_accepting(self) -> bool: ...

    async def all_identities_ready(self) -> bool: ...


class IngressRetryableFailure(RuntimeError):
    """Request a durable retry for a typed, temporary command-boundary failure."""

    def __init__(self, code: str) -> None:
        self.code = _validated_error_code(code)
        super().__init__(self.code)


class IngressRejectedFailure(RuntimeError):
    """Reject an invalid or unauthorized request without retrying it."""

    def __init__(self, code: str) -> None:
        self.code = _validated_error_code(code)
        super().__init__(self.code)


class IngressTerminalFailure(RuntimeError):
    """Fail a request whose command cannot be recovered by another attempt."""

    def __init__(self, code: str) -> None:
        self.code = _validated_error_code(code)
        super().__init__(self.code)


@unique
class IngressDrainStop(StrEnum):
    """Why one bounded drain pass stopped."""

    GATE_CLOSED = "gate_closed"
    RUNTIME_NOT_READY = "runtime_not_ready"
    RUNTIME_STATE_UNAVAILABLE = "runtime_state_unavailable"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"
    QUEUE_EMPTY = "queue_empty"
    QUEUE_DRAINED = "queue_drained"
    RETRY_SCHEDULED = "retry_scheduled"
    SLOT_BUSY = "slot_busy"
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True, slots=True)
class IngressDrainReport:
    """Content-free counters for one drain pass."""

    stop: IngressDrainStop
    claimed: int = 0
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    rescheduled: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.claimed,
            self.accepted,
            self.rejected,
            self.failed,
            self.rescheduled,
        ):
            if isinstance(value, bool) or value < 0:
                raise ValueError("drain counters must be non-negative integers")
        settled = self.accepted + self.rejected + self.failed + self.rescheduled
        if settled > self.claimed:
            raise ValueError("settled ingress count cannot exceed claimed count")


class RuntimeIngressDrainGate:
    """Fail-closed gate for local schema validation, recovery, and admission."""

    __slots__ = (
        "_admission",
        "_local_command_schema_checked",
        "_recovery_complete",
        "_shutdown",
        "_supervisor_started",
    )

    def __init__(self, admission: _RuntimeAdmission) -> None:
        self._admission = admission
        self._supervisor_started = False
        self._local_command_schema_checked = False
        self._recovery_complete = False
        self._shutdown = False

    @property
    def recovery_complete(self) -> bool:
        """Return whether recoverable debates have completed initial scheduling."""

        return self._recovery_complete

    def mark_supervisor_started(self) -> None:
        """Record that the process-owned Discord supervisor is running."""

        if not self._shutdown:
            self._supervisor_started = True

    def mark_local_command_schema_checked(self) -> None:
        """Record that the immutable local command schema was validated."""

        if not self._shutdown:
            self._local_command_schema_checked = True

    def begin_recovery(self) -> None:
        """Close the recovery barrier during startup or reconnect recovery."""

        if not self._shutdown:
            self._recovery_complete = False

    def mark_recovery_complete(self) -> None:
        """Open only the recovery barrier; admission and runtime state remain required."""

        if not self._shutdown:
            self._recovery_complete = True

    def begin_shutdown(self) -> None:
        """Permanently reject new claims for this process instance."""

        self._shutdown = True
        self._recovery_complete = False

    async def ready_to_drain(self) -> bool:
        """Check every process-local prerequisite without changing external state."""

        return (
            not self._shutdown
            and self._supervisor_started
            and self._local_command_schema_checked
            and self._recovery_complete
            and self._admission.is_accepting
            and await self._admission.all_identities_ready()
        )


class IngressDrainer:
    """Claim and settle ready ingress sequentially in repository FIFO order."""

    __slots__ = (
        "_clock",
        "_commands",
        "_context",
        "_gate",
        "_ingress",
        "_poll_seconds",
        "_retry_delay",
        "_runtime_instance_id",
        "_runtime_session",
        "_runtime_state",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        ingress: IngressRepository,
        runtime_state: RuntimeStateRepository,
        commands: _IngressCommandExecutor,
        context: _IngressContextExecutor,
        gate: RuntimeIngressDrainGate,
        runtime_instance_id: str,
        runtime_session: _RuntimeDrainSession | None = None,
        retry_delay: timedelta = timedelta(seconds=DEFAULT_INGRESS_RETRY_SECONDS),
        poll_seconds: float = DEFAULT_DRAIN_POLL_SECONDS,
    ) -> None:
        if not runtime_instance_id.strip():
            raise ValueError("runtime instance ID must not be empty")
        if retry_delay <= timedelta(0):
            raise ValueError("ingress retry delay must be positive")
        if poll_seconds <= 0:
            raise ValueError("drain poll interval must be positive")
        self._clock = clock
        self._ingress = ingress
        self._runtime_state = runtime_state
        self._commands = commands
        self._context = context
        self._gate = gate
        self._runtime_instance_id = runtime_instance_id
        self._retry_delay = retry_delay
        self._poll_seconds = poll_seconds
        self._runtime_session = runtime_session

    async def drain_once(self) -> IngressDrainReport:
        """Process one complete Query result, stopping before any FIFO overtake."""

        if not await self._gate.ready_to_drain():
            return IngressDrainReport(IngressDrainStop.GATE_CLOSED)
        try:
            runtime = await self._runtime_state.get()
        except RepositoryConflict, RepositoryUnavailable:
            return IngressDrainReport(IngressDrainStop.RUNTIME_STATE_UNAVAILABLE)
        if (
            runtime is None
            or runtime.status not in {RuntimeStatus.READY, RuntimeStatus.BUSY}
            or runtime.runtime_instance_id != self._runtime_instance_id
        ):
            return IngressDrainReport(IngressDrainStop.RUNTIME_NOT_READY)

        query_at = self._clock.now()
        try:
            ready = await self._ingress.list_ready(at=query_at)
        except RepositoryConflict, RepositoryUnavailable:
            return IngressDrainReport(IngressDrainStop.REPOSITORY_UNAVAILABLE)
        if not ready:
            return IngressDrainReport(IngressDrainStop.QUEUE_EMPTY)

        report = IngressDrainReport(IngressDrainStop.QUEUE_DRAINED)
        for request in ready:
            claim_at = self._clock.now()
            try:
                claimed = await self._ingress.claim(
                    request=request,
                    claim_owner=self._runtime_instance_id,
                    at=claim_at,
                )
            except RepositoryConflict:
                return replace(report, stop=IngressDrainStop.CLAIM_LOST)
            except RepositoryUnavailable:
                return replace(report, stop=IngressDrainStop.REPOSITORY_UNAVAILABLE)
            if claimed is None:
                return replace(report, stop=IngressDrainStop.CLAIM_LOST)
            report = replace(report, claimed=report.claimed + 1)
            applied: AppliedIngressCommand | None = None
            try:
                if claimed.kind is IngressKind.NEW_DEBATE and claimed.status_message_id is None:
                    raise IngressRetryableFailure("status_message_pending")
                if claimed.kind is not IngressKind.NEW_DEBATE:
                    await self._context.preflight(claimed)
                applied = await self._commands.apply(
                    claimed,
                    claim_owner=self._runtime_instance_id,
                    at=claim_at,
                )
                if applied.kind is not claimed.kind:
                    raise IngressTerminalFailure("command_result_kind_mismatch")
                if applied.terminal_error_code is not None:
                    raise IngressTerminalFailure(applied.terminal_error_code)
                await self._context.prepare(claimed, applied)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                report, should_stop = await self._settle_error(
                    report,
                    request=claimed,
                    error=error,
                    applied=applied,
                )
                if should_stop:
                    return report
                continue

            settle_at = self._clock.now()
            try:
                accepted = await self._ingress.mark_accepted(
                    request=claimed,
                    claim_owner=self._runtime_instance_id,
                    at=settle_at,
                    debate_id=applied.debate_id,
                    attempt_id=applied.attempt_id,
                )
            except RepositoryConflict:
                return replace(report, stop=IngressDrainStop.CLAIM_LOST)
            except RepositoryUnavailable:
                return replace(report, stop=IngressDrainStop.REPOSITORY_UNAVAILABLE)
            report = replace(report, accepted=report.accepted + 1)
            if self._runtime_session is not None:
                await self._runtime_session.mark_busy()
            await self._context.activate(accepted, applied)
        return report

    async def run(self, stop: asyncio.Event) -> None:
        """Poll with an owned stop event; cancellation always propagates."""

        while not stop.is_set():
            await self.drain_once()
            try:
                async with asyncio.timeout(self._poll_seconds):
                    await stop.wait()
            except TimeoutError:
                pass

    async def _settle_error(
        self,
        report: IngressDrainReport,
        *,
        request: IngressRequest,
        error: Exception,
        applied: AppliedIngressCommand | None = None,
    ) -> tuple[IngressDrainReport, bool]:
        disposition = classify_ingress_failure(error)
        if disposition.status is None:
            return replace(report, stop=IngressDrainStop.CLAIM_LOST), True

        if (
            disposition.status in {IngressStatus.REJECTED, IngressStatus.FAILED}
            and applied is not None
            and applied.kind is request.kind
            and applied.kind in {IngressKind.NEW_DEBATE, IngressKind.RETRY}
            and applied.terminal_error_code is None
        ):
            try:
                persisted_code = await self._commands.abort_pre_activation(
                    request,
                    applied,
                    claim_owner=self._runtime_instance_id,
                    at=self._clock.now(),
                    error_code=disposition.error_code,
                )
            except asyncio.CancelledError:
                raise
            except RepositoryConflict:
                return replace(report, stop=IngressDrainStop.CLAIM_LOST), True
            except RepositoryUnavailable:
                return replace(report, stop=IngressDrainStop.REPOSITORY_UNAVAILABLE), True
            disposition = replace(disposition, error_code=persisted_code)

        at = self._clock.now()
        try:
            if disposition.status is IngressStatus.RETRYING:
                await self._ingress.reschedule(
                    request=request,
                    claim_owner=self._runtime_instance_id,
                    at=at,
                    next_attempt_at=at + self._retry_delay,
                    error_code=disposition.error_code,
                )
                stop = (
                    IngressDrainStop.SLOT_BUSY
                    if isinstance(error, RepositoryBusy)
                    else IngressDrainStop.RETRY_SCHEDULED
                )
                return replace(report, stop=stop, rescheduled=report.rescheduled + 1), True
            terminal_status = disposition.status
            if terminal_status not in {IngressStatus.REJECTED, IngressStatus.FAILED}:
                raise AssertionError("unsupported ingress terminal disposition")
            await self._ingress.mark_claim_terminal(
                request=request,
                claim_owner=self._runtime_instance_id,
                at=at,
                status=terminal_status,
                error_code=disposition.error_code,
            )
        except RepositoryConflict:
            return replace(report, stop=IngressDrainStop.CLAIM_LOST), True
        except RepositoryUnavailable:
            return replace(report, stop=IngressDrainStop.REPOSITORY_UNAVAILABLE), True

        if disposition.status is IngressStatus.REJECTED:
            return replace(report, rejected=report.rejected + 1), False
        return replace(report, failed=report.failed + 1), False


@dataclass(frozen=True, slots=True)
class IngressFailureDisposition:
    """Durable settlement selected without SDK-specific exception types."""

    status: IngressStatus | None
    error_code: str

    def __post_init__(self) -> None:
        _validated_error_code(self.error_code)
        if self.status not in {
            None,
            IngressStatus.RETRYING,
            IngressStatus.REJECTED,
            IngressStatus.FAILED,
        }:
            raise ValueError("unsupported ingress failure disposition")


def classify_ingress_failure(error: Exception) -> IngressFailureDisposition:
    """Map application-boundary failures to stable, content-free persistence states."""

    if isinstance(error, RepositoryConflict):
        return IngressFailureDisposition(None, "claim_lost")
    if isinstance(error, RepositoryBusy):
        return IngressFailureDisposition(IngressStatus.RETRYING, "execution_slots_busy")
    if isinstance(error, RepositoryQuotaExceeded):
        return IngressFailureDisposition(IngressStatus.REJECTED, "daily_quota_exceeded")
    if isinstance(error, IngressRetryableFailure):
        return IngressFailureDisposition(IngressStatus.RETRYING, error.code)
    if isinstance(error, IngressRejectedFailure):
        return IngressFailureDisposition(IngressStatus.REJECTED, error.code)
    if isinstance(error, IngressTerminalFailure):
        return IngressFailureDisposition(IngressStatus.FAILED, error.code)
    if isinstance(error, RuntimeNotReady):
        return IngressFailureDisposition(IngressStatus.RETRYING, RuntimeNotReady.code)
    if isinstance(error, RequestNotAllowed | DebateNotFound | InvalidApplicationOperation):
        return IngressFailureDisposition(IngressStatus.REJECTED, error.code)
    if isinstance(error, RepositoryUnavailable):
        return IngressFailureDisposition(IngressStatus.RETRYING, "repository_unavailable")
    if isinstance(error, TimeoutError):
        return IngressFailureDisposition(IngressStatus.RETRYING, "command_timeout")
    if isinstance(error, ApplicationError):
        return IngressFailureDisposition(IngressStatus.RETRYING, error.code)
    return IngressFailureDisposition(IngressStatus.RETRYING, "command_failed")


def _validated_error_code(code: str) -> str:
    value = code.strip()
    if not value:
        raise ValueError("ingress error code must not be empty")
    if len(value) > 100:
        raise ValueError("ingress error code must be at most 100 characters")
    return value


__all__ = (
    "DEFAULT_DRAIN_POLL_SECONDS",
    "DEFAULT_INGRESS_RETRY_SECONDS",
    "IngressDrainReport",
    "IngressDrainStop",
    "IngressDrainer",
    "IngressFailureDisposition",
    "IngressRejectedFailure",
    "IngressRetryableFailure",
    "IngressTerminalFailure",
    "RuntimeIngressDrainGate",
    "classify_ingress_failure",
)
