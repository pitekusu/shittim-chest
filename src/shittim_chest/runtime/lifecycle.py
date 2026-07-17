"""Owned startup, readiness, recovery, and graceful process shutdown."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Mapping
from typing import Protocol

from shittim_chest.application import AcceptDebateRequest, DiscordBotSlot
from shittim_chest.application.ports import DiscordGateway

DEFAULT_DISCONNECT_GRACE_SECONDS = 60.0
DEFAULT_READINESS_POLL_SECONDS = 1.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 90.0


class RuntimeShutdownTimeout(RuntimeError):
    """Raised when graceful cleanup exceeds its internal safety deadline."""


class _DiscordClientSupervisor(Protocol):
    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None: ...


class _DiscordInteractionLifecycle(Protocol):
    @property
    def command_schema_hash(self) -> str: ...

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool: ...

    def begin_shutdown(self) -> None: ...

    async def checkpoint_active(self) -> None: ...

    async def close(self) -> None: ...


class _RecoverableDebates(Protocol):
    async def resume_recoverable(self) -> None: ...


class _SignalHandlers(Protocol):
    def install(self, callback: Callable[[], None]) -> None: ...

    def uninstall(self) -> None: ...


class RuntimeAdmissionGateway:
    """Add a process-owned fail-closed admission switch to Discord readiness."""

    def __init__(self, delegate: DiscordGateway) -> None:
        self._delegate = delegate
        self._accepting = False

    @property
    def is_accepting(self) -> bool:
        """Expose the process gate for health reporting and deterministic tests."""

        return self._accepting

    async def open(self) -> bool:
        """Open admission only if every physical Discord identity is ready."""

        self._accepting = await self._delegate.all_identities_ready()
        return self._accepting

    def close(self) -> None:
        """Synchronously reject subsequent acceptance checks."""

        self._accepting = False

    async def physical_identities_ready(self) -> bool:
        """Read physical readiness without bypassing it for acceptance."""

        return await self._delegate.all_identities_ready()

    async def all_identities_ready(self) -> bool:
        """Return true only while both the process gate and all clients are ready."""

        return self._accepting and await self._delegate.all_identities_ready()

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        """Preserve the delegate's fail-closed Guild/channel boundary."""

        return await self._delegate.request_is_allowed(request)


class UnixSignalHandlers:
    """Register event-loop-safe SIGINT and SIGTERM callbacks on Unix."""

    _SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._installed: tuple[signal.Signals, ...] = ()

    def install(self, callback: Callable[[], None]) -> None:
        """Install handlers from the main-thread event loop."""

        if self._loop is not None:
            raise RuntimeError("signal handlers are already installed")
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        try:
            for current_signal in self._SIGNALS:
                loop.add_signal_handler(current_signal, callback)
                installed.append(current_signal)
        except BaseException:
            for current_signal in installed:
                loop.remove_signal_handler(current_signal)
            raise
        self._loop = loop
        self._installed = tuple(installed)

    def uninstall(self) -> None:
        """Remove only handlers installed by this instance."""

        if self._loop is None:
            return
        for current_signal in self._installed:
            self._loop.remove_signal_handler(current_signal)
        self._loop = None
        self._installed = ()


