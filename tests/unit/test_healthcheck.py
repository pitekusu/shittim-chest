"""Tests for the isolated container health-check process."""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

from shittim_chest.healthcheck import HealthStatus, heartbeat_status, main


def test_healthcheck_accepts_a_fresh_task_local_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    path.touch()
    modified_at = path.stat().st_mtime

    assert heartbeat_status(path=path, now=lambda: modified_at + 20) is HealthStatus.HEALTHY


def test_healthcheck_returns_stable_failure_categories(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"

    assert heartbeat_status(path=path) is HealthStatus.HEARTBEAT_MISSING

    path.touch()
    modified_at = path.stat().st_mtime
    assert (
        heartbeat_status(path=path, now=lambda: modified_at - 1) is HealthStatus.TIMESTAMP_INVALID
    )
    assert heartbeat_status(path=path, now=lambda: math.nan) is HealthStatus.TIMESTAMP_INVALID
    assert (
        heartbeat_status(path=path, now=lambda: modified_at + 20.001)
        is HealthStatus.HEARTBEAT_STALE
    )
    assert heartbeat_status(path=path, max_age_seconds=0) is HealthStatus.CONFIGURATION_INVALID


@pytest.mark.parametrize("status", list(HealthStatus))
def test_healthcheck_main_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: HealthStatus,
) -> None:
    monkeypatch.setattr("shittim_chest.healthcheck.heartbeat_status", lambda: status)

    assert main() == int(status)
    assert capsys.readouterr() == ("", "")


def test_healthcheck_import_isolated_from_runtime_and_third_party_sdks() -> None:
    command = (
        "import sys; import shittim_chest.healthcheck; "
        "assert 'shittim_chest.runtime' not in sys.modules; "
        "assert 'openai' not in sys.modules; "
        "assert 'discord' not in sys.modules"
    )

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
