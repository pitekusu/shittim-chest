from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from shittim_chest.runtime.health import EventLoopHeartbeat, heartbeat_is_healthy, main


@pytest.mark.asyncio
async def test_event_loop_heartbeat_is_fresh_and_owned_by_current_process(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime" / "heartbeat"

    async with EventLoopHeartbeat(path=path, interval_seconds=0.01):
        await asyncio.sleep(0.03)

        assert path.read_text(encoding="ascii") == f"{os.getpid()}\n"
        assert heartbeat_is_healthy(
            path=path,
            max_age_seconds=1,
            process_probe=lambda pid: pid == os.getpid(),
        )

    assert not path.exists()


@pytest.mark.parametrize("contents", ["", "not-a-pid", "0", "-1"])
def test_health_rejects_invalid_pid_file(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "heartbeat"
    path.write_text(contents, encoding="ascii")

    assert not heartbeat_is_healthy(path=path, process_probe=lambda _: True)


def test_health_rejects_stale_future_and_missing_process(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.write_text("42\n", encoding="ascii")
    modified_at = path.stat().st_mtime

    assert not heartbeat_is_healthy(
        path=path,
        max_age_seconds=20,
        now=lambda: modified_at + 21,
        process_probe=lambda _: True,
    )
    assert not heartbeat_is_healthy(
        path=path,
        max_age_seconds=20,
        now=lambda: modified_at - 1,
        process_probe=lambda _: True,
    )
    assert not heartbeat_is_healthy(
        path=path,
        max_age_seconds=20,
        now=lambda: modified_at,
        process_probe=lambda _: False,
    )


def test_health_rejects_missing_file_and_invalid_age(tmp_path: Path) -> None:
    path = tmp_path / "missing"

    assert not heartbeat_is_healthy(path=path)
    assert not heartbeat_is_healthy(path=path, max_age_seconds=0)


def test_health_command_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shittim_chest.runtime.health.heartbeat_is_healthy", lambda: True)

    assert main() == 0

    monkeypatch.setattr("shittim_chest.runtime.health.heartbeat_is_healthy", lambda: False)
    assert main() == 1
