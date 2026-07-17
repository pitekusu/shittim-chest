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


@dataclass(slots=True)
class FakeSupervisor:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    tokens: Mapping[DiscordBotSlot, str] | None = None
    failure: Exception | None = None

    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None:
        self.tokens = tokens
        self.started.set()
        try:
            if self.failure is not None:
                raise self.failure
            await asyncio.Event().wait()
        finally:
            self.stopped.set()


@dataclass(slots=True)
class FakeInteractions:
    schema_hash: str = "current-schema"
    begin_shutdown_calls: int = 0
    checkpoint_calls: int = 0
    close_calls: int = 0
    sync_inputs: list[str | None] = field(default_factory=list)
    block_checkpoint: bool = False
    checkpoint_failure: Exception | None = None

    @property
    def command_schema_hash(self) -> str:
        return self.schema_hash

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool:
        self.sync_inputs.append(previous_schema_hash)
        return previous_schema_hash != self.schema_hash

    def begin_shutdown(self) -> None:
        self.begin_shutdown_calls += 1

    async def checkpoint_active(self) -> None:
        self.checkpoint_calls += 1
        if self.checkpoint_failure is not None:
            raise self.checkpoint_failure
        if self.block_checkpoint:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_calls += 1


@dataclass(slots=True)
class FakeApplication:
    resume_calls: int = 0
    block_recovery: bool = False
    failure: Exception | None = None
    cancellation_failure: Exception | None = None

    async def resume_recoverable(self) -> None:
        self.resume_calls += 1
        if self.failure is not None:
            raise self.failure
        if self.block_recovery:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if self.cancellation_failure is not None:
                    raise self.cancellation_failure from None
                raise


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


def tokens() -> dict[DiscordBotSlot, str]:
    return {slot: f"token-{slot.value}" for slot in DiscordBotSlot}


def request() -> AcceptDebateRequest:
    return AcceptDebateRequest(
        question="question",
        requester_id="101",
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
    application: FakeApplication | None = None,
    signal_handlers: FakeSignalHandlers | None = None,
    disconnect_grace_seconds: float = 0.02,
    shutdown_timeout_seconds: float = 0.2,
) -> tuple[
    RuntimeLifecycle,
    RuntimeAdmissionGateway,
    FakeSupervisor,
    FakeInteractions,
    FakeApplication,
    FakeSignalHandlers,
]:
    current_supervisor = supervisor or FakeSupervisor()
    current_interactions = interactions or FakeInteractions()
    current_application = application or FakeApplication()
    current_signals = signal_handlers or FakeSignalHandlers()
    admission = RuntimeAdmissionGateway(gateway)
    runtime = RuntimeLifecycle(
        admission=admission,
        supervisor=current_supervisor,
        interactions=current_interactions,
        application=current_application,
        tokens=tokens(),
        previous_command_schema_hash="previous-schema",
        signal_handlers=current_signals,
        readiness_poll_seconds=0.005,
        disconnect_grace_seconds=disconnect_grace_seconds,
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )
    return (
        runtime,
        admission,
        current_supervisor,
        current_interactions,
        current_application,
        current_signals,
    )


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
async def test_startup_syncs_once_resumes_recoverable_and_shutdown_is_owned() -> None:
    values = lifecycle(gateway=FakeDiscordGateway(ready=True))
    runtime, admission, supervisor, interactions, application, signals = values

    runtime_task = asyncio.create_task(runtime.run())
    await supervisor.started.wait()
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 1)

    assert interactions.sync_inputs == ["previous-schema"]
    assert supervisor.tokens == tokens()
    assert signals.install_calls == 1

    assert signals.callback is not None
    signals.callback()
    runtime.request_shutdown()
    await runtime_task

    assert runtime.shutdown_requested
    assert not admission.is_accepting
    assert interactions.begin_shutdown_calls == 1
    assert interactions.checkpoint_calls == 1
    assert interactions.close_calls == 1
    assert signals.uninstall_calls == 1
    assert supervisor.stopped.is_set()


