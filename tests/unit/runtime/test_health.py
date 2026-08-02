from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shittim_chest.healthcheck import HealthStatus, heartbeat_age_seconds, heartbeat_status
from shittim_chest.runtime.health import (
    EventLoopHeartbeat,
    heartbeat_is_healthy,
)


@pytest.mark.asyncio
async def test_event_loop_heartbeat_is_fresh(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "heartbeat"

    async with EventLoopHeartbeat(path=path, interval_seconds=0.01):
        await asyncio.sleep(0.03)

        assert path.read_text(encoding="ascii") == "\n"
        assert heartbeat_is_healthy(
            path=path,
            max_age_seconds=1,
        )
        assert heartbeat_status(path=path, max_age_seconds=1) is HealthStatus.HEALTHY

    assert not path.exists()


def test_health_rejects_stale_and_future_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.write_text("42\n", encoding="ascii")
    modified_at = path.stat().st_mtime

    assert not heartbeat_is_healthy(
        path=path,
        max_age_seconds=20,
        now=lambda: modified_at + 21,
    )
    assert not heartbeat_is_healthy(
        path=path,
        max_age_seconds=20,
        now=lambda: modified_at - 1,
    )


def test_health_rejects_missing_file_and_invalid_age(tmp_path: Path) -> None:
    path = tmp_path / "missing"

    assert not heartbeat_is_healthy(path=path)
    assert not heartbeat_is_healthy(path=path, max_age_seconds=0)


def test_heartbeat_age_is_content_free_and_rejects_invalid_samples(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.write_text("42\n", encoding="ascii")
    modified_at = path.stat().st_mtime

    assert heartbeat_age_seconds(path=path, now=lambda: modified_at + 2.5) == 2.5
    assert heartbeat_age_seconds(path=path, now=lambda: modified_at - 1) is None
    assert heartbeat_age_seconds(path=tmp_path / "missing") is None
