"""Lifecycle-independent tests for the IDLE farewell coordinator."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus
from shittim_chest.domain import ParticipantSlot
from shittim_chest.runtime.farewell import IdleFarewellCoordinator

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
CONTENT = (
    "東京の夏空と今日の楽しい科学ニュースに元気をもらいました。"
    "みんなと過ごせて本当にうれしいです!それでは素敵な夜を、"
    "また元気に集まりましょう!"
)


@dataclass(slots=True)
class Clock:
    current: datetime

    def now(self) -> datetime:
        return self.current


@dataclass(slots=True)
class Repository:
    state: RuntimeState | None

    async def get(self) -> RuntimeState | None:
        return self.state


@dataclass(slots=True)
class Generator:
    calls: list[ParticipantSlot] = field(default_factory=list)
    failure: Exception | None = None

    async def generate(self, *, participant: ParticipantSlot, time_context: object) -> str:
        del time_context
        self.calls.append(participant)
        if self.failure is not None:
            raise self.failure
        return CONTENT


@dataclass(slots=True)
class Sender:
    calls: list[tuple[ParticipantSlot, str, str]] = field(default_factory=list)
    failure: Exception | None = None

    async def send(self, *, participant: ParticipantSlot, content: str, nonce: str) -> None:
        self.calls.append((participant, content, nonce))
        if self.failure is not None:
            raise self.failure


@dataclass(slots=True)
class Telemetry:
    events: list[tuple[str, dict[str, str | int]]] = field(default_factory=list)

    def runtime_event(self, event: str, **fields: str | int) -> None:
        self.events.append((event, fields))


def idle_state(*, idle_at: datetime = NOW, owner: str = "runtime-a") -> RuntimeState:
    return (
        RuntimeState.stopped(at=idle_at - timedelta(minutes=1))
        .request_wake(at=idle_at - timedelta(seconds=50))
        .mark_started(at=idle_at - timedelta(seconds=40), runtime_instance_id=owner)
        .transition(
            RuntimeStatus.READY,
            at=idle_at - timedelta(seconds=30),
            runtime_instance_id=owner,
        )
        .begin_idle(at=idle_at)
    )


def coordinator(
    *,
    clock: Clock,
    repository: Repository,
    generator: Generator,
    sender: Sender,
    telemetry: Telemetry,
) -> IdleFarewellCoordinator:
    return IdleFarewellCoordinator(
        clock=clock,
        runtime_state=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
        random_source=random.Random(7),  # noqa: S311 - deterministic test selection
        poll_seconds=60,
    )


@pytest.mark.asyncio
async def test_generates_once_at_twenty_eight_minutes_from_three_participants() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=27, seconds=59))
    repository = Repository(state)
    generator = Generator()
    sender = Sender()
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()
    assert generator.calls == []

    clock.current = NOW + timedelta(minutes=28)
    await service.prepare_once()
    await service.prepare_once()

    assert len(generator.calls) == 1
    assert generator.calls[0] in tuple(ParticipantSlot)
    assert generator.calls[0] is not None


@pytest.mark.asyncio
async def test_new_work_discards_candidate_and_manual_shutdown_sends_nothing() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=28))
    repository = Repository(state)
    generator = Generator()
    sender = Sender()
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )
    await service.prepare_once()

    repository.state = state.request_wake(at=clock.current + timedelta(seconds=1))
    await service.prepare_once()
    await service.deliver_before_shutdown()

    assert sender.calls == []


@pytest.mark.asyncio
async def test_normal_idle_stopping_delivers_exactly_once() -> None:
    state = idle_state()
    deadline = state.stop_eligible_at
    assert deadline is not None
    clock = Clock(NOW + timedelta(minutes=28))
    repository = Repository(state)
    generator = Generator()
    sender = Sender()
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )
    await service.prepare_once()

    repository.state = state.begin_idle_stop(at=deadline)
    clock.current = deadline
    await service.deliver_before_shutdown()
    await service.deliver_before_shutdown()

    assert len(sender.calls) == 1
    assert len(sender.calls[0][2]) == 22


@pytest.mark.asyncio
async def test_generation_or_delivery_failure_is_best_effort_and_not_retried() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=28))
    repository = Repository(state)
    generator = Generator(failure=RuntimeError("private provider output"))
    sender = Sender(failure=RuntimeError("private Discord output"))
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()
    await service.prepare_once()
    await service.deliver_before_shutdown()

    assert len(generator.calls) == 1
    assert sender.calls == []
    assert "private provider output" not in repr(telemetry.events)
