"""Minimal container health-check entry point.

This module intentionally uses only the Python standard library and lives outside
``shittim_chest.runtime`` so ``python -m shittim_chest.healthcheck`` does not load
the runtime composition graph or third-party SDKs.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Final

HEARTBEAT_PATH: Final = Path("/tmp/shittim-chest/heartbeat")  # noqa: S108
DEFAULT_MAX_HEARTBEAT_AGE_SECONDS: Final = 20.0


class HealthStatus(IntEnum):
    """Stable, content-free process exit codes for the container health check."""

    HEALTHY = 0
    HEARTBEAT_MISSING = 1
    TIMESTAMP_INVALID = 2
    HEARTBEAT_STALE = 3
    CONFIGURATION_INVALID = 4


def heartbeat_status(
    *,
    path: Path = HEARTBEAT_PATH,
    max_age_seconds: float = DEFAULT_MAX_HEARTBEAT_AGE_SECONDS,
    now: Callable[[], float] = time.time,
) -> HealthStatus:
    """Classify the task-local heartbeat without importing application code."""

    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        return HealthStatus.CONFIGURATION_INVALID
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return HealthStatus.HEARTBEAT_MISSING
    age_seconds = now() - modified_at
    if not math.isfinite(age_seconds) or age_seconds < 0:
        return HealthStatus.TIMESTAMP_INVALID
    if age_seconds > max_age_seconds:
        return HealthStatus.HEARTBEAT_STALE
    return HealthStatus.HEALTHY


def heartbeat_age_seconds(
    *,
    path: Path = HEARTBEAT_PATH,
    now: Callable[[], float] = time.time,
) -> float | None:
    """Return a non-negative heartbeat age or ``None`` for an invalid sample."""

    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return None
    age_seconds = now() - modified_at
    if not math.isfinite(age_seconds) or age_seconds < 0:
        return None
    return age_seconds


def main() -> int:
    """Return a stable status code without writing health data to stdout/stderr."""

    return int(heartbeat_status())


if __name__ == "__main__":
    raise SystemExit(main())
