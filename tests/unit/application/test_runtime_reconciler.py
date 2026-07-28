"""Deterministic deadline and lost-wake reconciliation tests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.application import (
    STARTUP_TERMINAL_DEADLINE_ERROR,
    EcsRuntimeSnapshot,
    IngressRequest,
    IngressStatus,
    IngressStatusPublication,
    RuntimeActivity,
    RuntimeReconciler,
    RuntimeReconciliationReport,
    RuntimeState,
    RuntimeStatus,
    StatusMessageState,
)
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
from shittim_chest.application.scale_to_zero import IngressWakeCandidate

CREATED_AT = datetime(2026, 7, 26, 1, 0, tzinfo=UTC)


@dataclass(slots=True)
class FakeClock:
    current: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.current


class FakeIngress:
    def __init__(self, requests: tuple[IngressRequest, ...]) -> None:
        self.requests = list(requests)
        self.terminal_calls: list[str] = []
        self.timeout_calls: list[str] = []
        self.recovery_calls: list[str] = []
        self.terminal_conflicts: set[str] = set()
        self.terminal_persistent_conflicts: set[str] = set()
        self.timeout_conflicts: set[str] = set()
        self.timeout_persistent_conflicts: set[str] = set()
        self.recovery_conflicts: set[str] = set()
        self.recovery_persistent_conflicts: set[str] = set()

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return tuple(
            request
            for request in self.requests
            if request.status.counts_toward_queue_limit
            and request.processing_started_at is None
            and request.terminal_deadline_at <= at
        )

    async def mark_terminal_deadline(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        error_code: str,
    ) -> IngressRequest:
        self.terminal_calls.append(request.interaction_id)
        if request.interaction_id in self.terminal_persistent_conflicts:
            raise RepositoryConflict
        if request.interaction_id in self.terminal_conflicts:
            self.requests = [
                item for item in self.requests if item.interaction_id != request.interaction_id
            ]
            raise RepositoryConflict
        updated = replace(
            request,
            status=IngressStatus.FAILED,
            status_message_state=StatusMessageState.TERMINAL_FAILED,
            updated_at=at,
            error_code=error_code,
            completed_at=at,
            claim_owner=None,
            claim_expires_at=None,
        )
        self._replace(request, updated)
        return updated

    async def list_active_wake_candidates(self) -> tuple[IngressWakeCandidate, ...]:
        return tuple(
            IngressWakeCandidate.from_request(request)
            for request in self.requests
            if request.status.counts_toward_queue_limit
        )

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return tuple(
            request
            for request in self.requests
            if request.status.counts_toward_queue_limit
            and request.processing_started_at is None
            and request.startup_deadline_at <= at < request.terminal_deadline_at
            and request.status_message_state
            not in {StatusMessageState.STARTUP_TIMEOUT, StatusMessageState.RECOVERED}
        )

    async def mark_startup_timeout(
        self,
        *,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest:
        self.timeout_calls.append(request.interaction_id)
        if request.interaction_id in self.timeout_persistent_conflicts:
            raise RepositoryConflict
        if request.interaction_id in self.timeout_conflicts:
            self.requests = [
                item for item in self.requests if item.interaction_id != request.interaction_id
            ]
            raise RepositoryConflict
        updated = replace(
            request,
            status_message_state=StatusMessageState.STARTUP_TIMEOUT,
            updated_at=at,
        )
        self._replace(request, updated)
        return updated

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return tuple(
            request
            for request in self.requests
            if request.status.counts_toward_queue_limit
            and (request.processing_started_at is not None or at < request.terminal_deadline_at)
            and request.status is not IngressStatus.CLAIMED
        )

    async def request_status_publication(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        at: datetime,
    ) -> IngressRequest:
        self.recovery_calls.append(request.interaction_id)
        if request.interaction_id in self.recovery_persistent_conflicts:
            raise RepositoryConflict
        if request.interaction_id in self.recovery_conflicts:
            self.requests = [
                item for item in self.requests if item.interaction_id != request.interaction_id
            ]
            raise RepositoryConflict
        updated = replace(request, status_message_state=state, updated_at=at)
        self._replace(request, updated)
        return updated

    def _replace(self, previous: IngressRequest, updated: IngressRequest) -> None:
        self.requests[self.requests.index(previous)] = updated


class FakeRuntimeRepository:
    def __init__(self, state: RuntimeState | None = None) -> None:
        self.state = state
        self.ensure_calls: list[str] = []
        self.wake_results: set[str] = set()
        self.replace_calls = 0
        self.replace_conflicts = 0
        self.ensure_conflicts: set[str] = set()

    async def ensure_wake(self, *, interaction_id: str, at: datetime) -> RuntimeState:
        self.ensure_calls.append(interaction_id)
        if interaction_id in self.ensure_conflicts:
            raise RepositoryConflict
        if interaction_id in self.wake_results:
            if self.state is None:
                raise AssertionError("wake replay lost runtime state")
            return self.state
        self.wake_results.add(interaction_id)
        baseline = self.state or RuntimeState.stopped(at=at)
        self.state = baseline.request_wake(at=max(at, baseline.updated_at))
        return self.state

    async def get(self) -> RuntimeState | None:
        return self.state

    async def replace(self, *, expected: RuntimeState, updated: RuntimeState) -> RuntimeState:
        self.replace_calls += 1
        if self.replace_conflicts:
            self.replace_conflicts -= 1
            raise RepositoryConflict
        if self.state != expected:
            raise RepositoryConflict
        self.state = updated
        return updated

    async def begin_idle_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState:
        return await self.replace(expected=expected, updated=expected.begin_idle_stop(at=at))

    async def begin_unneeded_start_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState:
        return await self.replace(
            expected=expected,
            updated=expected.begin_unneeded_start_stop(at=at),
        )


class FakeActivity:
    def __init__(
        self,
        ingress: FakeIngress,
        snapshots: tuple[RuntimeActivity, ...] = (),
    ) -> None:
        self.ingress = ingress
        self.snapshots = list(snapshots)
        self.calls: list[datetime] = []

    async def inspect(self, *, at: datetime) -> RuntimeActivity:
        self.calls.append(at)
        if self.snapshots:
            return self.snapshots.pop(0)
        pending = sum(request.status is IngressStatus.PENDING for request in self.ingress.requests)
        claimed = sum(request.status is IngressStatus.CLAIMED for request in self.ingress.requests)
        retrying = sum(
            request.status is IngressStatus.RETRYING for request in self.ingress.requests
        )
        return RuntimeActivity(
            pending_ingress=pending,
            claimed_ingress=claimed,
            retrying_ingress=retrying,
        )


class FakeEcs:
    def __init__(self, snapshot: EcsRuntimeSnapshot) -> None:
        self.snapshot = snapshot
        self.describe_calls = 0
        self.update_calls: list[int] = []
        self.fail_describe = False

    async def describe(self) -> EcsRuntimeSnapshot:
        self.describe_calls += 1
        if self.fail_describe:
            raise EcsRuntimeUnavailable
        return self.snapshot

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot:
        self.update_calls.append(desired_count)
        self.snapshot = EcsRuntimeSnapshot(
            desired_count=desired_count,
            running_count=self.snapshot.running_count,
            pending_count=self.snapshot.pending_count,
        )
        return self.snapshot


class FailOnceScaleDownEcs(FakeEcs):
    """Lose one scale-down response either before or after ECS accepted it."""

    def __init__(
        self,
        snapshot: EcsRuntimeSnapshot,
        *,
        apply_before_error: bool,
    ) -> None:
        super().__init__(snapshot)
        self.apply_before_error = apply_before_error
        self.failed_once = False

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot:
        if desired_count != 0 or self.failed_once:
            return await super().set_desired_count(desired_count)
        self.failed_once = True
        self.update_calls.append(desired_count)
        if self.apply_before_error:
            self.snapshot = EcsRuntimeSnapshot(
                desired_count=desired_count,
                running_count=self.snapshot.running_count,
                pending_count=self.snapshot.pending_count,
            )
        raise EcsRuntimeUnavailable


class FakeStatusRepository:
    def __init__(self, due: tuple[IngressStatusPublication, ...] = ()) -> None:
        self.due = due
        self.limits: list[int] = []

    async def list_due_status_publications(
        self,
        *,
        at: datetime,
        limit: int,
    ) -> tuple[IngressStatusPublication, ...]:
        self.limits.append(limit)
        return self.due


class FakeStatusTrigger:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failures: set[str] = set()

    async def request_publication(self, interaction_id: str) -> None:
        self.calls.append(interaction_id)
        if interaction_id in self.failures:
            raise StatusTriggerUnavailable


def request(index: int = 1) -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id=str(1000 + index),
        operation_id=f"operation-{index}",
        application_id="application",
        question="question",
        requester_id="requester",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild",
        channel_id="channel",
        command_name="shittim",
        created_at=CREATED_AT + timedelta(microseconds=index),
    )


def ready_runtime(at: datetime) -> RuntimeState:
    starting = RuntimeState.stopped(at=CREATED_AT).request_wake(at=CREATED_AT)
    started = starting.mark_started(at=CREATED_AT, runtime_instance_id="runtime-1")
    return started.transition(RuntimeStatus.READY, at=at, runtime_instance_id="runtime-1")


def stopping_runtime() -> RuntimeState:
    idle_at = CREATED_AT + timedelta(minutes=1)
    idle = ready_runtime(idle_at - timedelta(seconds=1)).begin_idle(at=idle_at)
    assert idle.stop_eligible_at is not None
    return idle.begin_idle_stop(at=idle.stop_eligible_at)


def reconciler(
    *,
    at: datetime,
    ingress: FakeIngress,
    runtime: FakeRuntimeRepository,
    ecs: FakeEcs | None = None,
    statuses: FakeStatusRepository | None = None,
    trigger: FakeStatusTrigger | None = None,
    activity: FakeActivity | None = None,
) -> tuple[RuntimeReconciler, FakeClock, FakeEcs, FakeStatusRepository, FakeStatusTrigger]:
    clock = FakeClock(at)
    ecs = ecs or FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    statuses = statuses or FakeStatusRepository()
    trigger = trigger or FakeStatusTrigger()
    activity = activity or FakeActivity(ingress)
    value = RuntimeReconciler(
        clock=cast(Clock, clock),
        ingress=cast(IngressRepository, ingress),
        activity=cast(RuntimeActivityInspector, activity),
        runtime_state=cast(RuntimeStateRepository, runtime),
        ecs=cast(EcsRuntimeControl, ecs),
        status_publications=cast(StatusPublicationRepository, statuses),
        status_trigger=cast(StatusPublicationTrigger, trigger),
    )
    return value, clock, ecs, statuses, trigger


@pytest.mark.asyncio
async def test_two_fifty_nine_wakes_and_scales_up_without_timeout() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    value, clock, ecs, _, _ = reconciler(
        at=source.created_at + timedelta(minutes=2, seconds=59),
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert clock.calls == 1
    assert report.wake_candidates == 1
    assert report.startup_timed_out == 0
    assert report.terminal_failed == 0
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING


@pytest.mark.asyncio
async def test_three_minutes_is_nonterminal_and_keeps_scale_up_active() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    value, _, ecs, _, trigger = reconciler(
        at=source.startup_deadline_at,
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    current = ingress.requests[0]
    assert report.startup_timed_out == 1
    assert report.terminal_failed == 0
    assert current.status is IngressStatus.PENDING
    assert current.status_message_state is StatusMessageState.STARTUP_TIMEOUT
    assert ecs.update_calls == [1]
    assert trigger.calls == [source.interaction_id]


@pytest.mark.asyncio
async def test_ready_between_three_and_fifteen_minutes_requests_recovered_status() -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository(ready_runtime(at))
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 1, 0))
    value, _, _, _, trigger = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    report = await value.reconcile()

    assert report.startup_recovered == 1
    assert ingress.requests[0].status_message_state is StatusMessageState.RECOVERED
    assert ingress.recovery_calls == [source.interaction_id]
    assert trigger.calls == [source.interaction_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_count", [0, 1])
@pytest.mark.parametrize("runtime_status", [RuntimeStatus.READY, RuntimeStatus.BUSY])
async def test_stale_ready_owner_is_repaired_before_recovery(
    pending_count: int,
    runtime_status: RuntimeStatus,
) -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    stale = ready_runtime(at)
    if runtime_status is RuntimeStatus.BUSY:
        stale = stale.transition(RuntimeStatus.BUSY, at=at, runtime_instance_id="runtime-1")
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository(stale)
    runtime.wake_results.add(source.interaction_id)
    value, _, _, _, trigger = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 0, pending_count)),
    )

    report = await value.reconcile()

    assert report.startup_recovered == 0
    assert ingress.requests[0].status_message_state is StatusMessageState.STARTUP_TIMEOUT
    assert trigger.calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stale.generation + 1
    assert runtime.state.runtime_instance_id is None
    rebound = runtime.state.mark_started(
        at=runtime.state.updated_at + timedelta(microseconds=1),
        runtime_instance_id="runtime-2",
    )
    assert rebound.runtime_instance_id == "runtime-2"


@pytest.mark.asyncio
async def test_bound_starting_owner_is_repaired_when_replacement_is_pending() -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    bound = (
        RuntimeState.stopped(at=CREATED_AT)
        .request_wake(at=CREATED_AT)
        .mark_started(at=CREATED_AT, runtime_instance_id="runtime-old")
    )
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository(bound)
    runtime.wake_results.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 0, 1)),
    )

    report = await value.reconcile()

    assert report.startup_recovered == 0
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == bound.generation + 1
    assert runtime.state.runtime_instance_id is None


@pytest.mark.asyncio
async def test_runtime_repair_conflict_stays_conservative_until_next_pass() -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    stale = ready_runtime(at)
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository(stale)
    runtime.wake_results.add(source.interaction_id)
    runtime.replace_conflicts = 1
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    first, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)
    second, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    first_report = await first.reconcile()
    second_report = await second.reconcile()

    assert first_report.startup_recovered == 0
    assert first_report.conditional_conflicts == 1
    assert second_report.startup_recovered == 0
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING


@pytest.mark.asyncio
async def test_fourteen_fifty_nine_does_not_terminalize() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    value, _, _, _, _ = reconciler(
        at=source.created_at + timedelta(minutes=14, seconds=59),
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert report.terminal_failed == 0
    assert ingress.terminal_calls == []


@pytest.mark.asyncio
async def test_fifteen_minutes_terminalizes_before_wake_and_never_executes() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    value, _, ecs, _, trigger = reconciler(
        at=source.terminal_deadline_at,
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert report.terminal_failed == 1
    assert report.wake_candidates == 0
    assert runtime.ensure_calls == []
    assert ecs.describe_calls == 0
    assert ingress.requests[0].error_code == STARTUP_TERMINAL_DEADLINE_ERROR
    assert trigger.calls == [source.interaction_id]


@pytest.mark.asyncio
async def test_terminal_conflict_is_benign_only_after_request_stops_being_due() -> None:
    source = request()
    ingress = FakeIngress((source,))
    ingress.terminal_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=source.terminal_deadline_at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(),
    )

    report = await value.reconcile()

    assert report.terminal_failed == 0
    assert report.conditional_conflicts == 1
    assert ingress.requests == []


@pytest.mark.asyncio
async def test_terminal_conflict_is_raised_while_request_remains_due() -> None:
    source = request()
    ingress = FakeIngress((source,))
    ingress.terminal_persistent_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=source.terminal_deadline_at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(),
    )

    with pytest.raises(RepositoryConflict):
        await value.reconcile()


@pytest.mark.asyncio
async def test_duplicate_invocations_use_every_real_id_and_scale_only_once() -> None:
    sources = (request(1), request(2), request(3))
    ingress = FakeIngress(sources)
    runtime = FakeRuntimeRepository()
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    at = sources[0].created_at + timedelta(seconds=1)
    first, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)
    second, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    await first.reconcile()
    await second.reconcile()

    expected_ids = [source.interaction_id for source in sources]
    assert runtime.ensure_calls == [*expected_ids, *expected_ids]
    assert runtime.state is not None
    assert runtime.state.generation == len(sources)
    assert ecs.update_calls == [1]


@pytest.mark.asyncio
async def test_expired_claim_remains_a_wake_candidate_for_the_drainer() -> None:
    source = request()
    claim_expiry = source.created_at + timedelta(minutes=1)
    expired = replace(
        source,
        status=IngressStatus.CLAIMED,
        updated_at=source.created_at + timedelta(seconds=1),
        claim_owner="runtime-old",
        claim_expires_at=claim_expiry,
        delivery_attempt=1,
    )
    ingress = FakeIngress((expired,))
    runtime = FakeRuntimeRepository()
    value, _, _, _, _ = reconciler(
        at=claim_expiry,
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert report.wake_candidates == 1
    assert runtime.ensure_calls == [source.interaction_id]
    assert ingress.requests[0].status is IngressStatus.CLAIMED


@pytest.mark.asyncio
async def test_wake_conflict_is_raised_while_request_remains_active() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    runtime.ensure_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=source.created_at + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
    )

    with pytest.raises(RepositoryConflict):
        await value.reconcile()


@pytest.mark.asyncio
async def test_wake_conflict_is_benign_after_request_stops_being_active() -> None:
    source = request()
    ingress = FakeIngress((source,))

    class DisappearingWakeRuntime(FakeRuntimeRepository):
        async def ensure_wake(self, *, interaction_id: str, at: datetime) -> RuntimeState:
            self.ensure_calls.append(interaction_id)
            ingress.requests.clear()
            raise RepositoryConflict

    runtime = DisappearingWakeRuntime()
    value, _, _, _, _ = reconciler(
        at=source.created_at + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert report.wake_candidates == 1
    assert report.conditional_conflicts == 1


@pytest.mark.asyncio
async def test_startup_timeout_conflict_is_benign_only_after_request_stops_being_due() -> None:
    source = request()
    ingress = FakeIngress((source,))
    ingress.timeout_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=source.startup_deadline_at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(),
    )

    report = await value.reconcile()

    assert report.startup_timed_out == 0
    assert report.conditional_conflicts == 1
    assert ingress.requests == []


@pytest.mark.asyncio
async def test_startup_timeout_conflict_is_raised_while_request_remains_due() -> None:
    source = request()
    ingress = FakeIngress((source,))
    ingress.timeout_persistent_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=source.startup_deadline_at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(),
    )

    with pytest.raises(RepositoryConflict):
        await value.reconcile()


@pytest.mark.asyncio
async def test_ready_runtime_skips_statuses_that_never_timed_out() -> None:
    source = request()
    at = source.created_at + timedelta(minutes=10)
    ingress = FakeIngress((source,))
    value, _, _, _, trigger = reconciler(
        at=at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(ready_runtime(at)),
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 1, 0)),
    )

    report = await value.reconcile()

    assert report.startup_recovered == 0
    assert ingress.recovery_calls == []
    assert trigger.calls == []


@pytest.mark.asyncio
async def test_recovery_conflict_is_raised_while_timed_out_request_remains_ready() -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    ingress = FakeIngress((source,))
    ingress.recovery_persistent_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(ready_runtime(at)),
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 1, 0)),
    )

    with pytest.raises(RepositoryConflict):
        await value.reconcile()


@pytest.mark.asyncio
async def test_recovery_conflict_is_benign_after_request_stops_being_ready() -> None:
    source = replace(request(), status_message_state=StatusMessageState.STARTUP_TIMEOUT)
    at = source.created_at + timedelta(minutes=10)
    ingress = FakeIngress((source,))
    ingress.recovery_conflicts.add(source.interaction_id)
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=FakeRuntimeRepository(ready_runtime(at)),
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 1, 0)),
    )

    report = await value.reconcile()

    assert report.startup_recovered == 0
    assert report.conditional_conflicts == 1


@pytest.mark.asyncio
async def test_ecs_failure_keeps_dynamodb_wake_for_next_reconciliation() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    ecs.fail_describe = True
    value, _, _, _, _ = reconciler(
        at=source.created_at + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )

    with pytest.raises(EcsRuntimeUnavailable):
        await value.reconcile()

    assert runtime.state is not None
    assert runtime.state.desired_count == 1
    assert runtime.replace_calls == 0
    generation = runtime.state.generation

    ecs.fail_describe = False
    retry, _, _, _, _ = reconciler(
        at=source.created_at + timedelta(seconds=2),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    report = await retry.reconcile()

    assert report.ecs_scaled_up
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.generation == generation


@pytest.mark.asyncio
async def test_ecs_failure_at_three_minutes_still_persists_timeout_status() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    ecs.fail_describe = True
    value, _, _, _, trigger = reconciler(
        at=source.startup_deadline_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )

    with pytest.raises(EcsRuntimeUnavailable):
        await value.reconcile()

    assert ingress.requests[0].status is IngressStatus.PENDING
    assert ingress.requests[0].status_message_state is StatusMessageState.STARTUP_TIMEOUT
    assert trigger.calls == [source.interaction_id]


@pytest.mark.asyncio
async def test_ecs_failure_at_fifteen_minutes_still_terminalizes_and_kicks_status() -> None:
    source = request()
    ingress = FakeIngress((source,))
    starting = RuntimeState.stopped(at=CREATED_AT).request_wake(at=CREATED_AT)
    runtime = FakeRuntimeRepository(starting)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 1))
    ecs.fail_describe = True
    value, _, _, _, trigger = reconciler(
        at=source.terminal_deadline_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )

    with pytest.raises(EcsRuntimeUnavailable):
        await value.reconcile()

    assert ingress.requests[0].status is IngressStatus.FAILED
    assert ingress.requests[0].status_message_state is StatusMessageState.TERMINAL_FAILED
    assert trigger.calls == [source.interaction_id]


@pytest.mark.asyncio
async def test_ecs_success_and_runtime_cas_conflict_converge_on_next_invocation() -> None:
    source = request()
    ingress = FakeIngress((source,))
    runtime = FakeRuntimeRepository()
    runtime.replace_conflicts = 1
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    at = source.created_at + timedelta(seconds=1)
    first, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)
    second, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    first_report = await first.reconcile()
    second_report = await second.reconcile()

    assert first_report.ecs_scaled_up
    assert first_report.conditional_conflicts == 1
    assert second_report.runtime_reconciled
    assert ecs.update_calls == [1]


@pytest.mark.asyncio
async def test_due_status_sweep_attempts_every_item_before_reporting_failure() -> None:
    sources = (request(1), request(2), request(3))
    due = tuple(IngressStatusPublication.prepared(source, content="status") for source in sources)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository()
    statuses = FakeStatusRepository(due)
    trigger = FakeStatusTrigger()
    trigger.failures.add(sources[1].interaction_id)
    value, _, _, status_repository, status_trigger = reconciler(
        at=CREATED_AT + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
        statuses=statuses,
        trigger=trigger,
    )

    with pytest.raises(StatusTriggerUnavailable):
        await value.reconcile()

    assert status_repository.limits == [100]
    assert status_trigger.calls == [source.interaction_id for source in sources]


def test_reconciliation_report_rejects_invalid_counters() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        RuntimeReconciliationReport(observed_at=CREATED_AT, terminal_failed=-1)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        RuntimeReconciliationReport(observed_at=CREATED_AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="non-negative"):
        RuntimeReconciliationReport(observed_at=CREATED_AT, terminal_failed=cast(int, 1.5))


@pytest.mark.asyncio
async def test_complete_ready_runtime_enters_idle_once_without_scaling_down() -> None:
    at = CREATED_AT + timedelta(minutes=20)
    runtime = FakeRuntimeRepository(ready_runtime(at - timedelta(seconds=1)))
    ingress = FakeIngress(())
    value, _, ecs, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 1, 0)),
    )

    report = await value.reconcile()

    assert report.runtime_entered_idle
    assert not report.ecs_scaled_down
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == at
    assert ecs.update_calls == []


@pytest.mark.asyncio
async def test_idle_stops_at_thirty_minutes_but_not_one_microsecond_before() -> None:
    idle_at = CREATED_AT + timedelta(minutes=1)
    idle = ready_runtime(idle_at - timedelta(seconds=1)).begin_idle(at=idle_at)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(idle)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    before, _, _, _, _ = reconciler(
        at=idle_at + timedelta(minutes=30) - timedelta(microseconds=1),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )

    before_report = await before.reconcile()

    assert not before_report.ecs_scaled_down
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == idle_at

    at_deadline, _, _, _, _ = reconciler(
        at=idle_at + timedelta(minutes=30),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    deadline_report = await at_deadline.reconcile()

    assert deadline_report.ecs_scaled_down
    assert deadline_report.runtime_stopped
    assert ecs.update_calls == [0]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_status_only_does_not_start_fargate_and_cancels_unneeded_start() -> None:
    at = CREATED_AT + timedelta(minutes=16)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(RuntimeState.stopped(at=CREATED_AT).request_wake(at=CREATED_AT))
    activity = FakeActivity(ingress, (RuntimeActivity(pending_status_updates=1),))
    value, _, ecs, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        activity=activity,
    )

    report = await value.reconcile()

    assert not report.ecs_scaled_up
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_active_attempt_resumes_stopped_generation_and_scales_up() -> None:
    at = CREATED_AT + timedelta(minutes=1)
    ingress = FakeIngress(())
    stopped = RuntimeState.stopped(at=CREATED_AT)
    runtime = FakeRuntimeRepository(stopped)
    activity = FakeActivity(ingress, (RuntimeActivity(active_attempts=1),))
    value, _, ecs, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        activity=activity,
    )

    report = await value.reconcile()

    assert report.ecs_scaled_up
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopped.generation + 1
    assert runtime.state.last_request_at is None


@pytest.mark.asyncio
async def test_terminal_deadline_does_not_scale_up_after_last_work_disappears() -> None:
    source = request()
    at = source.terminal_deadline_at
    ingress = FakeIngress((source,))
    starting = RuntimeState.stopped(at=source.created_at).request_wake(at=source.created_at)
    runtime = FakeRuntimeRepository(starting)
    value, _, ecs, _, trigger = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
    )

    report = await value.reconcile()

    assert report.terminal_failed == 1
    assert not report.ecs_scaled_up
    assert ecs.update_calls == []
    assert trigger.calls == [source.interaction_id]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_bound_ready_degraded_runtime_enters_idle_after_work_completes() -> None:
    at = CREATED_AT + timedelta(minutes=20)
    ready = ready_runtime(at - timedelta(seconds=3))
    busy = ready.transition(RuntimeStatus.BUSY, at=at - timedelta(seconds=2))
    degraded = busy.transition(
        RuntimeStatus.DEGRADED,
        at=at - timedelta(seconds=1),
        error_code="discord_not_ready",
    )
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(degraded)
    value, _, ecs, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=FakeEcs(EcsRuntimeSnapshot(1, 1, 0)),
    )

    report = await value.reconcile()

    assert report.runtime_entered_idle
    assert not report.ecs_scaled_down
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == at


@pytest.mark.asyncio
async def test_unbound_degraded_runtime_uses_fenced_stop_instead_of_entering_idle() -> None:
    degraded = RuntimeState.stopped(at=CREATED_AT).request_wake(
        at=CREATED_AT + timedelta(seconds=1)
    )
    degraded = degraded.transition(
        RuntimeStatus.DEGRADED,
        at=CREATED_AT + timedelta(seconds=2),
        error_code="startup_failed",
    )
    at = CREATED_AT + timedelta(seconds=3)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(degraded)
    value, _, ecs, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=FakeEcs(EcsRuntimeSnapshot(0, 0, 0)),
    )

    report = await value.reconcile()

    assert report.runtime_stopped
    assert not report.runtime_entered_idle
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_status_only_activity_resets_idle_timer_without_waking_fargate() -> None:
    idle_at = CREATED_AT + timedelta(minutes=1)
    idle = ready_runtime(idle_at - timedelta(seconds=1)).begin_idle(at=idle_at)
    status_at = idle_at + timedelta(minutes=29)
    completed_at = status_at + timedelta(seconds=1)
    old_stop_deadline = idle_at + timedelta(minutes=30)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(idle)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 1, 0))
    status_activity = FakeActivity(
        ingress,
        (RuntimeActivity(pending_status_updates=1),),
    )
    interrupted, _, _, _, _ = reconciler(
        at=status_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=status_activity,
    )

    interrupted_report = await interrupted.reconcile()

    assert not interrupted_report.ecs_scaled_up
    assert not interrupted_report.ecs_scaled_down
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.READY
    assert runtime.state.idle_since is None
    assert runtime.state.stop_eligible_at is None

    completed, _, _, _, _ = reconciler(
        at=completed_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    completed_report = await completed.reconcile()

    assert completed_report.runtime_entered_idle
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == completed_at
    assert runtime.state.stop_eligible_at == completed_at + timedelta(minutes=30)

    old_deadline, _, _, _, _ = reconciler(
        at=old_stop_deadline,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    old_deadline_report = await old_deadline.reconcile()

    assert not old_deadline_report.ecs_scaled_down
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == completed_at


@pytest.mark.asyncio
async def test_stopping_with_status_only_work_never_restarts_fargate() -> None:
    idle_at = CREATED_AT + timedelta(minutes=1)
    idle = ready_runtime(idle_at - timedelta(seconds=1)).begin_idle(at=idle_at)
    assert idle.stop_eligible_at is not None
    stopping = idle.begin_idle_stop(at=idle.stop_eligible_at)
    first_at = stopping.updated_at + timedelta(seconds=1)
    second_at = first_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 1, 0))
    status_only = RuntimeActivity(pending_status_updates=1)
    activity = FakeActivity(ingress, (status_only,) * 6)
    first, _, _, _, _ = reconciler(
        at=first_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    first_report = await first.reconcile()

    assert first_report.ecs_scaled_down
    assert not first_report.runtime_stopped
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPING
    assert ecs.update_calls == [0]

    ecs.snapshot = EcsRuntimeSnapshot(0, 0, 0)
    second, _, _, _, _ = reconciler(
        at=second_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )
    second_report = await second.reconcile()

    assert second_report.runtime_stopped
    assert not second_report.ecs_scaled_up
    assert ecs.update_calls == [0]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_request_winning_the_idle_stop_fence_prevents_scale_down() -> None:
    """A wake committed after the first activity read must win the stop CAS."""

    idle_at = CREATED_AT + timedelta(minutes=1)
    idle = ready_runtime(idle_at - timedelta(seconds=1)).begin_idle(at=idle_at)
    assert idle.stop_eligible_at is not None
    at = idle.stop_eligible_at
    ingress = FakeIngress(())

    class WakeDuringStopFenceRepository(FakeRuntimeRepository):
        async def begin_idle_stop(
            self,
            *,
            expected: RuntimeState,
            at: datetime,
        ) -> RuntimeState:
            assert self.state == expected
            self.state = expected.request_wake(at=at)
            raise RepositoryConflict

    runtime = WakeDuringStopFenceRepository(idle)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 1, 0))
    value, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    report = await value.reconcile()

    assert report.conditional_conflicts == 1
    assert not report.ecs_scaled_down
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == idle.generation + 1
    assert runtime.state.desired_count == 1


@pytest.mark.asyncio
async def test_work_seen_after_stop_fence_resumes_before_scale_down() -> None:
    """The activity recheck after STOPPING must restore desired one first."""

    stopping = stopping_runtime()
    at = stopping.updated_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    activity = FakeActivity(
        ingress,
        (RuntimeActivity(), RuntimeActivity(active_attempts=1)),
    )
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    report = await value.reconcile()

    assert not report.ecs_scaled_down
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopping.generation + 1
    assert runtime.state.desired_count == 1


@pytest.mark.asyncio
async def test_work_seen_after_scale_down_is_immediately_scaled_back_up() -> None:
    """A request appearing after UpdateService(0) must restore desired one."""

    stopping = stopping_runtime()
    at = stopping.updated_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    activity = FakeActivity(
        ingress,
        (
            RuntimeActivity(),
            RuntimeActivity(),
            RuntimeActivity(pending_ingress=1),
        ),
    )
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    report = await value.reconcile()

    assert report.ecs_scaled_down
    assert not report.runtime_stopped
    assert ecs.update_calls == [0, 1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopping.generation + 1
    assert runtime.state.desired_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wake_on_inspection", "expected_updates"),
    ((2, [1]), (3, [0, 1])),
    ids=("before-update", "after-update"),
)
async def test_wake_state_fences_stale_activity_around_scale_down(
    wake_on_inspection: int,
    expected_updates: list[int],
) -> None:
    """A generation change is authoritative even when activity reads are stale."""

    stopping = stopping_runtime()
    at = stopping.updated_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)

    class WakeOnInspectionActivity(FakeActivity):
        async def inspect(self, *, at: datetime) -> RuntimeActivity:
            snapshot = await super().inspect(at=at)
            if len(self.calls) == wake_on_inspection:
                assert runtime.state is not None
                runtime.state = runtime.state.request_wake(at=max(at, runtime.state.updated_at))
            return snapshot

    activity = WakeOnInspectionActivity(
        ingress,
        (RuntimeActivity(), RuntimeActivity(), RuntimeActivity()),
    )
    ecs = FakeEcs(EcsRuntimeSnapshot(1 if wake_on_inspection == 3 else 0, 0, 0))
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    report = await value.reconcile()

    assert ecs.update_calls == expected_updates
    assert report.conditional_conflicts == 1
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopping.generation + 1
    assert runtime.state.desired_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("apply_before_error", "expected_updates"),
    ((False, [0, 0]), (True, [0])),
    ids=("request-failed", "response-lost"),
)
async def test_scale_down_failure_converges_on_the_next_pass(
    apply_before_error: bool,
    expected_updates: list[int],
) -> None:
    """STOPPING survives both a rejected update and a lost successful response."""

    stopping = stopping_runtime()
    at = stopping.updated_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)
    ecs = FailOnceScaleDownEcs(
        EcsRuntimeSnapshot(1, 0, 0),
        apply_before_error=apply_before_error,
    )
    first, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    with pytest.raises(EcsRuntimeUnavailable):
        await first.reconcile()

    assert runtime.state == stopping

    second, _, _, _, _ = reconciler(
        at=at + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    second_report = await second.reconcile()

    assert second_report.runtime_stopped
    assert ecs.update_calls == expected_updates
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED

    duplicate, _, _, _, _ = reconciler(
        at=at + timedelta(seconds=2),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    duplicate_report = await duplicate.reconcile()

    assert duplicate_report.runtime_reconciled
    assert ecs.update_calls == expected_updates
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED


@pytest.mark.asyncio
async def test_stopped_runtime_repairs_stale_ecs_desired_count() -> None:
    """A prior DynamoDB stop may be safely replayed after ECS update failure."""

    at = CREATED_AT + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(RuntimeState.stopped(at=CREATED_AT))
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    value, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    report = await value.reconcile()

    assert report.ecs_scaled_down
    assert ecs.update_calls == [0]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED
    assert runtime.state.desired_count == 0


@pytest.mark.asyncio
async def test_stopped_runtime_rechecks_work_before_repairing_ecs_down() -> None:
    """A request after the first activity read prevents stale desired-zero repair."""

    at = CREATED_AT + timedelta(seconds=1)
    stopped = RuntimeState.stopped(at=CREATED_AT)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopped)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    activity = FakeActivity(
        ingress,
        (RuntimeActivity(), RuntimeActivity(pending_ingress=1)),
    )
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    report = await value.reconcile()

    assert not report.ecs_scaled_down
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopped.generation + 1


@pytest.mark.asyncio
async def test_stopped_transition_conflict_converges_on_the_next_pass() -> None:
    """Concurrent STOPPED CAS loss remains STOPPING until a later pass wins."""

    stopping = stopping_runtime()
    at = stopping.updated_at + timedelta(seconds=1)
    ingress = FakeIngress(())
    runtime = FakeRuntimeRepository(stopping)
    runtime.replace_conflicts = 1
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    first, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    first_report = await first.reconcile()

    assert first_report.conditional_conflicts == 1
    assert not first_report.runtime_stopped
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPING

    second, _, _, _, _ = reconciler(
        at=at + timedelta(seconds=1),
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    second_report = await second.reconcile()

    assert second_report.runtime_stopped
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STOPPED
    assert ecs.update_calls == []


@pytest.mark.asyncio
async def test_concurrent_resumer_wins_without_losing_scale_up() -> None:
    """Two reconcilers may race to resume one STOPPED generation safely."""

    at = CREATED_AT + timedelta(seconds=1)
    stopped = RuntimeState.stopped(at=CREATED_AT)
    ingress = FakeIngress(())

    class ConcurrentResumeRepository(FakeRuntimeRepository):
        async def replace(
            self,
            *,
            expected: RuntimeState,
            updated: RuntimeState,
        ) -> RuntimeState:
            if expected.status is RuntimeStatus.STOPPED:
                self.state = updated
                raise RepositoryConflict
            return await super().replace(expected=expected, updated=updated)

    runtime = ConcurrentResumeRepository(stopped)
    ecs = FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    activity = FakeActivity(ingress, (RuntimeActivity(active_attempts=1),))
    value, _, _, _, _ = reconciler(
        at=at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=activity,
    )

    report = await value.reconcile()

    assert report.conditional_conflicts == 1
    assert report.ecs_scaled_up
    assert ecs.update_calls == [1]
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopped.generation + 1


@pytest.mark.asyncio
async def test_stale_stopped_observation_yields_to_a_concurrent_wake() -> None:
    """A stale reconciler must not scale down after another invocation wakes."""

    at = CREATED_AT + timedelta(seconds=1)
    stopped = RuntimeState.stopped(at=CREATED_AT)
    ingress = FakeIngress(())

    class WakeOnSecondReadRepository(FakeRuntimeRepository):
        def __init__(self, state: RuntimeState) -> None:
            super().__init__(state)
            self.get_calls = 0

        async def get(self) -> RuntimeState | None:
            self.get_calls += 1
            if self.get_calls == 2:
                assert self.state is not None
                self.state = self.state.request_wake(at=at)
            return self.state

    runtime = WakeOnSecondReadRepository(stopped)
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 0, 0))
    value, _, _, _, _ = reconciler(at=at, ingress=ingress, runtime=runtime, ecs=ecs)

    report = await value.reconcile()

    assert report.conditional_conflicts == 1
    assert not report.ecs_scaled_down
    assert ecs.update_calls == []
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.STARTING
    assert runtime.state.generation == stopped.generation + 1


@pytest.mark.asyncio
async def test_status_activity_and_idle_cas_conflict_converge_without_early_stop() -> None:
    """External publication and one lost idle CAS both delay the idle deadline."""

    first_at = CREATED_AT + timedelta(minutes=20)
    runtime = FakeRuntimeRepository(ready_runtime(first_at - timedelta(seconds=1)))
    ingress = FakeIngress(())
    ecs = FakeEcs(EcsRuntimeSnapshot(1, 1, 0))
    statuses_pending = FakeActivity(
        ingress,
        (RuntimeActivity(pending_status_updates=1),),
    )
    first, _, _, _, _ = reconciler(
        at=first_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
        activity=statuses_pending,
    )

    first_report = await first.reconcile()

    assert not first_report.runtime_entered_idle
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.READY

    runtime.replace_conflicts = 1
    second_at = first_at + timedelta(seconds=1)
    second, _, _, _, _ = reconciler(
        at=second_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    second_report = await second.reconcile()

    assert second_report.conditional_conflicts == 1
    assert not second_report.runtime_entered_idle
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.READY

    third_at = second_at + timedelta(seconds=1)
    third, _, _, _, _ = reconciler(
        at=third_at,
        ingress=ingress,
        runtime=runtime,
        ecs=ecs,
    )
    third_report = await third.reconcile()

    assert third_report.runtime_entered_idle
    assert runtime.state is not None
    assert runtime.state.status is RuntimeStatus.IDLE
    assert runtime.state.idle_since == third_at
    assert ecs.update_calls == []
