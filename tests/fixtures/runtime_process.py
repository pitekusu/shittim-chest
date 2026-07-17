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

    @property
    def command_schema_hash(self) -> str:
        return "a" * 64

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool:
        del previous_schema_hash
        _append(self._journal, "command_synced")
        return True

    def begin_shutdown(self) -> None:
        _append(self._journal, "admission_closed")

    async def checkpoint_active(self) -> None:
        self._state.write_text("checkpointed", encoding="utf-8")
        _append(self._journal, "checkpointed")

    async def close(self) -> None:
        _append(self._journal, "interactions_closed")


class _Recoverable:
    def __init__(self, journal: Path, state: Path, ready: Path) -> None:
        self._journal = journal
        self._state = state
        self._ready = ready

    async def resume_recoverable(self) -> None:
        previous = self._state.read_text(encoding="utf-8") if self._state.exists() else ""
        _append(self._journal, "recovery_started")
        if previous == "active":
            _append(self._journal, "recovered_after_forced_stop")
        self._state.write_text("active", encoding="utf-8")
        self._ready.write_text(str(os.getpid()), encoding="utf-8")


async def _run(journal: Path, state: Path, ready: Path) -> None:
    interactions = _Interactions(journal, state)
    lifecycle = RuntimeLifecycle(
        admission=RuntimeAdmissionGateway(_Gateway()),
        supervisor=_Supervisor(journal),
        interactions=interactions,
        application=_Recoverable(journal, state, ready),
        tokens={slot: f"placeholder-{slot.value}" for slot in DiscordBotSlot},
        previous_command_schema_hash=None,
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
