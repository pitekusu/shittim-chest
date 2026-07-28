"""Deterministic process lifecycle tests without Discord or AWS access."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest

from shittim_chest.application import AcceptDebateRequest, DiscordBotSlot
from shittim_chest.runtime import (
    RuntimeAdmissionGateway,
    RuntimeLifecycle,
    RuntimeShutdownTimeout,
    UnixSignalHandlers,
)


@dataclass(slots=True)
class FakeDiscordGateway:
    ready: bool = False
    allowed: bool = True

    async def all_identities_ready(self) -> bool:
        return self.ready

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        del request
        return self.allowed


class RecordingAdmissionGateway(RuntimeAdmissionGateway):
    def __init__(self, delegate: FakeDiscordGateway, events: list[str]) -> None:
        super().__init__(delegate)
        self._events = events

    def close(self) -> None:
        self._events.append("admission_closed")
        super().close()


@dataclass(slots=True)
class FakeSupervisor:
    events: list[str]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    tokens: Mapping[DiscordBotSlot, str] | None = None
    failure: Exception | None = None

    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None:
        self.tokens = tokens
        self.events.append("supervisor_started")
        self.started.set()
        try:
            if self.failure is not None:
                raise self.failure
            await asyncio.Event().wait()
        finally:
            self.events.append("supervisor_stopped")
            self.stopped.set()


@dataclass(slots=True)
class FakeInteractions:
    events: list[str]
    schema_hash: str = "current-schema"
    begin_shutdown_calls: int = 0
    close_calls: int = 0
    sync_inputs: list[str | None] = field(default_factory=list)
    close_failure: Exception | None = None

    @property
    def command_schema_hash(self) -> str:
        return self.schema_hash

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool:
        self.events.append("command_schema_synced")
        self.sync_inputs.append(previous_schema_hash)
        return previous_schema_hash != self.schema_hash

    def begin_shutdown(self) -> None:
        self.events.append("interactions_shutdown")
        self.begin_shutdown_calls += 1

    async def close(self) -> None:
        self.events.append("interactions_closed")
        self.close_calls += 1
        if self.close_failure is not None:
            raise self.close_failure


@dataclass(slots=True)
class FakeIngressRuntime:
    events: list[str]
    active_tasks: int = 0
    recover_calls: int = 0
    begin_shutdown_calls: int = 0
    checkpoint_calls: int = 0
    close_calls: int = 0
    recover_failure: Exception | None = None
    cancellation_failure: Exception | None = None
    checkpoint_failure: Exception | None = None
    block_checkpoint: bool = False
    recover_release: asyncio.Event | None = None
    recover_results: list[int] = field(default_factory=list)
    debate_release: asyncio.Event | None = None
    debate_task: asyncio.Task[bool] | None = None

    @property
    def active_task_count(self) -> int:
        return self.active_tasks

    def begin_shutdown(self) -> None:
        self.events.append("ingress_runtime_shutdown")
        self.begin_shutdown_calls += 1

    async def recover_once(self) -> int:
        self.events.append("recovery_started")
        self.recover_calls += 1
        if self.recover_failure is not None:
            raise self.recover_failure
        try:
            if self.recover_release is not None:
                await self.recover_release.wait()
        except asyncio.CancelledError:
            if self.cancellation_failure is not None:
                raise self.cancellation_failure from None
            raise
        started = self.recover_results.pop(0) if self.recover_results else 0
        if started > 0:
            self.active_tasks += started
        if self.debate_release is not None and (
            self.debate_task is None or self.debate_task.done()
        ):
            self.debate_task = asyncio.create_task(
                self.debate_release.wait(),
                name="fake:long-running-debate",
            )
            self.active_tasks = 1
            started += 1
        self.events.append("recovery_registered")
        return started

    async def checkpoint_active(self) -> None:
        self.events.append("ingress_checkpointed")
        self.checkpoint_calls += 1
        if self.checkpoint_failure is not None:
            raise self.checkpoint_failure
        if self.block_checkpoint:
            await asyncio.Event().wait()
        if self.debate_task is not None and not self.debate_task.done():
            self.debate_task.cancel()
            await asyncio.gather(self.debate_task, return_exceptions=True)
        self.debate_task = None
        self.active_tasks = 0

    async def close(self) -> None:
        self.events.append("ingress_runtime_closed")
        self.close_calls += 1


@dataclass(slots=True)
class FakeDrainGate:
    events: list[str]
    supervisor_started: bool = False
    command_schema_checked: bool = False
    recovery_complete: bool = False
    shutting_down: bool = False

    def mark_supervisor_started(self) -> None:
        self.events.append("gate_supervisor_started")
        self.supervisor_started = True

    def mark_command_schema_checked(self) -> None:
        self.events.append("gate_schema_checked")
        self.command_schema_checked = True

    def begin_recovery(self) -> None:
        self.events.append("gate_recovery_closed")
        self.recovery_complete = False

    def mark_recovery_complete(self) -> None:
        self.events.append("gate_recovery_complete")
        self.recovery_complete = True

    def begin_shutdown(self) -> None:
        self.events.append("gate_shutdown")
        self.recovery_complete = False
        self.shutting_down = True


@dataclass(slots=True)
class FakeDrainer:
    events: list[str]
    run_calls: int = 0
    stop_calls: int = 0
    failure: Exception | None = None

    async def run(self, stop: asyncio.Event) -> None:
        self.events.append("drainer_started")
        self.run_calls += 1
        try:
            if self.failure is not None:
                raise self.failure
            await stop.wait()
        finally:
            self.events.append("drainer_stopped")
            self.stop_calls += 1


@dataclass(slots=True)
class FakeRuntimeInstance:
    events: list[str]
    started_calls: int = 0
    ready_inputs: list[bool] = field(default_factory=list)
    woken_results: list[bool] = field(default_factory=list)
    woken_checks: int = 0
    shutdown_calls: int = 0
    started_failure: Exception | None = None
    ready_failure: Exception | None = None
    shutdown_failure: Exception | None = None

    async def mark_started(self) -> None:
        self.events.append("runtime_started")
        self.started_calls += 1
        if self.started_failure is not None:
            raise self.started_failure

    async def mark_ready(self, *, active: bool) -> None:
        self.events.append(f"runtime_ready:{active}")
        self.ready_inputs.append(active)
        if self.ready_failure is not None:
            raise self.ready_failure

    async def claim_woken_start(self) -> bool:
        self.woken_checks += 1
        claimed = self.woken_results.pop(0) if self.woken_results else False
        if claimed:
            self.events.append("runtime_wake_claimed")
        return claimed

    async def mark_shutdown_complete(self) -> None:
        self.events.append("runtime_shutdown_complete")
        self.shutdown_calls += 1
        if self.shutdown_failure is not None:
            raise self.shutdown_failure


@dataclass(slots=True)
class FakeSignalHandlers:
    callback: Callable[[], None] | None = None
    install_calls: int = 0
    uninstall_calls: int = 0

    def install(self, callback: Callable[[], None]) -> None:
        self.install_calls += 1
        self.callback = callback

    def uninstall(self) -> None:
        self.uninstall_calls += 1
        self.callback = None


@dataclass(slots=True)
class FakeEventLoop:
    callbacks: dict[signal.Signals, Callable[[], None]] = field(default_factory=dict)
    removed: list[signal.Signals] = field(default_factory=list)

    def add_signal_handler(
        self,
        current_signal: signal.Signals,
        callback: Callable[[], None],
    ) -> None:
        self.callbacks[current_signal] = callback

    def remove_signal_handler(self, current_signal: signal.Signals) -> bool:
        self.removed.append(current_signal)
        return self.callbacks.pop(current_signal, None) is not None


@dataclass(frozen=True, slots=True)
class LifecycleFakes:
    runtime: RuntimeLifecycle
    admission: RecordingAdmissionGateway
    supervisor: FakeSupervisor
    interactions: FakeInteractions
    ingress_runtime: FakeIngressRuntime
    gate: FakeDrainGate
    drainer: FakeDrainer
    runtime_instance: FakeRuntimeInstance
    signals: FakeSignalHandlers
    events: list[str]


def tokens() -> dict[DiscordBotSlot, str]:
    return {slot: f"token-{slot.value}" for slot in DiscordBotSlot}


def request() -> AcceptDebateRequest:
    return AcceptDebateRequest(
        question="question",
        requester_id="101",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="102",
        channel_id="103",
        operation_id="104",
    )


async def wait_until(predicate: Callable[[], bool], *, deadline_seconds: float = 1.0) -> None:
    async with asyncio.timeout(deadline_seconds):
        while not predicate():  # noqa: ASYNC110 - deterministic polling of external fake state
            await asyncio.sleep(0.001)


def lifecycle(
    *,
    gateway: FakeDiscordGateway,
    supervisor: FakeSupervisor | None = None,
    interactions: FakeInteractions | None = None,
    ingress_runtime: FakeIngressRuntime | None = None,
    gate: FakeDrainGate | None = None,
    drainer: FakeDrainer | None = None,
    runtime_instance: FakeRuntimeInstance | None = None,
    signal_handlers: FakeSignalHandlers | None = None,
    disconnect_grace_seconds: float = 0.02,
    shutdown_timeout_seconds: float = 0.2,
) -> LifecycleFakes:
    events: list[str] = []
    current_supervisor = supervisor or FakeSupervisor(events)
    current_interactions = interactions or FakeInteractions(events)
    current_ingress_runtime = ingress_runtime or FakeIngressRuntime(events)
    current_gate = gate or FakeDrainGate(events)
    current_drainer = drainer or FakeDrainer(events)
    current_runtime_instance = runtime_instance or FakeRuntimeInstance(events)
    current_signals = signal_handlers or FakeSignalHandlers()
    current_supervisor.events = events
    current_interactions.events = events
    current_ingress_runtime.events = events
    current_gate.events = events
    current_drainer.events = events
    current_runtime_instance.events = events
    admission = RecordingAdmissionGateway(gateway, events)
    runtime = RuntimeLifecycle(
        admission=admission,
        supervisor=current_supervisor,
        interactions=current_interactions,
        ingress_runtime=current_ingress_runtime,
        drain_gate=current_gate,
        drainer=current_drainer,
        runtime_instance=current_runtime_instance,
        tokens=tokens(),
        previous_command_schema_hash="previous-schema",
        signal_handlers=current_signals,
        readiness_poll_seconds=0.005,
        disconnect_grace_seconds=disconnect_grace_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    return LifecycleFakes(
        runtime=runtime,
        admission=admission,
        supervisor=current_supervisor,
        interactions=current_interactions,
        ingress_runtime=current_ingress_runtime,
        gate=current_gate,
        drainer=current_drainer,
        runtime_instance=current_runtime_instance,
        signals=current_signals,
        events=events,
    )


def assert_order(events: list[str], *ordered: str) -> None:
    positions = [events.index(event) for event in ordered]
    assert positions == sorted(positions), events


@pytest.mark.asyncio
async def test_admission_is_fail_closed_and_preserves_request_policy() -> None:
    physical = FakeDiscordGateway(ready=True)
    admission = RuntimeAdmissionGateway(physical)

    assert not admission.is_accepting
    assert not await admission.all_identities_ready()
    assert await admission.request_is_allowed(request())
    assert await admission.open()
    assert await admission.all_identities_ready()

    physical.ready = False
    assert not await admission.all_identities_ready()
    admission.close()
    assert not admission.is_accepting


@pytest.mark.asyncio
async def test_unix_signal_handlers_own_sigint_and_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = FakeEventLoop()
    callbacks = 0

    def callback() -> None:
        nonlocal callbacks
        callbacks += 1

    monkeypatch.setattr(
        asyncio,
        "get_running_loop",
        lambda: cast(asyncio.AbstractEventLoop, loop),
    )
    handlers = UnixSignalHandlers()
    handlers.install(callback)

    assert set(loop.callbacks) == {signal.SIGINT, signal.SIGTERM}
    loop.callbacks[signal.SIGTERM]()
    assert callbacks == 1
    with pytest.raises(RuntimeError, match="already installed"):
        handlers.install(callback)

    handlers.uninstall()
    handlers.uninstall()
    assert loop.removed == [signal.SIGINT, signal.SIGTERM]


@pytest.mark.asyncio
async def test_startup_orders_recovery_before_runtime_ready_admission_and_drain() -> None:
    debate_release = asyncio.Event()
    ingress_runtime = FakeIngressRuntime([], debate_release=debate_release)
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await values.supervisor.started.wait()
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    assert values.interactions.sync_inputs == ["previous-schema"]
    assert values.supervisor.tokens == tokens()
    assert values.runtime_instance.ready_inputs == [True]
    assert values.ingress_runtime.debate_task is not None
    assert not values.ingress_runtime.debate_task.done()
    assert_order(
        values.events,
        "runtime_started",
        "supervisor_started",
        "gate_supervisor_started",
        "command_schema_synced",
        "gate_schema_checked",
        "recovery_started",
        "recovery_registered",
        "gate_recovery_complete",
        "runtime_ready:True",
        "drainer_started",
    )

    values.runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_recovery_registration_barrier_is_fail_closed() -> None:
    release = asyncio.Event()
    ingress_runtime = FakeIngressRuntime([], recover_release=release)
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: ingress_runtime.recover_calls == 1)

    assert not values.admission.is_accepting
    assert values.drainer.run_calls == 0
    assert values.runtime_instance.ready_inputs == []
    assert not values.gate.recovery_complete

    release.set()
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)
    assert values.signals.callback is not None
    values.signals.callback()
    values.runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_ready_poll_reclaims_a_debate_after_an_old_lease_expires() -> None:
    ingress_runtime = FakeIngressRuntime([], recover_results=[0, 1])
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    runtime_task = asyncio.create_task(values.runtime.run())

    await wait_until(
        lambda: (
            values.admission.is_accepting
            and values.drainer.run_calls == 1
            and values.runtime_instance.ready_inputs == [False, True]
        )
    )

    assert values.ingress_runtime.recover_calls >= 2
    assert values.gate.recovery_complete
    assert values.drainer.run_calls == 1
    first_drain = values.events.index("drainer_started")
    busy_transition = values.events.index("runtime_ready:True")
    assert first_drain < busy_transition

    values.runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_idle_wake_rebinds_then_recovers_before_reopening_drain() -> None:
    runtime_instance = FakeRuntimeInstance([], woken_results=[True])
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        runtime_instance=runtime_instance,
    )
    runtime_task = asyncio.create_task(values.runtime.run())

    await wait_until(
        lambda: (
            values.admission.is_accepting
            and values.drainer.run_calls == 2
            and values.runtime_instance.ready_inputs == [False, False]
        )
    )

    wake_start = values.events.index("runtime_wake_claimed")
    wake_events = values.events[wake_start:]
    assert_order(
        wake_events,
        "runtime_wake_claimed",
        "admission_closed",
        "gate_recovery_closed",
        "drainer_stopped",
        "recovery_started",
        "recovery_registered",
        "gate_recovery_complete",
        "runtime_ready:False",
        "drainer_started",
    )
    assert values.runtime_instance.woken_checks >= 1

    values.runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_disconnect_stops_claims_then_checkpoints_and_recovers_before_reopen() -> None:
    physical = FakeDiscordGateway(ready=True)
    values = lifecycle(gateway=physical)
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    physical.ready = False
    await wait_until(lambda: not values.admission.is_accepting and values.drainer.stop_calls == 1)
    disconnect_gate = len(values.events) - 1 - values.events[::-1].index("gate_recovery_closed")
    disconnect_admission = len(values.events) - 1 - values.events[::-1].index("admission_closed")
    stopped = len(values.events) - 1 - values.events[::-1].index("drainer_stopped")
    assert disconnect_admission < disconnect_gate < stopped
    assert values.ingress_runtime.checkpoint_calls == 0
    await wait_until(lambda: values.ingress_runtime.checkpoint_calls == 1)

    recover_calls_before_reconnect = values.ingress_runtime.recover_calls
    recovery_start = len(values.events)
    physical.ready = True
    await wait_until(
        lambda: (
            values.admission.is_accepting
            and values.ingress_runtime.recover_calls > recover_calls_before_reconnect
            and values.drainer.run_calls == 2
        )
    )
    reconnect_events = values.events[recovery_start:]
    assert_order(
        reconnect_events,
        "recovery_started",
        "recovery_registered",
        "gate_recovery_complete",
        "runtime_ready:False",
        "drainer_started",
    )
    assert values.interactions.sync_inputs == ["previous-schema"]

    values.runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_shutdown_closes_gates_before_drain_checkpoint_and_components() -> None:
    values = lifecycle(gateway=FakeDiscordGateway(ready=True))
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    start = len(values.events)
    values.runtime.request_shutdown()
    assert not values.admission.is_accepting
    assert values.gate.shutting_down
    assert values.ingress_runtime.begin_shutdown_calls == 1
    await runtime_task

    shutdown_events = values.events[start:]
    assert_order(
        shutdown_events,
        "admission_closed",
        "gate_shutdown",
        "ingress_runtime_shutdown",
        "interactions_shutdown",
        "drainer_stopped",
        "ingress_checkpointed",
        "ingress_runtime_closed",
        "interactions_closed",
        "supervisor_stopped",
        "runtime_shutdown_complete",
    )
    assert values.ingress_runtime.checkpoint_calls == 1
    assert values.ingress_runtime.close_calls == 1
    assert values.interactions.close_calls == 1
    assert values.runtime_instance.shutdown_calls == 1
    assert values.signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_repeated_shutdown_request_is_idempotent() -> None:
    values = lifecycle(gateway=FakeDiscordGateway(ready=True))
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    values.runtime.request_shutdown()
    values.runtime.request_shutdown()
    await runtime_task

    assert values.events.count("admission_closed") == 1
    assert values.events.count("gate_shutdown") == 1
    assert values.ingress_runtime.begin_shutdown_calls == 1
    assert values.interactions.begin_shutdown_calls == 1
    assert values.runtime_instance.shutdown_calls == 1


@pytest.mark.asyncio
async def test_unexpected_supervisor_failure_is_propagated_after_cleanup() -> None:
    supervisor = FakeSupervisor([], failure=RuntimeError("gateway failed"))
    values = lifecycle(gateway=FakeDiscordGateway(), supervisor=supervisor)
    with pytest.raises(RuntimeError, match="gateway failed"):
        await values.runtime.run()

    assert not values.admission.is_accepting
    assert values.interactions.begin_shutdown_calls == 1
    assert values.ingress_runtime.close_calls == 1
    assert values.interactions.close_calls == 1
    assert values.signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_runtime_start_failure_never_starts_clients_or_admission() -> None:
    runtime_instance = FakeRuntimeInstance([], started_failure=RuntimeError("start failed"))
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        runtime_instance=runtime_instance,
    )

    with pytest.raises(RuntimeError, match="start failed"):
        await values.runtime.run()

    assert not values.supervisor.started.is_set()
    assert not values.admission.is_accepting
    assert values.drainer.run_calls == 0
    assert values.ingress_runtime.begin_shutdown_calls == 1
    assert values.signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_recovery_failure_closes_admission_and_stops_the_runtime() -> None:
    ingress_runtime = FakeIngressRuntime([], recover_failure=RuntimeError("recovery failed"))
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    with pytest.raises(RuntimeError, match="recovery failed"):
        await values.runtime.run()

    assert ingress_runtime.recover_calls == 1
    assert not values.admission.is_accepting
    assert values.drainer.run_calls == 0
    assert values.supervisor.stopped.is_set()


@pytest.mark.asyncio
async def test_runtime_ready_failure_never_opens_admission_or_starts_drain() -> None:
    runtime_instance = FakeRuntimeInstance([], ready_failure=RuntimeError("ready failed"))
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        runtime_instance=runtime_instance,
    )
    with pytest.raises(RuntimeError, match="ready failed"):
        await values.runtime.run()

    assert not values.admission.is_accepting
    assert values.drainer.run_calls == 0
    assert values.gate.recovery_complete is False


@pytest.mark.asyncio
async def test_unexpected_drainer_failure_stops_the_runtime() -> None:
    drainer = FakeDrainer([], failure=RuntimeError("drain failed"))
    values = lifecycle(gateway=FakeDiscordGateway(ready=True), drainer=drainer)
    with pytest.raises(RuntimeError, match="drain failed"):
        await values.runtime.run()

    assert not values.admission.is_accepting
    assert values.ingress_runtime.checkpoint_calls == 1
    assert values.supervisor.stopped.is_set()


@pytest.mark.asyncio
async def test_shutdown_timeout_fails_explicitly_before_fargate_deadline() -> None:
    ingress_runtime = FakeIngressRuntime([], block_checkpoint=True)
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
        shutdown_timeout_seconds=0.01,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    values.runtime.request_shutdown()
    with pytest.raises(RuntimeShutdownTimeout, match=r"0\.01 seconds"):
        await runtime_task
    await values.supervisor.stopped.wait()

    assert not values.admission.is_accepting
    assert values.signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_checkpoint_failure_stops_clients_without_recording_normal_shutdown() -> None:
    ingress_runtime = FakeIngressRuntime(
        [],
        checkpoint_failure=RuntimeError("checkpoint failed"),
    )
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    values.runtime.request_shutdown()
    with pytest.raises(ExceptionGroup, match="runtime shutdown failed"):
        await runtime_task

    assert values.supervisor.stopped.is_set()
    assert values.ingress_runtime.close_calls == 1
    assert values.interactions.close_calls == 1
    assert values.runtime_instance.shutdown_calls == 0
    assert values.signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_runtime_state_shutdown_failure_is_reported_after_clients_close() -> None:
    runtime_instance = FakeRuntimeInstance(
        [],
        shutdown_failure=RuntimeError("runtime state update failed"),
    )
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        runtime_instance=runtime_instance,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: values.admission.is_accepting and values.drainer.run_calls == 1)

    values.runtime.request_shutdown()
    with pytest.raises(ExceptionGroup, match="runtime shutdown failed") as captured:
        await runtime_task

    assert any(
        isinstance(error, RuntimeError) and str(error) == "runtime state update failed"
        for error in captured.value.exceptions
    )
    assert values.supervisor.stopped.is_set()
    assert values.ingress_runtime.close_calls == 1
    assert values.interactions.close_calls == 1
    assert values.runtime_instance.shutdown_calls == 1


@pytest.mark.asyncio
async def test_recovery_cancellation_failure_is_not_swallowed() -> None:
    release = asyncio.Event()
    ingress_runtime = FakeIngressRuntime(
        [],
        recover_release=release,
        cancellation_failure=RuntimeError("recovery checkpoint failed"),
    )
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        ingress_runtime=ingress_runtime,
    )
    runtime_task = asyncio.create_task(values.runtime.run())
    await wait_until(lambda: ingress_runtime.recover_calls == 1)

    values.runtime.request_shutdown()
    with pytest.raises(ExceptionGroup, match="runtime shutdown failed"):
        await runtime_task

    assert values.supervisor.stopped.is_set()
    assert values.interactions.close_calls == 1
    assert values.signals.uninstall_calls == 1


def test_runtime_rejects_non_positive_timeouts() -> None:
    events: list[str] = []
    admission = RecordingAdmissionGateway(FakeDiscordGateway(), events)

    with pytest.raises(ValueError, match="positive"):
        RuntimeLifecycle(
            admission=admission,
            supervisor=FakeSupervisor(events),
            interactions=FakeInteractions(events),
            ingress_runtime=FakeIngressRuntime(events),
            drain_gate=FakeDrainGate(events),
            drainer=FakeDrainer(events),
            runtime_instance=FakeRuntimeInstance(events),
            tokens=tokens(),
            previous_command_schema_hash=None,
            signal_handlers=FakeSignalHandlers(),
            readiness_poll_seconds=0,
        )
