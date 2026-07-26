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


def reconciler(
    *,
    at: datetime,
    ingress: FakeIngress,
    runtime: FakeRuntimeRepository,
    ecs: FakeEcs | None = None,
    statuses: FakeStatusRepository | None = None,
    trigger: FakeStatusTrigger | None = None,
) -> tuple[RuntimeReconciler, FakeClock, FakeEcs, FakeStatusRepository, FakeStatusTrigger]:
    clock = FakeClock(at)
    ecs = ecs or FakeEcs(EcsRuntimeSnapshot(0, 0, 0))
    statuses = statuses or FakeStatusRepository()
    trigger = trigger or FakeStatusTrigger()
    value = RuntimeReconciler(
        clock=cast(Clock, clock),
        ingress=cast(IngressRepository, ingress),
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
