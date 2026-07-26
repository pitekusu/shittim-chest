"""Reconcile durable ingress deadlines and the singleton runtime desired state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from shittim_chest.application.ports import (
    Clock,
    EcsRuntimeControl,
    EcsRuntimeUnavailable,
    IngressRepository,
    RepositoryConflict,
    RuntimeStateRepository,
    StatusPublicationRepository,
    StatusPublicationTrigger,
    StatusTriggerUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    EcsRuntimeSnapshot,
    IngressRequest,
    RuntimeState,
    RuntimeStatus,
    StatusMessageState,
)

STARTUP_TERMINAL_DEADLINE_ERROR = "startup_terminal_deadline_exceeded"
STATUS_PUBLICATION_SWEEP_LIMIT = 100


@dataclass(frozen=True, slots=True)
class RuntimeReconciliationReport:
    """Content-free outcome counters for one deterministic reconciliation pass."""

    observed_at: datetime
    terminal_failed: int = 0
    wake_candidates: int = 0
    startup_timed_out: int = 0
    startup_recovered: int = 0
    status_publications_triggered: int = 0
    conditional_conflicts: int = 0
    ecs_observed: bool = False
    ecs_scaled_up: bool = False
    runtime_reconciled: bool = False

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != UTC.utcoffset(
            self.observed_at
        ):
            raise ValueError("reconciliation timestamp must be timezone-aware UTC")
        for value in (
            self.terminal_failed,
            self.wake_candidates,
            self.startup_timed_out,
            self.startup_recovered,
            self.status_publications_triggered,
            self.conditional_conflicts,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("reconciliation counters must be non-negative integers")


class RuntimeReconciler:
    """Converge persisted work before applying the matching ECS desired count."""

    __slots__ = (
        "_clock",
        "_ecs",
        "_ingress",
        "_runtime_state",
        "_status_publications",
        "_status_trigger",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        ingress: IngressRepository,
        runtime_state: RuntimeStateRepository,
        ecs: EcsRuntimeControl,
        status_publications: StatusPublicationRepository,
        status_trigger: StatusPublicationTrigger,
    ) -> None:
        self._clock = clock
        self._ingress = ingress
        self._runtime_state = runtime_state
        self._ecs = ecs
        self._status_publications = status_publications
        self._status_trigger = status_trigger

    async def reconcile(self) -> RuntimeReconciliationReport:
        """Run one pass using a single wall-clock observation."""

        at = self._clock.now()
        (
            terminal_failed,
            terminal_conflicts,
            terminal_statuses,
        ) = await self._expire_terminal_requests(at)
        wake_candidates, wake_conflicts = await self._ensure_active_wakes(at)

        runtime = await self._runtime_state.get()
        ecs_error: EcsRuntimeUnavailable | None = None
        try:
            ecs_snapshot, ecs_scaled_up = await self._converge_ecs_up(runtime)
        except EcsRuntimeUnavailable as error:
            ecs_snapshot = None
            ecs_scaled_up = False
            ecs_error = error
        runtime, repair_conflicts = await self._repair_missing_task(runtime, ecs_snapshot, at)
        if _runtime_can_start_requests(runtime, ecs_snapshot):
            startup_timed_out = 0
            (
                startup_recovered,
                deadline_conflicts,
                deadline_statuses,
            ) = await self._recover_startup_messages(at)
        else:
            (
                startup_timed_out,
                deadline_conflicts,
                deadline_statuses,
            ) = await self._mark_startup_timeouts(at)
            startup_recovered = 0

        status_triggered, status_trigger_failed = await self._trigger_due_statuses(
            at,
            immediate=(*terminal_statuses, *deadline_statuses),
        )
        if ecs_error is None:
            runtime_reconciled, reconcile_conflicts = await self._record_reconciled(runtime, at)
        else:
            runtime_reconciled, reconcile_conflicts = False, 0

        report = RuntimeReconciliationReport(
            observed_at=at,
            terminal_failed=terminal_failed,
            wake_candidates=wake_candidates,
            startup_timed_out=startup_timed_out,
            startup_recovered=startup_recovered,
            status_publications_triggered=status_triggered,
            conditional_conflicts=(
                terminal_conflicts
                + wake_conflicts
                + repair_conflicts
                + deadline_conflicts
                + reconcile_conflicts
            ),
            ecs_observed=ecs_snapshot is not None,
            ecs_scaled_up=ecs_scaled_up,
            runtime_reconciled=runtime_reconciled,
        )
        if ecs_error is not None:
            raise ecs_error
        if status_trigger_failed:
            raise StatusTriggerUnavailable
        return report

    async def _expire_terminal_requests(
        self,
        at: datetime,
    ) -> tuple[int, int, tuple[str, ...]]:
        failed = 0
        conflicts = 0
        status_ids: list[str] = []
        for request in await self._ingress.list_terminal_deadlines(at=at):
            try:
                await self._ingress.mark_terminal_deadline(
                    request=request,
                    at=at,
                    error_code=STARTUP_TERMINAL_DEADLINE_ERROR,
                )
            except RepositoryConflict:
                if await self._deadline_request_remains(request, at=at, terminal=True):
                    raise
                conflicts += 1
            else:
                failed += 1
                status_ids.append(request.interaction_id)
        return failed, conflicts, tuple(status_ids)

    async def _ensure_active_wakes(self, at: datetime) -> tuple[int, int]:
        candidates = await self._ingress.list_active_wake_candidates()
        conflicts = 0
        for candidate in candidates:
            try:
                await self._runtime_state.ensure_wake(
                    interaction_id=candidate.interaction_id,
                    at=at,
                )
            except RepositoryConflict:
                current = await self._ingress.list_active_wake_candidates()
                if any(item.interaction_id == candidate.interaction_id for item in current):
                    raise
                conflicts += 1
        return len(candidates), conflicts

    async def _mark_startup_timeouts(
        self,
        at: datetime,
    ) -> tuple[int, int, tuple[str, ...]]:
        timed_out = 0
        conflicts = 0
        status_ids: list[str] = []
        for request in await self._ingress.list_startup_deadlines(at=at):
            try:
                await self._ingress.mark_startup_timeout(request=request, at=at)
            except RepositoryConflict:
                if await self._deadline_request_remains(request, at=at, terminal=False):
                    raise
                conflicts += 1
            else:
                timed_out += 1
                status_ids.append(request.interaction_id)
        return timed_out, conflicts, tuple(status_ids)

    async def _recover_startup_messages(
        self,
        at: datetime,
    ) -> tuple[int, int, tuple[str, ...]]:
        recovered = 0
        conflicts = 0
        status_ids: list[str] = []
        for request in await self._ingress.list_ready(at=at):
            if request.status_message_state is not StatusMessageState.STARTUP_TIMEOUT:
                continue
            try:
                await self._ingress.request_status_publication(
                    request=request,
                    state=StatusMessageState.RECOVERED,
                    at=at,
                )
            except RepositoryConflict:
                if await self._ready_timeout_remains(request, at=at):
                    raise
                conflicts += 1
            else:
                recovered += 1
                status_ids.append(request.interaction_id)
        return recovered, conflicts, tuple(status_ids)

    async def _trigger_due_statuses(
        self,
        at: datetime,
        *,
        immediate: tuple[str, ...],
    ) -> tuple[int, bool]:
        triggered = 0
        failed = False
        due = await self._status_publications.list_due_status_publications(
            at=at,
            limit=STATUS_PUBLICATION_SWEEP_LIMIT,
        )
        interaction_ids = dict.fromkeys(
            (*immediate, *(publication.canonical_interaction_id for publication in due))
        )
        for interaction_id in interaction_ids:
            try:
                await self._status_trigger.request_publication(interaction_id)
            except StatusTriggerUnavailable:
                failed = True
            else:
                triggered += 1
        return triggered, failed

    async def _converge_ecs_up(
        self,
        runtime: RuntimeState | None,
    ) -> tuple[EcsRuntimeSnapshot | None, bool]:
        if runtime is None:
            return None, False
        observed = await self._ecs.describe()
        if runtime.desired_count == 1 and observed.desired_count == 0:
            return await self._ecs.set_desired_count(1), True
        return observed, False

    async def _record_reconciled(
        self,
        runtime: RuntimeState | None,
        at: datetime,
    ) -> tuple[bool, int]:
        if runtime is None:
            return False, 0
        updated = runtime.record_reconciled(at=max(at, runtime.updated_at))
        try:
            await self._runtime_state.replace(expected=runtime, updated=updated)
        except RepositoryConflict:
            await self._runtime_state.get()
            return False, 1
        return True, 0

    async def _repair_missing_task(
        self,
        runtime: RuntimeState | None,
        observed: EcsRuntimeSnapshot | None,
        at: datetime,
    ) -> tuple[RuntimeState | None, int]:
        if (
            runtime is None
            or observed is None
            or observed.running_count == 1
            or (
                runtime.status not in {RuntimeStatus.READY, RuntimeStatus.BUSY}
                and not (
                    runtime.status is RuntimeStatus.STARTING
                    and runtime.runtime_instance_id is not None
                )
            )
        ):
            return runtime, 0
        updated = runtime.fence_stale_instance(at=max(at, runtime.updated_at))
        try:
            return await self._runtime_state.replace(expected=runtime, updated=updated), 0
        except RepositoryConflict:
            return await self._runtime_state.get(), 1

    async def _deadline_request_remains(
        self,
        expected: IngressRequest,
        *,
        at: datetime,
        terminal: bool,
    ) -> bool:
        current = (
            await self._ingress.list_terminal_deadlines(at=at)
            if terminal
            else await self._ingress.list_startup_deadlines(at=at)
        )
        return any(request.interaction_id == expected.interaction_id for request in current)

    async def _ready_timeout_remains(self, expected: IngressRequest, *, at: datetime) -> bool:
        return any(
            request.interaction_id == expected.interaction_id
            and request.status_message_state is StatusMessageState.STARTUP_TIMEOUT
            for request in await self._ingress.list_ready(at=at)
        )


def _runtime_can_start_requests(
    runtime: RuntimeState | None,
    observed: EcsRuntimeSnapshot | None,
) -> bool:
    return (
        runtime is not None
        and observed is not None
        and observed.running_count == 1
        and runtime.status in {RuntimeStatus.READY, RuntimeStatus.BUSY}
    )


__all__ = (
    "STARTUP_TERMINAL_DEADLINE_ERROR",
    "STATUS_PUBLICATION_SWEEP_LIMIT",
    "RuntimeReconciler",
    "RuntimeReconciliationReport",
)
