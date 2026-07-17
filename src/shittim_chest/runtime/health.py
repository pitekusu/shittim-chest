"""Container-local process and event-loop heartbeat health check."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

# The production container has one numeric non-root user and an isolated /tmp volume.
HEARTBEAT_PATH: Final = Path("/tmp/shittim-chest/heartbeat")  # noqa: S108
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 5.0
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS: Final = 20.0

ProcessProbe = Callable[[int], bool]


class EventLoopHeartbeat:
    """Own a file heartbeat that advances only while the event loop is responsive."""

    def __init__(
        self,
        *,
        path: Path = HEARTBEAT_PATH,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._path = path
        self._interval_seconds = interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> EventLoopHeartbeat:
        if self._task is not None:
            raise RuntimeError("heartbeat is already running")
        self._write()
        self._task = asyncio.create_task(self._pulse(), name="runtime:event-loop-heartbeat")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task
        self._path.unlink(missing_ok=True)

    async def _pulse(self) -> None:
        while not self._stop.is_set():
            try:
                async with asyncio.timeout(self._interval_seconds):
                    await self._stop.wait()
            except TimeoutError:
                self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.next")
        temporary.write_text(f"{os.getpid()}\n", encoding="ascii")
        temporary.chmod(0o600)
        temporary.replace(self._path)


def heartbeat_is_healthy(
    *,
    path: Path = HEARTBEAT_PATH,
    max_age_seconds: float = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    now: Callable[[], float] = time.time,
    process_probe: ProcessProbe | None = None,
) -> bool:
    """Return true only for a live process with a fresh heartbeat."""

    if max_age_seconds <= 0:
        return False
    try:
        raw_pid = path.read_text(encoding="ascii").strip()
        pid = int(raw_pid)
        modified_at = path.stat().st_mtime
    except OSError, UnicodeError, ValueError:
        return False
    if pid <= 0:
        return False
    age_seconds = now() - modified_at
    if not 0 <= age_seconds <= max_age_seconds:
        return False
    return (process_probe or _process_exists)(pid)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError, ValueError:
        return False
    return True


def main() -> int:
    """Provide a content-free command for Docker and ECS health checks."""

    return 0 if heartbeat_is_healthy() else 1


if __name__ == "__main__":
    raise SystemExit(main())
