"""Runtime instance ownership tests without AWS access."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application.errors import RuntimeNotReady
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.runtime_instance import RuntimeInstanceState
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
PROMPT_REVISION = "r" + "0" * 26


@dataclass(slots=True)
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


@dataclass(slots=True)
class FakeRuntimeRepository:
    state: RuntimeState | None
    conflicts: int = 0
    conflict_state: RuntimeState | None = None
    read_error: Exception | None = None
    replacements: list[tuple[RuntimeState, RuntimeState]] = field(default_factory=list)

    async def get(self) -> RuntimeState | None:
        if self.read_error is not None:
            raise self.read_error
        return self.state

    async def replace(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
    ) -> RuntimeState:
        self.replacements.append((expected, updated))
        if self.conflicts:
            self.conflicts -= 1
            if self.conflict_state is not None:
                self.state = self.conflict_state
            elif self.state is not None:
                self.state = self.state.request_wake(at=updated.updated_at)
            raise RepositoryConflict("race")
        if self.state != expected:
            raise RepositoryConflict("stale")
        self.state = updated
        return updated


def starting() -> RuntimeState:
    return RuntimeState.stopped(at=NOW - timedelta(minutes=1)).request_wake(
        at=NOW - timedelta(seconds=30)
    )


def session(
    repository: FakeRuntimeRepository,
    *,
    owner: str = "runtime-a",
    prompt_revision: str | None = None,
    cas_attempts: int = 5,
) -> RuntimeInstanceState:
    return RuntimeInstanceState(
        clock=FakeClock(),
        repository=repository,
        runtime_instance_id=owner,
        runtime_prompt_revision=prompt_revision,
        cas_attempts=cas_attempts,
    )


def owned_runtime(status: RuntimeStatus, *, owner: str = "runtime-a") -> RuntimeState:
    started = starting().mark_started(at=NOW, runtime_instance_id=owner)
    if status is RuntimeStatus.STARTING:
        return started
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=1),
        runtime_instance_id=owner,
    )
    if status is RuntimeStatus.READY:
        return ready
    busy = ready.transition(RuntimeStatus.BUSY, at=NOW + timedelta(seconds=2))
    if status is RuntimeStatus.BUSY:
        return busy
    if status is RuntimeStatus.IDLE:
        return ready.begin_idle(at=NOW + timedelta(seconds=2))
    if status is RuntimeStatus.DEGRADED:
        return busy.transition(
            RuntimeStatus.DEGRADED,
            at=NOW + timedelta(seconds=3),
            error_code="runtime_not_ready",
        )
    idle = ready.begin_idle(at=NOW + timedelta(seconds=2))
    if status is RuntimeStatus.STOPPING:
        return idle.begin_idle_stop(at=idle.stop_eligible_at or NOW)
    raise ValueError(f"unsupported owned runtime status: {status}")


@pytest.mark.asyncio
async def test_start_and_ready_bind_one_runtime_after_recovery() -> None:
    repository = FakeRuntimeRepository(starting())
    runtime = session(repository)

    started = await runtime.mark_started()
    ready = await runtime.mark_ready(active=False)

    assert started.status is RuntimeStatus.STARTING
    assert started.runtime_instance_id == "runtime-a"
    assert ready.status is RuntimeStatus.READY
    assert ready.runtime_instance_id == "runtime-a"
    assert ready.ready_at is not None


@pytest.mark.asyncio
async def test_start_atomically_records_the_loaded_prompt_revision() -> None:
    repository = FakeRuntimeRepository(starting())
    runtime = session(repository, prompt_revision=PROMPT_REVISION)

    started = await runtime.mark_started()
    ready = await runtime.mark_ready(active=False)

    assert started.runtime_prompt_revision == PROMPT_REVISION
    assert ready.runtime_prompt_revision == PROMPT_REVISION
    assert repository.replacements[0][0].runtime_prompt_revision is None
    assert repository.replacements[0][1].runtime_prompt_revision == PROMPT_REVISION


@pytest.mark.asyncio
async def test_same_owner_cannot_silently_change_its_loaded_prompt_revision() -> None:
    bound = starting().mark_started(
        at=NOW,
        runtime_instance_id="runtime-a",
        runtime_prompt_revision=PROMPT_REVISION,
    )

    with pytest.raises(RuntimeNotReady, match="prompt revision"):
        await session(FakeRuntimeRepository(bound)).mark_started()


def test_fresh_generation_clears_the_previous_prompt_revision() -> None:
    ready = (
        starting()
        .mark_started(
            at=NOW,
            runtime_instance_id="runtime-a",
            runtime_prompt_revision=PROMPT_REVISION,
        )
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="runtime-a",
        )
    )

    fenced = ready.fence_stale_instance(at=NOW + timedelta(seconds=2))

    assert fenced.runtime_instance_id is None
    assert fenced.runtime_prompt_revision is None


@pytest.mark.asyncio
async def test_recovered_work_opens_runtime_as_busy_and_calls_are_idempotent() -> None:
    repository = FakeRuntimeRepository(starting())
    runtime = session(repository)

    await runtime.mark_started()
    busy = await runtime.mark_ready(active=True)
    replay = await runtime.mark_busy()

    assert busy.status is RuntimeStatus.BUSY
    assert replay == busy


@pytest.mark.asyncio
async def test_concurrent_wake_cas_is_retried_without_changing_owner() -> None:
    repository = FakeRuntimeRepository(starting(), conflicts=1)
    runtime = session(repository)

    started = await runtime.mark_started()

    assert started.runtime_instance_id == "runtime-a"
    assert started.generation == 2
    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_live_runtime_claims_an_idle_wake_and_latest_generation() -> None:
    active = (
        starting()
        .mark_started(at=NOW, runtime_instance_id="runtime-a")
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="runtime-a",
        )
    )
    idle = active.begin_idle(at=NOW + timedelta(seconds=2))
    woken = idle.request_wake(at=NOW + timedelta(seconds=3))
    repository = FakeRuntimeRepository(woken, conflicts=1)

    claimed = await session(repository).claim_woken_start()

    assert claimed
    assert repository.state is not None
    assert repository.state.status is RuntimeStatus.STARTING
    assert repository.state.runtime_instance_id == "runtime-a"
    assert repository.state.generation == woken.generation + 1
    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_live_runtime_does_not_mutate_a_nonstarting_generation() -> None:
    bound = (
        starting()
        .mark_started(at=NOW, runtime_instance_id="runtime-a")
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="runtime-a",
        )
    )
    repository = FakeRuntimeRepository(bound)

    assert not await session(repository).claim_woken_start()
    assert repository.replacements == []


@pytest.mark.asyncio
async def test_live_runtime_cannot_claim_a_foreign_starting_generation() -> None:
    foreign = starting().mark_started(at=NOW, runtime_instance_id="runtime-b")

    with pytest.raises(RuntimeNotReady, match="another instance"):
        await session(FakeRuntimeRepository(foreign)).claim_woken_start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [RuntimeStatus.READY, RuntimeStatus.BUSY],
)
async def test_replacement_ecs_task_fences_foreign_physical_owner(
    status: RuntimeStatus,
) -> None:
    foreign = starting().mark_started(at=NOW, runtime_instance_id="ecs-task-old")
    if status is RuntimeStatus.READY:
        foreign = foreign.transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="ecs-task-old",
        )
    elif status is RuntimeStatus.BUSY:
        foreign = foreign.transition(
            RuntimeStatus.BUSY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="ecs-task-old",
        )
    repository = FakeRuntimeRepository(foreign)

    rebound = await session(repository, owner="ecs-task-new").mark_started()

    assert rebound.status is RuntimeStatus.STARTING
    assert rebound.runtime_instance_id == "ecs-task-new"
    assert rebound.generation == foreign.generation + 1
    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_replacement_ecs_task_fences_foreign_bound_starting_owner() -> None:
    foreign = starting().mark_started(at=NOW, runtime_instance_id="ecs-task-other")
    repository = FakeRuntimeRepository(foreign)

    rebound = await session(repository, owner="ecs-task-current").mark_started()

    assert rebound.status is RuntimeStatus.STARTING
    assert rebound.runtime_instance_id == "ecs-task-current"
    assert rebound.generation == foreign.generation + 1
    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_same_live_ecs_task_owner_is_not_repaired() -> None:
    ready = (
        starting()
        .mark_started(at=NOW, runtime_instance_id="ecs-task-live")
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="ecs-task-live",
        )
    )
    repository = FakeRuntimeRepository(ready)

    replay = await session(repository, owner="ecs-task-live").mark_started()

    assert replay == ready
    assert repository.replacements == []


@pytest.mark.asyncio
async def test_replacement_loser_does_not_fence_the_new_physical_owner() -> None:
    old = (
        starting()
        .mark_started(at=NOW, runtime_instance_id="ecs-task-old")
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="ecs-task-old",
        )
    )
    competitor = (
        old.fence_stale_instance(at=NOW + timedelta(seconds=2))
        .mark_started(
            at=NOW + timedelta(seconds=3),
            runtime_instance_id="ecs-task-competitor",
        )
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=4),
            runtime_instance_id="ecs-task-competitor",
        )
    )
    repository = FakeRuntimeRepository(old, conflicts=1, conflict_state=competitor)

    with pytest.raises(RuntimeNotReady, match="ownership race"):
        await session(repository, owner="ecs-task-loser").mark_started()

    assert repository.state == competitor
    assert len(repository.replacements) == 1


@pytest.mark.asyncio
async def test_unbound_bind_loser_does_not_repair_the_cas_winner() -> None:
    unbound = starting()
    winner = unbound.mark_started(
        at=NOW,
        runtime_instance_id="ecs-task-winner",
    )
    repository = FakeRuntimeRepository(unbound, conflicts=1, conflict_state=winner)

    with pytest.raises(RuntimeNotReady, match="ownership race"):
        await session(repository, owner="ecs-task-loser").mark_started()

    assert repository.state == winner
    assert len(repository.replacements) == 1


@pytest.mark.asyncio
async def test_missing_runtime_state_fails_closed() -> None:
    with pytest.raises(RuntimeNotReady, match="missing"):
        await session(FakeRuntimeRepository(None)).mark_started()


@pytest.mark.asyncio
async def test_nonready_state_cannot_be_reopened_silently() -> None:
    stopped = RuntimeState.stopped(at=NOW)

    with pytest.raises(RuntimeNotReady, match="not starting"):
        await session(FakeRuntimeRepository(stopped)).mark_started()


@pytest.mark.asyncio
async def test_owned_stopping_generation_converges_to_stopped_at_shutdown() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    stopping = owned_runtime(RuntimeStatus.STOPPING)
    repository.state = stopping

    stopped = await runtime.mark_shutdown_complete()
    replay = await runtime.mark_shutdown_complete()

    assert stopped.status is RuntimeStatus.STOPPED
    assert stopped.generation == stopping.generation
    assert stopped.version == stopping.version + 1
    assert stopped.runtime_instance_id is None
    assert stopped.desired_count == 0
    assert replay == stopped
    assert len(repository.replacements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [RuntimeStatus.STARTING, RuntimeStatus.READY, RuntimeStatus.BUSY],
)
async def test_unexpected_live_shutdown_fences_a_fresh_unbound_generation(
    status: RuntimeStatus,
) -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    live = owned_runtime(status)
    repository.state = live

    replacement = await runtime.mark_shutdown_complete()

    assert replacement == live.fence_stale_instance(at=replacement.updated_at)
    assert replacement.status is RuntimeStatus.STARTING
    assert replacement.generation == live.generation + 1
    assert replacement.runtime_instance_id is None
    assert replacement.desired_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RuntimeStatus.IDLE, RuntimeStatus.DEGRADED])
async def test_owned_idle_or_degraded_shutdown_requests_a_fresh_runtime(
    status: RuntimeStatus,
) -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    inactive = owned_runtime(status)
    repository.state = inactive

    replacement = await runtime.mark_shutdown_complete()

    assert replacement == inactive.resume_for_work(at=replacement.updated_at)
    assert replacement.status is RuntimeStatus.STARTING
    assert replacement.generation == inactive.generation + 1
    assert replacement.runtime_instance_id is None
    assert replacement.desired_count == 1


@pytest.mark.asyncio
async def test_shutdown_does_not_overwrite_a_newer_request_generation() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    ready = owned_runtime(RuntimeStatus.READY)
    newer = ready.request_wake(at=ready.updated_at + timedelta(seconds=1))
    repository.state = newer

    observed = await runtime.mark_shutdown_complete()

    assert observed == newer
    assert repository.state == newer
    assert repository.replacements == []


@pytest.mark.asyncio
async def test_shutdown_does_not_overwrite_a_replacement_owner() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    ready = owned_runtime(RuntimeStatus.READY)
    replacement = ready.fence_stale_instance(
        at=ready.updated_at + timedelta(seconds=1)
    ).mark_started(
        at=ready.updated_at + timedelta(seconds=2),
        runtime_instance_id="runtime-b",
    )
    repository.state = replacement

    observed = await runtime.mark_shutdown_complete()

    assert observed == replacement
    assert repository.state == replacement
    assert repository.replacements == []


@pytest.mark.asyncio
async def test_shutdown_cas_loss_to_a_new_request_is_a_safe_noop() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    ready = owned_runtime(RuntimeStatus.READY)
    newer = ready.request_wake(at=ready.updated_at + timedelta(seconds=1))
    repository.state = ready
    repository.conflicts = 1
    repository.conflict_state = newer

    observed = await runtime.mark_shutdown_complete()

    assert observed == newer
    assert repository.state == newer
    assert len(repository.replacements) == 1


@pytest.mark.asyncio
async def test_shutdown_retries_same_generation_cas_conflicts() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    ready = owned_runtime(RuntimeStatus.READY)
    reconciled = ready.record_reconciled(at=ready.updated_at + timedelta(seconds=1))
    repository.state = ready
    repository.conflicts = 1
    repository.conflict_state = reconciled

    replacement = await runtime.mark_shutdown_complete()

    assert replacement.status is RuntimeStatus.STARTING
    assert replacement.generation == ready.generation + 1
    assert replacement.runtime_instance_id is None
    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_shutdown_fails_after_bounded_same_generation_cas_conflicts() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository, cas_attempts=2)
    await runtime.mark_started()
    ready = owned_runtime(RuntimeStatus.READY)
    repository.state = ready
    repository.conflicts = 2
    repository.conflict_state = ready.record_reconciled(at=ready.updated_at + timedelta(seconds=1))

    with pytest.raises(RuntimeNotReady, match="repeated state races"):
        await runtime.mark_shutdown_complete()

    assert len(repository.replacements) == 2


@pytest.mark.asyncio
async def test_shutdown_missing_or_corrupt_state_fails_closed() -> None:
    repository = FakeRuntimeRepository(owned_runtime(RuntimeStatus.STARTING))
    runtime = session(repository)
    await runtime.mark_started()
    repository.state = None

    with pytest.raises(RuntimeNotReady, match="missing"):
        await runtime.mark_shutdown_complete()

    repository.read_error = ValueError("corrupt runtime state")
    with pytest.raises(ValueError, match="corrupt runtime state"):
        await runtime.mark_shutdown_complete()


@pytest.mark.asyncio
async def test_shutdown_same_owner_without_observed_generation_fails_closed() -> None:
    ready = owned_runtime(RuntimeStatus.READY)
    repository = FakeRuntimeRepository(ready)

    with pytest.raises(RuntimeNotReady, match="no owned generation"):
        await session(repository).mark_shutdown_complete()

    assert repository.state == ready
    assert repository.replacements == []


def test_runtime_instance_rejects_empty_owner() -> None:
    with pytest.raises(ValueError):
        RuntimeInstanceState(
            clock=FakeClock(),
            repository=FakeRuntimeRepository(starting()),
            runtime_instance_id=" ",
        )


@pytest.mark.parametrize(("attempts", "exception"), [(0, ValueError), (True, TypeError)])
def test_runtime_instance_rejects_invalid_cas_attempts(
    attempts: int,
    exception: type[Exception],
) -> None:
    with pytest.raises(exception):
        RuntimeInstanceState(
            clock=FakeClock(),
            repository=FakeRuntimeRepository(starting()),
            runtime_instance_id="runtime-a",
            cas_attempts=attempts,
        )
