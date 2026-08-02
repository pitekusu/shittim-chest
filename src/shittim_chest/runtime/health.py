"""Container-local process and event-loop heartbeat health check."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from shittim_chest.healthcheck import (
    DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    HEARTBEAT_PATH,
    HealthStatus,
    heartbeat_status,
)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final = 5.0


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
        temporary = self._path.with_name(f".{self._path.name}.next")
        temporary.write_text("\n", encoding="ascii")
        temporary.chmod(0o600)
        temporary.replace(self._path)


def heartbeat_is_healthy(
    *,
    path: Path = HEARTBEAT_PATH,
    max_age_seconds: float = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    now: Callable[[], float] = time.time,
) -> bool:
    """Return true when the task-local event-loop heartbeat is fresh."""

    return (
        heartbeat_status(path=path, max_age_seconds=max_age_seconds, now=now)
        is HealthStatus.HEALTHY
    )
