"""Subprocess target used to inject real Unix termination signals."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping
from pathlib import Path

from shittim_chest.application import AcceptDebateRequest, DiscordBotSlot
from shittim_chest.runtime import RuntimeAdmissionGateway, RuntimeLifecycle


class _Gateway:
    async def all_identities_ready(self) -> bool:
        return True

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        del request
        return True


class _Supervisor:
    def __init__(self, journal: Path) -> None:
        self._journal = journal

    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None:
        del tokens
        _append(self._journal, "clients_started")
        try:
            await asyncio.Event().wait()
        finally:
            _append(self._journal, "clients_stopped")

    async def close(self) -> None:
        _append(self._journal, "clients_closed")


class _Interactions:
    def __init__(self, journal: Path, state: Path) -> None:
        self._journal = journal
        self._state = state

    def begin_shutdown(self) -> None:
        _append(self._journal, "admission_closed")

    async def close(self) -> None:
        _append(self._journal, "interactions_closed")


class _IngressRuntime:
    def __init__(self, journal: Path, state: Path, ready: Path) -> None:
        self._journal = journal
        self._state = state
        self._ready = ready
        self._task: asyncio.Task[None] | None = None

    @property
    def active_task_count(self) -> int:
        return int(self._task is not None and not self._task.done())

    def begin_shutdown(self) -> None:
        return

    async def recover_once(self) -> int:
        if self._task is not None and not self._task.done():
            return 0
        previous = self._state.read_text(encoding="utf-8") if self._state.exists() else ""
        _append(self._journal, "recovery_started")
        if previous == "active":
            _append(self._journal, "recovered_after_forced_stop")
        self._state.write_text("active", encoding="utf-8")
        self._ready.write_text(str(os.getpid()), encoding="utf-8")
        self._task = asyncio.create_task(self._run_debate(), name="fixture:debate")
        return 1

    async def checkpoint_active(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._state.write_text("checkpointed", encoding="utf-8")
        _append(self._journal, "checkpointed")

    async def close(self) -> None:
        return

    async def _run_debate(self) -> None:
        await asyncio.Event().wait()


class _DrainGate:
    def mark_supervisor_started(self) -> None:
        return

    def mark_local_command_schema_checked(self) -> None:
        return

    def begin_recovery(self) -> None:
        return

    def mark_recovery_complete(self) -> None:
        return

    def begin_shutdown(self) -> None:
        return


class _Drainer:
    async def run(self, stop: asyncio.Event) -> None:
        await stop.wait()


class _RuntimeInstance:
    def __init__(self, journal: Path) -> None:
        self._journal = journal

    async def mark_started(self) -> None:
        return

    async def claim_woken_start(self) -> bool:
        return False

    async def mark_ready(self, *, active: bool) -> None:
        del active

    async def mark_shutdown_complete(self) -> None:
        _append(self._journal, "runtime_state_updated")


async def _run(journal: Path, state: Path, ready: Path) -> None:
    interactions = _Interactions(journal, state)
    ingress_runtime = _IngressRuntime(journal, state, ready)
    lifecycle = RuntimeLifecycle(
        admission=RuntimeAdmissionGateway(_Gateway()),
        supervisor=_Supervisor(journal),
        interactions=interactions,
        ingress_runtime=ingress_runtime,
        drain_gate=_DrainGate(),
        drainer=_Drainer(),
        runtime_instance=_RuntimeInstance(journal),
        tokens={slot: f"placeholder-{slot.value}" for slot in DiscordBotSlot},
        readiness_poll_seconds=0.01,
        disconnect_grace_seconds=0.05,
        shutdown_timeout_seconds=2.0,
    )
    await lifecycle.run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("ready", type=Path)
    args = parser.parse_args()
    asyncio.run(_run(args.journal, args.state, args.ready))
    return 0


def _append(path: Path, event: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{event}\n")
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    raise SystemExit(main())
