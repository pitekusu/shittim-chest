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
    RuntimeActivityInspector,
    RuntimeStateRepository,
    StatusPublicationRepository,
    StatusPublicationTrigger,
    StatusTriggerUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    EcsRuntimeSnapshot,
    IngressRequest,
    RuntimeActivity,
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
    runtime_status: RuntimeStatus | None = None
    runtime_desired_count: int = 0
    ecs_running_count: int = 0
    ecs_pending_count: int = 0
    ingress_pending: int = 0
    outbox_pending: int = 0
    ecs_observed: bool = False
    ecs_scaled_up: bool = False
    ecs_scaled_down: bool = False
    runtime_entered_idle: bool = False
    runtime_stopped: bool = False
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
            self.runtime_desired_count,
            self.ecs_running_count,
            self.ecs_pending_count,
            self.ingress_pending,
            self.outbox_pending,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("reconciliation counters must be non-negative integers")
        for value in (
            self.runtime_desired_count,
            self.ecs_running_count,
            self.ecs_pending_count,
        ):
            if value > 1:
                raise ValueError("singleton runtime counts must be zero or one")
        if self.runtime_status is not None and not isinstance(self.runtime_status, RuntimeStatus):
            raise ValueError("runtime status must be a RuntimeStatus")
        if self.runtime_status is None and self.runtime_desired_count != 0:
            raise ValueError("missing runtime state cannot have desired capacity")
        if not self.ecs_observed and (self.ecs_running_count or self.ecs_pending_count):
            raise ValueError("unobserved ECS state cannot have task counts")


