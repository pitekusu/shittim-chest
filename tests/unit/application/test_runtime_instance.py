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
    replacements: list[tuple[RuntimeState, RuntimeState]] = field(default_factory=list)

    async def get(self) -> RuntimeState | None:
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
            if self.state is not None:
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


def session(repository: FakeRuntimeRepository, *, owner: str = "runtime-a") -> RuntimeInstanceState:
    return RuntimeInstanceState(
        clock=FakeClock(),
        repository=repository,
        runtime_instance_id=owner,
    )


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
async def test_foreign_or_missing_runtime_state_fails_closed() -> None:
    foreign = starting().mark_started(at=NOW, runtime_instance_id="runtime-b")

    with pytest.raises(RuntimeNotReady, match="another instance"):
        await session(FakeRuntimeRepository(foreign)).mark_started()
    with pytest.raises(RuntimeNotReady, match="missing"):
        await session(FakeRuntimeRepository(None)).mark_started()


@pytest.mark.asyncio
async def test_nonready_state_cannot_be_reopened_silently() -> None:
    stopped = RuntimeState.stopped(at=NOW)

    with pytest.raises(RuntimeNotReady, match="not starting"):
        await session(FakeRuntimeRepository(stopped)).mark_started()


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