class RuntimeLifecycle:
    """Own Discord clients, recovery tasks, readiness, and bounded shutdown."""

    def __init__(
        self,
        *,
        admission: RuntimeAdmissionGateway,
        supervisor: _DiscordClientSupervisor,
        interactions: _DiscordInteractionLifecycle,
        application: _RecoverableDebates,
        tokens: Mapping[DiscordBotSlot, str],
        previous_command_schema_hash: str | None,
        signal_handlers: _SignalHandlers | None = None,
        readiness_poll_seconds: float = DEFAULT_READINESS_POLL_SECONDS,
        disconnect_grace_seconds: float = DEFAULT_DISCONNECT_GRACE_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if (
            readiness_poll_seconds <= 0
            or disconnect_grace_seconds <= 0
            or shutdown_timeout_seconds <= 0
        ):
            raise ValueError("runtime timeouts must be positive")
        self._admission = admission
        self._supervisor = supervisor
        self._interactions = interactions
        self._application = application
        self._tokens = dict(tokens)
        self._previous_command_schema_hash = previous_command_schema_hash
        self._signal_handlers = signal_handlers or UnixSignalHandlers()
        self._readiness_poll_seconds = readiness_poll_seconds
        self._disconnect_grace_seconds = disconnect_grace_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._shutdown_requested = asyncio.Event()
        self._recovery_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def shutdown_requested(self) -> bool:
        """Return whether a signal or runtime failure initiated shutdown."""

        return self._shutdown_requested.is_set()

    def request_shutdown(self) -> None:
        """Idempotently close admission and request asynchronous cleanup."""

        if self._shutdown_requested.is_set():
            return
        self._admission.close()
        self._interactions.begin_shutdown()
        self._shutdown_requested.set()

    async def run(self) -> None:
        """Run until a signal or owned background task requests process exit."""

        if self._running:
            raise RuntimeError("runtime lifecycle may only be run once")
        self._running = True
        self._signal_handlers.install(self.request_shutdown)
        supervisor_task = asyncio.create_task(
            self._supervisor.run(self._tokens),
            name="runtime:discord-clients",
        )
        readiness_task = asyncio.create_task(
            self._monitor_readiness(),
            name="runtime:discord-readiness",
        )
        shutdown_waiter = asyncio.create_task(
            self._shutdown_requested.wait(),
            name="runtime:shutdown-waiter",
        )
        try:
            done, _ = await asyncio.wait(
                (supervisor_task, readiness_task, shutdown_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._shutdown_requested.is_set():
                return
            if supervisor_task in done:
                await supervisor_task
                raise RuntimeError("Discord client supervisor stopped unexpectedly")
            if readiness_task in done:
                await readiness_task
                raise RuntimeError("Discord readiness monitor stopped unexpectedly")
        finally:
            self.request_shutdown()
            try:
                await self._graceful_shutdown(
                    supervisor_task=supervisor_task,
                    readiness_task=readiness_task,
                    shutdown_waiter=shutdown_waiter,
                )
            finally:
                self._signal_handlers.uninstall()

    async def _monitor_readiness(self) -> None:
        loop = asyncio.get_running_loop()
        ever_ready = False
        command_synced = False
        outage_started_at: float | None = None
        outage_checkpointed = False

        while not self._shutdown_requested.is_set():
            self._raise_recovery_failure()
            physically_ready = await self._admission.physical_identities_ready()
            if physically_ready:
                if not command_synced:
                    await self._interactions.sync_command_if_changed(
                        previous_schema_hash=self._previous_command_schema_hash
                    )
                    command_synced = True
                if not ever_ready or outage_checkpointed:
                    self._start_recovery()
                await self._admission.open()
                ever_ready = True
                outage_started_at = None
                outage_checkpointed = False
            else:
                self._admission.close()
                if ever_ready and outage_started_at is None:
                    outage_started_at = loop.time()
                elif (
                    ever_ready
                    and outage_started_at is not None
                    and not outage_checkpointed
                    and loop.time() - outage_started_at >= self._disconnect_grace_seconds
                ):
                    await self._checkpoint_for_outage()
                    outage_checkpointed = True
            await self._wait_for_next_readiness_check()

    async def _wait_for_next_readiness_check(self) -> None:
        try:
            async with asyncio.timeout(self._readiness_poll_seconds):
                await self._shutdown_requested.wait()
        except TimeoutError:
            return

    def _start_recovery(self) -> None:
        self._raise_recovery_failure()
        if self._recovery_task is None:
            self._recovery_task = asyncio.create_task(
                self._application.resume_recoverable(),
                name="runtime:resume-recoverable",
            )
            self._recovery_task.add_done_callback(self._recovery_done)

    def _recovery_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self._admission.close()

    def _raise_recovery_failure(self) -> None:
        task = self._recovery_task
        if task is None or not task.done():
            return
        self._recovery_task = None
        task.result()

    async def _checkpoint_for_outage(self) -> None:
        recovery_task = self._recovery_task
        self._recovery_task = None
        async with asyncio.TaskGroup() as group:
            group.create_task(
                self._interactions.checkpoint_active(),
                name="runtime:checkpoint-interactions",
            )
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
                group.create_task(
                    _await_cancelled(recovery_task),
                    name="runtime:checkpoint-recovery",
                )

    async def _graceful_shutdown(
        self,
        *,
        supervisor_task: asyncio.Task[None],
        readiness_task: asyncio.Task[None],
        shutdown_waiter: asyncio.Task[bool],
    ) -> None:
        try:
            async with asyncio.timeout(self._shutdown_timeout_seconds):
                errors: list[Exception] = []
                if not readiness_task.done():
                    readiness_task.cancel()
                await asyncio.gather(readiness_task, return_exceptions=True)
                try:
                    await self._checkpoint_for_outage()
                except Exception as error:
                    errors.append(error)
                try:
                    await self._interactions.close()
                except Exception as error:
                    errors.append(error)
                supervisor_was_running = not supervisor_task.done()
                if supervisor_was_running:
                    supervisor_task.cancel()
                supervisor_result = (await asyncio.gather(supervisor_task, return_exceptions=True))[
                    0
                ]
                if supervisor_was_running and isinstance(supervisor_result, Exception):
                    errors.append(supervisor_result)
                if not shutdown_waiter.done():
                    shutdown_waiter.cancel()
                await asyncio.gather(shutdown_waiter, return_exceptions=True)
                if errors:
                    raise ExceptionGroup("runtime shutdown failed", errors)
        except TimeoutError as error:
            for task in (readiness_task, self._recovery_task, supervisor_task, shutdown_waiter):
                if task is not None and not task.done():
                    task.cancel()
            raise RuntimeShutdownTimeout(
                f"runtime shutdown exceeded {self._shutdown_timeout_seconds:g} seconds"
            ) from error


async def _await_cancelled(task: asyncio.Task[None]) -> None:
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
        raise RuntimeError("a recovery task failed during checkpoint") from result