@pytest.mark.asyncio
async def test_disconnect_closes_admission_then_checkpoints_and_resumes_after_reconnect() -> None:
    physical = FakeDiscordGateway(ready=True)
    application = FakeApplication(block_recovery=True)
    values = lifecycle(gateway=physical, application=application)
    runtime, admission, _, interactions, _, _ = values
    runtime_task = asyncio.create_task(runtime.run())
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 1)

    physical.ready = False
    await wait_until(lambda: not admission.is_accepting)
    assert interactions.checkpoint_calls == 0
    await wait_until(lambda: interactions.checkpoint_calls == 1)

    physical.ready = True
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 2)
    assert interactions.sync_inputs == ["previous-schema"]

    runtime.request_shutdown()
    await runtime_task


@pytest.mark.asyncio
async def test_unexpected_supervisor_failure_is_propagated_after_cleanup() -> None:
    supervisor = FakeSupervisor(failure=RuntimeError("gateway failed"))
    values = lifecycle(gateway=FakeDiscordGateway(), supervisor=supervisor)
    runtime, admission, _, interactions, _, signals = values

    with pytest.raises(RuntimeError, match="gateway failed"):
        await runtime.run()

    assert not admission.is_accepting
    assert interactions.begin_shutdown_calls == 1
    assert interactions.close_calls == 1
    assert signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_recovery_failure_closes_admission_and_stops_the_runtime() -> None:
    application = FakeApplication(failure=RuntimeError("recovery failed"))
    values = lifecycle(gateway=FakeDiscordGateway(ready=True), application=application)
    runtime, admission, supervisor, interactions, _, signals = values

    with pytest.raises(RuntimeError, match="recovery failed"):
        await runtime.run()

    assert application.resume_calls == 1
    assert not admission.is_accepting
    assert supervisor.stopped.is_set()
    assert interactions.close_calls == 1
    assert signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_fails_explicitly_before_fargate_deadline() -> None:
    interactions = FakeInteractions(block_checkpoint=True)
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        interactions=interactions,
        shutdown_timeout_seconds=0.01,
    )
    runtime, admission, supervisor, _, application, signals = values
    runtime_task = asyncio.create_task(runtime.run())
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 1)

    runtime.request_shutdown()
    with pytest.raises(RuntimeShutdownTimeout, match=r"0\.01 seconds"):
        await runtime_task
    await supervisor.stopped.wait()

    assert not admission.is_accepting
    assert signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_checkpoint_failure_is_reported_after_clients_are_stopped() -> None:
    interactions = FakeInteractions(checkpoint_failure=RuntimeError("checkpoint failed"))
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        interactions=interactions,
    )
    runtime, admission, supervisor, _, application, signals = values
    runtime_task = asyncio.create_task(runtime.run())
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 1)

    runtime.request_shutdown()
    with pytest.raises(ExceptionGroup, match="runtime shutdown failed"):
        await runtime_task

    assert supervisor.stopped.is_set()
    assert not admission.is_accepting
    assert interactions.close_calls == 1
    assert signals.uninstall_calls == 1


@pytest.mark.asyncio
async def test_recovery_checkpoint_failure_is_not_swallowed() -> None:
    application = FakeApplication(
        block_recovery=True,
        cancellation_failure=RuntimeError("recovery checkpoint failed"),
    )
    values = lifecycle(
        gateway=FakeDiscordGateway(ready=True),
        application=application,
    )
    runtime, admission, supervisor, interactions, _, signals = values
    runtime_task = asyncio.create_task(runtime.run())
    await wait_until(lambda: admission.is_accepting and application.resume_calls == 1)

    runtime.request_shutdown()
    with pytest.raises(ExceptionGroup, match="runtime shutdown failed"):
        await runtime_task

    assert supervisor.stopped.is_set()
    assert interactions.close_calls == 1
    assert signals.uninstall_calls == 1


def test_runtime_rejects_non_positive_timeouts() -> None:
    physical = FakeDiscordGateway()
    admission = RuntimeAdmissionGateway(physical)

    with pytest.raises(ValueError, match="positive"):
        RuntimeLifecycle(
            admission=admission,
            supervisor=FakeSupervisor(),
            interactions=FakeInteractions(),
            application=FakeApplication(),
            tokens=tokens(),
            previous_command_schema_hash=None,
            signal_handlers=FakeSignalHandlers(),
            readiness_poll_seconds=0,
        )
