#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate a native production image and inject real container stop signals."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

IMAGE_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
EXPECTED_USER: Final = "10001:10001"
MAXIMUM_STOP_SECONDS: Final = 120.0
NON_TERMINAL_PHASES: Final = (
    "accepted",
    "preparing_evidence",
    "collecting_initial_opinions",
    "discussing",
    "collecting_final_proposals",
    "selecting_winner",
    "generating_decision",
)
FORCED_BOUNDARIES: Final = (
    "transaction-before",
    "transaction-after",
    "discord-before",
    "discord-after",
)


class ContainerGateError(RuntimeError):
    """Raised when the native image or fault-recovery contract is violated."""


def _run(
    arguments: Sequence[str],
    *,
    capture_output: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed docker executable and validated arguments
            tuple(arguments),
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=150,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ContainerGateError(f"container command failed: {arguments[1]}") from error


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(("docker", *arguments), check=check)


def _validate_image_name(image: str) -> None:
    if IMAGE_PATTERN.fullmatch(image) is None:
        raise ContainerGateError("invalid local image reference")


def validate_image_configuration(document: object, expected_architecture: str) -> None:
    """Validate immutable Docker image configuration relevant to Fargate."""

    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise ContainerGateError("docker inspect must return one image object")
    image = document[0]
    config = image.get("Config")
    if not isinstance(config, dict):
        raise ContainerGateError("image Config is missing")
    if image.get("Architecture") != expected_architecture:
        raise ContainerGateError("image architecture does not match the native runner")
    if config.get("User") != EXPECTED_USER:
        raise ContainerGateError("image must use numeric UID/GID 10001")
    if config.get("Entrypoint") != ["python", "-m", "shittim_chest"]:
        raise ContainerGateError("image entrypoint is not the production module")
    if config.get("StopSignal") != "SIGTERM":
        raise ContainerGateError("image stop signal must be SIGTERM")
    # Docker Engine nests this under Config; Podman exposes the same Docker metadata
    # at the image object's top level.
    health = config.get("Healthcheck", image.get("Healthcheck"))
    if not isinstance(health, dict):
        raise ContainerGateError("image health check is missing")
    if health.get("Test") != ["CMD", "python", "-m", "shittim_chest.runtime.health"]:
        raise ContainerGateError("image health command is unexpected")
    expected_health = {
        "Interval": 10_000_000_000,
        "Timeout": 3_000_000_000,
        "StartPeriod": 30_000_000_000,
        "Retries": 3,
    }
    for key, expected in expected_health.items():
        if health.get(key) != expected:
            raise ContainerGateError(f"image health {key} is unexpected")


def _validate_runtime_security(image: str) -> None:
    script = """
import asyncio
import os
import pathlib
import shutil
from shittim_chest.runtime.health import EventLoopHeartbeat, heartbeat_is_healthy

async def verify():
    assert os.getuid() == 10001 and os.getgid() == 10001
    assert shutil.which("uv") is None
    assert not pathlib.Path("/app/src").exists()
    assert not pathlib.Path("/app/tests").exists()
    pathlib.Path("/tmp/native-arm64-probe").write_text("ok", encoding="ascii")
    try:
        pathlib.Path("/app/read-only-probe").write_text("blocked", encoding="ascii")
    except OSError:
        pass
    else:
        raise AssertionError("root filesystem accepted a write")
    async with EventLoopHeartbeat(interval_seconds=0.01):
        await asyncio.sleep(0.03)
        assert heartbeat_is_healthy(max_age_seconds=1)

asyncio.run(verify())
"""
    _docker(
        "run",
        "--rm",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",  # noqa: S108
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--entrypoint",
        "python",
        image,
        "-c",
        script,
    )


def _container_name(suffix: str) -> str:
    safe = re.sub(r"[^a-z0-9-]", "-", suffix.lower())
    return f"shittim-step08b-{os.getpid()}-{safe}"[:120]


def _start_fault_container(image: str, state: Path, scenario: str) -> str:
    name = _container_name(scenario)
    result = _docker(
        "run",
        "--detach",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",  # noqa: S108
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--volume",
        f"{state}:/state:rw,Z",
        "--entrypoint",
        "python",
        image,
        "-m",
        "tests.fixtures.container_process",
        "/state",
        scenario,
    )
    if not result.stdout.strip():
        raise ContainerGateError("docker run returned no container ID")
    return name


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise ContainerGateError(f"container did not create {path.name}")


def _remove(name: str) -> None:
    _docker("rm", "--force", name, check=False)


def _exit_code(name: str) -> int:
    result = _docker("inspect", "--format", "{{.State.ExitCode}}", name)
    try:
        return int(result.stdout.strip())
    except ValueError as error:
        raise ContainerGateError("container exit code is invalid") from error


def _test_phase_sigterm(image: str, root: Path) -> None:
    for phase in NON_TERMINAL_PHASES:
        state = root / f"phase-{phase}"
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        name = _start_fault_container(image, state, f"phase:{phase}")
        try:
            _wait_for(state / "ready")
            started = time.monotonic()
            _docker("stop", "--time", "120", name)
            elapsed = time.monotonic() - started
            if elapsed >= MAXIMUM_STOP_SECONDS or _exit_code(name) != 0:
                raise ContainerGateError(f"SIGTERM shutdown failed at phase {phase}")
            recovery = (state / "recovery").read_text(encoding="utf-8").strip()
            if recovery != f"checkpointed:{phase}":
                raise ContainerGateError(f"checkpoint missing at phase {phase}")
        finally:
            _remove(name)


def _test_forced_boundaries(image: str, root: Path) -> None:
    for boundary in FORCED_BOUNDARIES:
        state = root / boundary
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        first = _start_fault_container(image, state, boundary)
        try:
            _wait_for(state / "ready")
            _docker("kill", "--signal", "KILL", first)
            _docker("wait", first)
            if _exit_code(first) != 137 or (state / "recovery").exists():
                raise ContainerGateError(f"forced stop was not preserved at {boundary}")
        finally:
            _remove(first)

        (state / "ready").unlink(missing_ok=True)
        replacement = _start_fault_container(image, state, "recover")
        try:
            _wait_for(state / "complete")
            _docker("stop", "--time", "120", replacement)
            if _exit_code(replacement) != 0:
                raise ContainerGateError(f"replacement shutdown failed at {boundary}")
        finally:
            _remove(replacement)

        transaction_events = (state / "transaction-events").read_text(encoding="utf-8")
        discord_history = (state / "discord-history").read_text(encoding="utf-8")
        outbox = (state / "outbox").read_text(encoding="utf-8").strip()
        if transaction_events.splitlines() != ["commit"]:
            raise ContainerGateError(f"transaction duplicated at {boundary}")
        if discord_history.splitlines() != ["stable-content-hash:stable-message-id"]:
            raise ContainerGateError(f"Discord post duplicated at {boundary}")
        if outbox != "completed:stable-message-id":
            raise ContainerGateError(f"outbox did not reconcile at {boundary}")


def run_gate(production_image: str, fault_image: str, expected_architecture: str) -> None:
    """Execute the complete native container contract."""

    for image in (production_image, fault_image):
        _validate_image_name(image)
    inspect = json.loads(_docker("image", "inspect", production_image).stdout)
    validate_image_configuration(inspect, expected_architecture)
    _validate_runtime_security(production_image)
    with tempfile.TemporaryDirectory(prefix="shittim-step08b-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        _test_phase_sigterm(fault_image, root)
        _test_forced_boundaries(fault_image, root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-image", required=True)
    parser.add_argument("--fault-image", required=True)
    parser.add_argument("--expected-architecture", choices=("amd64", "arm64"), required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        run_gate(args.production_image, args.fault_image, args.expected_architecture)
    except (ContainerGateError, json.JSONDecodeError, OSError) as error:
        print(f"container gate failed: {error}")
        return 1
    print("native container gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