class RuntimeReconciler:
    """Converge persisted work before applying the matching ECS desired count."""

    __slots__ = (
        "_activity",
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
        activity: RuntimeActivityInspector,
        runtime_state: RuntimeStateRepository,
        ecs: EcsRuntimeControl,
        status_publications: StatusPublicationRepository,
        status_trigger: StatusPublicationTrigger,
    ) -> None:
        self._clock = clock
        self._ingress = ingress
        self._activity = activity
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
        activity = await self._activity.inspect(at=at)
        runtime = await self._runtime_state.get()
        runtime, resume_conflicts = await self._resume_required_work(runtime, activity, at)
        ecs_error: EcsRuntimeUnavailable | None = None
        try:
            ecs_snapshot = None if runtime is None else await self._ecs.describe()
            (
                runtime,
                ecs_snapshot,
                ecs_scaled_up,
                repair_conflicts,
            ) = await self._converge_required_runtime(
                runtime=runtime,
                activity=activity,
                observed=ecs_snapshot,
                at=at,
            )
        except EcsRuntimeUnavailable as error:
            ecs_snapshot = None
            ecs_scaled_up = False
            repair_conflicts = 0
            ecs_error = error
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
        entered_idle = False
        scaled_down = False
        stopped = False
        stop_conflicts = 0
        if ecs_error is None:
            (
                runtime,
                ecs_snapshot,
                entered_idle,
                scaled_down,
                stopped,
                stop_conflicts,
            ) = await self._converge_idle_and_stop(
                runtime=runtime,
                activity=activity,
                observed=ecs_snapshot,
                at=at,
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
            runtime_status=None if runtime is None else runtime.status,
            runtime_desired_count=0 if runtime is None else runtime.desired_count,
            ecs_running_count=0 if ecs_snapshot is None else ecs_snapshot.running_count,
            ecs_pending_count=0 if ecs_snapshot is None else ecs_snapshot.pending_count,
            ingress_pending=(
                activity.pending_ingress + activity.claimed_ingress + activity.retrying_ingress
            ),
            outbox_pending=activity.pending_outbox + activity.claimed_outbox,
            conditional_conflicts=(
                terminal_conflicts
                + wake_conflicts
                + resume_conflicts
                + repair_conflicts
                + deadline_conflicts
                + stop_conflicts
                + reconcile_conflicts
            ),
            ecs_observed=ecs_snapshot is not None,
            ecs_scaled_up=ecs_scaled_up,
            ecs_scaled_down=scaled_down,
            runtime_entered_idle=entered_idle,
            runtime_stopped=stopped,
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

    async def _resume_required_work(
        self,
        runtime: RuntimeState | None,
        activity: RuntimeActivity,
        at: datetime,
    ) -> tuple[RuntimeState | None, int]:
        if not activity.requires_runtime:
            return runtime, 0
        if runtime is None:
            raise RepositoryConflict("durable runtime work has no Runtime State")
        updated = runtime.resume_for_work(at=max(at, runtime.updated_at))
        if updated == runtime:
            return runtime, 0
        try:
            return await self._runtime_state.replace(expected=runtime, updated=updated), 0
        except RepositoryConflict:
            current = await self._runtime_state.get()
            if current is not None and current.status in {
                RuntimeStatus.STARTING,
                RuntimeStatus.READY,
                RuntimeStatus.BUSY,
            }:
                return current, 1
            raise

    async def _converge_required_runtime(
        self,
        *,
        runtime: RuntimeState | None,
        activity: RuntimeActivity,
        observed: EcsRuntimeSnapshot | None,
        at: datetime,
    ) -> tuple[RuntimeState | None, EcsRuntimeSnapshot | None, bool, int]:
        if runtime is None or observed is None or not activity.requires_runtime:
            return runtime, observed, False, 0
        scaled_up = False
        if runtime.desired_count == 1 and observed.desired_count == 0:
            observed = await self._ecs.set_desired_count(1)
            scaled_up = True
        runtime, conflicts = await self._repair_missing_task(runtime, observed, at)
        return runtime, observed, scaled_up, conflicts

    async def _converge_idle_and_stop(
        self,
        *,
        runtime: RuntimeState | None,
        activity: RuntimeActivity,
        observed: EcsRuntimeSnapshot | None,
        at: datetime,
    ) -> tuple[
        RuntimeState | None,
        EcsRuntimeSnapshot | None,
        bool,
        bool,
        bool,
        int,
    ]:
        if runtime is None or observed is None or activity.requires_runtime:
            return runtime, observed, False, False, False, 0

        entered_idle = False
        conflicts = 0
        degraded_can_idle = (
            runtime.status is RuntimeStatus.DEGRADED
            and runtime.desired_count == 1
            and runtime.runtime_instance_id is not None
            and runtime.started_at is not None
            and runtime.ready_at is not None
        )
        if runtime.status in {RuntimeStatus.READY, RuntimeStatus.BUSY} or degraded_can_idle:
            if not activity.is_complete:
                return runtime, observed, False, False, False, 0
            updated = runtime.begin_idle(at=max(at, runtime.updated_at))
            try:
                runtime = await self._runtime_state.replace(expected=runtime, updated=updated)
                entered_idle = True
            except RepositoryConflict:
                runtime = await self._runtime_state.get()
                conflicts += 1
            return runtime, observed, entered_idle, False, False, conflicts

        if runtime.status in {RuntimeStatus.STARTING, RuntimeStatus.DEGRADED}:
            try:
                runtime = await self._runtime_state.begin_unneeded_start_stop(
                    expected=runtime,
                    at=max(at, runtime.updated_at),
                )
            except RepositoryConflict:
                return await self._runtime_state.get(), observed, False, False, False, 1
        elif runtime.status is RuntimeStatus.IDLE:
            if not activity.is_complete:
                updated = runtime.leave_idle_for_external_work(at=max(at, runtime.updated_at))
                try:
                    runtime = await self._runtime_state.replace(
                        expected=runtime,
                        updated=updated,
                    )
                except RepositoryConflict:
                    return await self._runtime_state.get(), observed, False, False, False, 1
                return runtime, observed, False, False, False, 0
            if not runtime.may_stop(
                at=at,
                expected_generation=runtime.generation,
                activity=activity,
            ):
                return runtime, observed, False, False, False, 0
            try:
                runtime = await self._runtime_state.begin_idle_stop(
                    expected=runtime,
                    at=max(at, runtime.updated_at),
                )
            except RepositoryConflict:
                return await self._runtime_state.get(), observed, False, False, False, 1
        elif runtime.status is RuntimeStatus.STOPPED:
            current = await self._runtime_state.get()
            if current != runtime:
                return current, observed, False, False, False, 1
            if observed.desired_count == 0:
                return current, observed, False, False, False, 0
            latest_activity = await self._activity.inspect(at=at)
            if latest_activity.requires_runtime:
                current, resume_conflicts = await self._resume_required_work(
                    current, latest_activity, at
                )
                if current is not None and current.desired_count == 1:
                    observed = await self._ecs.set_desired_count(1)
                return current, observed, False, False, False, resume_conflicts
            observed = await self._ecs.set_desired_count(0)
            return current, observed, False, True, False, 0
        elif runtime.status is not RuntimeStatus.STOPPING:
            return runtime, observed, False, False, False, 0

        return await self._converge_stopping(
            runtime=runtime,
            observed=observed,
            at=at,
            conflicts=conflicts,
        )

    async def _converge_stopping(
        self,
        *,
        runtime: RuntimeState,
        observed: EcsRuntimeSnapshot,
        at: datetime,
        conflicts: int,
    ) -> tuple[RuntimeState, EcsRuntimeSnapshot, bool, bool, bool, int]:
        latest_activity = await self._activity.inspect(at=at)
        if latest_activity.requires_runtime:
            resumed, resume_conflicts = await self._resume_required_work(
                runtime,
                latest_activity,
                at,
            )
            if resumed is None:
                raise RepositoryConflict("Runtime State disappeared while stopping")
            if resumed.desired_count == 1 and observed.desired_count == 0:
                observed = await self._ecs.set_desired_count(1)
            return resumed, observed, False, False, False, conflicts + resume_conflicts

        current = await self._runtime_state.get()
        if current != runtime:
            if current is None:
                raise RepositoryConflict("Runtime State disappeared while stopping")
            if (
                current.status in {RuntimeStatus.STARTING, RuntimeStatus.READY, RuntimeStatus.BUSY}
                and current.desired_count == 1
                and observed.desired_count == 0
            ):
                observed = await self._ecs.set_desired_count(1)
            return current, observed, False, False, False, conflicts + 1

        scaled_down = False
        if observed.desired_count != 0:
            observed = await self._ecs.set_desired_count(0)
            scaled_down = True

        latest_activity = await self._activity.inspect(at=at)
        if latest_activity.requires_runtime:
            current = await self._runtime_state.get()
            if current is None:
                raise RepositoryConflict("Runtime State disappeared after scale down")
            resumed, resume_conflicts = await self._resume_required_work(
                current,
                latest_activity,
                at,
            )
            if resumed is None:
                raise RepositoryConflict("Runtime State disappeared after scale down")
            if observed.desired_count == 0:
                observed = await self._ecs.set_desired_count(1)
            return resumed, observed, False, scaled_down, False, conflicts + resume_conflicts

        current = await self._runtime_state.get()
        if current != runtime:
            if current is None:
                raise RepositoryConflict("Runtime State disappeared after scale down")
            if (
                current.status in {RuntimeStatus.STARTING, RuntimeStatus.READY, RuntimeStatus.BUSY}
                and current.desired_count == 1
                and observed.desired_count == 0
            ):
                observed = await self._ecs.set_desired_count(1)
            return current, observed, False, scaled_down, False, conflicts + 1
        if current is None:
            raise RepositoryConflict("Runtime State disappeared after scale down")
        if observed.desired_count or observed.running_count or observed.pending_count:
            return current, observed, False, scaled_down, False, conflicts

        stopped = current.transition(RuntimeStatus.STOPPED, at=max(at, current.updated_at))
        try:
            current = await self._runtime_state.replace(expected=current, updated=stopped)
        except RepositoryConflict:
            current = await self._runtime_state.get()
            if current is None:
                raise RepositoryConflict(
                    "Runtime State disappeared at STOPPED convergence"
                ) from None
            return current, observed, False, scaled_down, False, conflicts + 1
        return current, observed, False, scaled_down, True, conflicts

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
