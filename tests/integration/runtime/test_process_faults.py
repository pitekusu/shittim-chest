from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix signal contract")
_PROJECT_ROOT = Path(__file__).parents[3]


def test_real_sigterm_runs_checkpoint_and_owned_cleanup(tmp_path: Path) -> None:
    journal = tmp_path / "sigterm.log"
    state = tmp_path / "sigterm.state"
    ready = tmp_path / "sigterm.ready"
    process = _start(journal, state, ready)
    try:
        _wait_for_path(ready)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
    finally:
        _ensure_stopped(process)

    events = journal.read_text(encoding="utf-8").splitlines()
    assert state.read_text(encoding="utf-8") == "checkpointed"
    assert "admission_closed" in events
    assert "checkpointed" in events
    assert "interactions_closed" in events
    assert "clients_stopped" in events


def test_real_sigkill_leaves_durable_state_for_replacement_process(tmp_path: Path) -> None:
    journal = tmp_path / "sigkill.log"
    state = tmp_path / "sigkill.state"
    first_ready = tmp_path / "first.ready"
    first = _start(journal, state, first_ready)
    try:
        _wait_for_path(first_ready)
        first.kill()
        assert first.wait(timeout=5) == -signal.SIGKILL
    finally:
        _ensure_stopped(first)

    assert state.read_text(encoding="utf-8") == "active"
    assert "checkpointed" not in journal.read_text(encoding="utf-8").splitlines()

    second_ready = tmp_path / "second.ready"
    second = _start(journal, state, second_ready)
    try:
        _wait_for_path(second_ready)
        second.terminate()
        assert second.wait(timeout=5) == 0
    finally:
        _ensure_stopped(second)

    events = journal.read_text(encoding="utf-8").splitlines()
    assert "recovered_after_forced_stop" in events
    assert state.read_text(encoding="utf-8") == "checkpointed"


def test_module_entrypoint_fails_closed_without_echoing_injected_values() -> None:
    private_marker = "private-startup-marker"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "OPENAI_API_KEY": private_marker,
    }

    result = subprocess.run(
        (sys.executable, "-m", "shittim_chest"),
        cwd=_PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        timeout=5,
    )

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert private_marker.encode() not in output
    assert b"startup_configuration_invalid" in output


def _start(journal: Path, state: Path, ready: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and module argv
        (
            sys.executable,
            "-m",
            "tests.fixtures.runtime_process",
            str(journal),
            str(state),
            str(ready),
        ),
        cwd=_PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            time.sleep(0.05)
            return
        time.sleep(0.01)
    raise AssertionError(f"subprocess did not become ready: {path.name}")


def _ensure_stopped(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
