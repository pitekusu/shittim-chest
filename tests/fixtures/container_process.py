"""CI-only PID 1 target for container signal and recovery fault injection."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping
from pathlib import Path

from shittim_chest.application import AcceptDebateRequest, DiscordBotSlot
from shittim_chest.domain.debate_state import NORMAL_PHASE_FLOW
from shittim_chest.runtime import RuntimeAdmissionGateway, RuntimeLifecycle

PHASES = tuple(phase.value for phase in NORMAL_PHASE_FLOW[:-1])
FORCED_BOUNDARIES = (
    "transaction-before",
    "transaction-after",
    "discord-before",
    "discord-after",
)


class _Gateway:
    async def all_identities_ready(self) -> bool:
        return True

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        del request
        return True


class _Supervisor:
    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None:
        del tokens
        await asyncio.Event().wait()


class _Interactions:
    def __init__(self, state: Path) -> None:
        self._state = state

    @property
    def command_schema_hash(self) -> str:
        return "b" * 64

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool:
        del previous_schema_hash
        return True

    def begin_shutdown(self) -> None:
        return

    async def checkpoint_active(self) -> None:
        current = _read(self._state / "phase", "unknown")
        _write(self._state / "recovery", f"checkpointed:{current}")

    async def close(self) -> None:
        return


class _Recoverable:
    def __init__(self, state: Path, scenario: str) -> None:
        self._state = state
        self._scenario = scenario

    async def resume_recoverable(self) -> None:
        if self._scenario.startswith("phase:"):
            phase = self._scenario.removeprefix("phase:")
            if phase not in PHASES:
                raise ValueError("unsupported phase")
            _write(self._state / "phase", phase)
            _write(self._state / "ready", self._scenario)
            await asyncio.Event().wait()
            return

        if self._scenario == "recover":
            _finish_recovery(self._state)
            _write(self._state / "complete", "complete")
            await asyncio.Event().wait()
            return

        _prepare_forced_boundary(self._state, self._scenario)
        _write(self._state / "ready", self._scenario)
        await asyncio.Event().wait()


def _prepare_forced_boundary(state: Path, scenario: str) -> None:
    if scenario not in FORCED_BOUNDARIES:
        raise ValueError("unsupported forced-stop boundary")
    _write(state / "phase", "discussing")
    if scenario == "transaction-before":
        return
    _commit_once(state)
    if scenario == "transaction-after":
        return
    _write(state / "outbox", "prepared")
    if scenario == "discord-before":
        return
    _post_once(state)


def _finish_recovery(state: Path) -> None:
    _commit_once(state)
    _write(state / "outbox", "prepared")
    _post_once(state)
    _write(state / "outbox", "completed:stable-message-id")


def _commit_once(state: Path) -> None:
    marker = state / "transaction"
    if marker.exists():
        return
    _write(marker, "committed")
    _append(state / "transaction-events", "commit")


def _post_once(state: Path) -> None:
    history = state / "discord-history"
    if history.exists() and "stable-content-hash" in history.read_text(encoding="utf-8"):
        return
    _append(history, "stable-content-hash:stable-message-id")


def _read(path: Path, default: str) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else default


def _write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.next")
    temporary.write_text(f"{value}\n", encoding="utf-8")
    temporary.replace(path)


def _append(path: Path, value: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{value}\n")
        stream.flush()
        os.fsync(stream.fileno())


async def _run(state: Path, scenario: str) -> None:
    lifecycle = RuntimeLifecycle(
        admission=RuntimeAdmissionGateway(_Gateway()),
        supervisor=_Supervisor(),
        interactions=_Interactions(state),
        application=_Recoverable(state, scenario),
        tokens={slot: f"placeholder-{slot.value}" for slot in DiscordBotSlot},
        previous_command_schema_hash=None,
        readiness_poll_seconds=0.01,
        disconnect_grace_seconds=0.05,
        shutdown_timeout_seconds=2.0,
    )
    await lifecycle.run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("scenario")
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    asyncio.run(_run(args.state, args.scenario))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
