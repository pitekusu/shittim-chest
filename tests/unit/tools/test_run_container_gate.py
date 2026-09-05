"""Tests for native image configuration validation."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from subprocess import CompletedProcess
from typing import cast

import pytest
from tools import run_container_gate
from tools.run_container_gate import (
    NON_TERMINAL_PHASES,
    ContainerGateError,
    validate_image_configuration,
)

from shittim_chest.domain.debate_state import NORMAL_PHASE_FLOW


def _inspect() -> list[object]:
    return [
        {
            "Architecture": "arm64",
            "Config": {
                "User": "65532:65532",
                "Entrypoint": ["python", "-m", "shittim_chest"],
                "StopSignal": "SIGTERM",
                "Labels": {
                    "com.docker.dhi.distro": "debian-13",
                    "com.docker.dhi.name": "dhi/python",
                    "com.docker.dhi.package-manager": "",
                    "com.docker.dhi.shell": "",
                    "com.docker.dhi.variant": "runtime",
                },
                "Healthcheck": {
                    "Test": ["CMD", "python", "-m", "shittim_chest.healthcheck"],
                    "Interval": 10_000_000_000,
                    "Timeout": 3_000_000_000,
                    "StartPeriod": 30_000_000_000,
                    "Retries": 3,
                },
            },
        }
    ]


def test_native_arm64_image_configuration_is_accepted() -> None:
    validate_image_configuration(_inspect(), "arm64")


def test_fault_gate_covers_every_non_terminal_domain_phase() -> None:
    assert tuple(phase.value for phase in NORMAL_PHASE_FLOW[:-1]) == NON_TERMINAL_PHASES


@pytest.mark.parametrize("recovery_drills", [False, True])
def test_cli_runs_security_checks_and_only_explicit_recovery_drills(
    monkeypatch: pytest.MonkeyPatch, recovery_drills: bool
) -> None:
    arguments = [
        "gate",
        "--production-image",
        "local:production",
        "--expected-architecture",
        "arm64",
    ]
    if recovery_drills:
        arguments.extend(["--fault-image", "local:fault"])
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(
        run_container_gate,
        "_docker",
        lambda *args: CompletedProcess(args, 0, stdout=json.dumps(_inspect())),
    )
    performed: list[str] = []
    monkeypatch.setattr(
        run_container_gate, "_validate_runtime_security", lambda image: performed.append("security")
    )
    monkeypatch.setattr(
        run_container_gate, "_test_phase_sigterm", lambda image, root: performed.append("sigterm")
    )
    monkeypatch.setattr(
        run_container_gate,
        "_test_forced_boundaries",
        lambda image, root: performed.append("recovery"),
    )

    assert run_container_gate.main() == 0
    assert performed == (["security", "sigterm", "recovery"] if recovery_drills else ["security"])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("Architecture", "amd64", "architecture"),
        ("User", "root", "numeric DHI"),
        ("StopSignal", "SIGKILL", "SIGTERM"),
        ("com.docker.dhi.variant", "dev", "DHI runtime"),
        ("Interval", 1, "Interval"),
    ],
)
def test_invalid_image_configuration_is_rejected(
    field: str,
    value: object,
    message: str,
) -> None:
    document = deepcopy(_inspect())
    image = document[0]
    assert isinstance(image, dict)
    image = cast(dict[str, object], image)
    config = image["Config"]
    assert isinstance(config, dict)
    config = cast(dict[str, object], config)
    health = config["Healthcheck"]
    assert isinstance(health, dict)
    health = cast(dict[str, object], health)
    if field == "Architecture":
        image[field] = value
    elif field in {"User", "StopSignal"}:
        config[field] = value
    elif field.startswith("com.docker.dhi"):
        labels = config["Labels"]
        assert isinstance(labels, dict)
        labels = cast(dict[str, object], labels)
        labels[field] = value
    else:
        health[field] = value

    with pytest.raises(ContainerGateError, match=message):
        validate_image_configuration(document, "arm64")
