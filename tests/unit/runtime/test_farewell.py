"""Lifecycle-independent tests for the IDLE farewell coordinator."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.adapters.discord import FarewellDeliveryError
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus
from shittim_chest.domain import ParticipantSlot
from shittim_chest.runtime.farewell import IdleFarewellCoordinator

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
CONTENT = "また集まりましょう!\n参考リンク: https://example.test/source"


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
    after_generate: Callable[[], None] | None = None

    async def generate(self, *, participant: ParticipantSlot, time_context: object) -> str:
        del time_context
        self.calls.append(participant)
        if self.failure is not None:
            raise self.failure
        if self.after_generate is not None:
            self.after_generate()
        return CONTENT


@dataclass(slots=True)
class Sender:
    calls: list[tuple[ParticipantSlot, str, str]] = field(default_factory=list)
    reconciliation_calls: list[bool] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)
    after_send: Callable[[], None] | None = None

    async def send(
        self,
        *,
        participant: ParticipantSlot,
        content: str,
        nonce: str,
        reconcile: bool = False,
    ) -> None:
        self.calls.append((participant, content, nonce))
        self.reconciliation_calls.append(reconcile)
        if self.after_send is not None:
            self.after_send()
        if self.failures:
            raise self.failures.pop(0)


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
async def test_sends_once_at_twenty_five_idle_minutes_from_three_participants() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=24, seconds=59))
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

    clock.current = NOW + timedelta(minutes=25)
    await service.prepare_once()
    await service.prepare_once()

    assert len(generator.calls) == 1
    assert generator.calls[0] in tuple(ParticipantSlot)
    assert len(sender.calls) == 1
    assert len(sender.calls[0][2]) == 22


@pytest.mark.asyncio
async def test_generation_change_discards_result_before_delivery() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=25))
    repository = Repository(state)
    generator = Generator()
    sender = Sender()
    telemetry = Telemetry()
    generator.after_generate = lambda: setattr(
        repository,
        "state",
        state.request_wake(at=clock.current + timedelta(seconds=1)),
    )
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()

    assert sender.calls == []
    assert telemetry.events[-1] == (
        "farewell_generation_discarded",
        {"reason": "farewell_idle_period_changed", "delivery_attempt_count": 0},
    )


@pytest.mark.asyncio
async def test_transient_delivery_failure_retries_once_with_the_same_nonce() -> None:
    clock = Clock(NOW + timedelta(minutes=25))
    repository = Repository(idle_state())
    generator = Generator()
    sender = Sender(failures=[FarewellDeliveryError("farewell_discord_unavailable")])
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()

    assert len(sender.calls) == 2
    assert sender.calls[0][2] == sender.calls[1][2]
    assert sender.reconciliation_calls == [False, True]
    assert telemetry.events[-1] == (
        "farewell_delivery_completed",
        {"delivery_attempt_count": 2},
    )


@pytest.mark.asyncio
async def test_non_retryable_delivery_failure_is_attempted_once() -> None:
    clock = Clock(NOW + timedelta(minutes=25))
    repository = Repository(idle_state())
    generator = Generator()
    sender = Sender(failures=[FarewellDeliveryError("farewell_permission_denied")])
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()

    assert len(sender.calls) == 1
    assert telemetry.events[-1] == (
        "farewell_delivery_omitted",
        {"reason": "farewell_permission_denied", "delivery_attempt_count": 1},
    )


@pytest.mark.asyncio
async def test_idle_change_prevents_a_transient_delivery_retry() -> None:
    state = idle_state()
    clock = Clock(NOW + timedelta(minutes=25))
    repository = Repository(state)
    generator = Generator()
    sender = Sender(
        failures=[FarewellDeliveryError("farewell_delivery_timeout")],
        after_send=lambda: setattr(
            repository,
            "state",
            state.request_wake(at=clock.current + timedelta(seconds=1)),
        ),
    )
    telemetry = Telemetry()
    service = coordinator(
        clock=clock,
        repository=repository,
        generator=generator,
        sender=sender,
        telemetry=telemetry,
    )

    await service.prepare_once()

    assert len(sender.calls) == 1
    assert telemetry.events[-1] == (
        "farewell_generation_discarded",
        {"reason": "farewell_idle_period_changed", "delivery_attempt_count": 1},
    )


@pytest.mark.asyncio
async def test_generation_failure_is_best_effort_and_does_not_repeat() -> None:
    clock = Clock(NOW + timedelta(minutes=25))
    repository = Repository(idle_state())
    generator = Generator(failure=RuntimeError("private provider output"))
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
    await service.prepare_once()

    assert len(generator.calls) == 1
    assert sender.calls == []
    assert "private provider output" not in repr(telemetry.events)


@pytest.mark.asyncio
async def test_stopping_state_never_generates_or_sends() -> None:
    state = idle_state()
    deadline = state.stop_eligible_at
    assert deadline is not None
    repository = Repository(state.begin_idle_stop(at=deadline))
    clock = Clock(deadline)
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
    assert sender.calls == []
