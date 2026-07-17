"""Tests for native image configuration validation."""

from __future__ import annotations

from copy import deepcopy

import pytest
from tools.run_container_gate import ContainerGateError, validate_image_configuration


def _inspect() -> list[object]:
    return [
        {
            "Architecture": "arm64",
            "Config": {
                "User": "10001:10001",
                "Entrypoint": ["python", "-m", "shittim_chest"],
                "StopSignal": "SIGTERM",
                "Healthcheck": {
                    "Test": ["CMD", "python", "-m", "shittim_chest.runtime.health"],
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("Architecture", "amd64", "architecture"),
        ("User", "root", "numeric UID"),
        ("StopSignal", "SIGKILL", "SIGTERM"),
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
    config = image["Config"]
    assert isinstance(config, dict)
    health = config["Healthcheck"]
    assert isinstance(health, dict)
    if field == "Architecture":
        image[field] = value
    elif field in {"User", "StopSignal"}:
        config[field] = value
    else:
        health[field] = value

    with pytest.raises(ContainerGateError, match=message):
        validate_image_configuration(document, "arm64")
