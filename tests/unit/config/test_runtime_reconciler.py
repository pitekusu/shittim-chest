"""Fail-closed configuration tests for the runtime reconciler Lambda."""

from __future__ import annotations

import pytest

from shittim_chest.config.models import DEFAULT_AWS_REGION, StartupConfigurationError
from shittim_chest.config.runtime_reconciler import load_runtime_reconciler_settings


def environment() -> dict[str, str]:
    return {
        "AWS_REGION": DEFAULT_AWS_REGION,
        "SHITTIM_DYNAMODB_TABLE": "ShittimChest-Prod-Stateful",
        "SHITTIM_ECS_CLUSTER": "shittim-chest-runtime",
        "SHITTIM_ECS_SERVICE": "shittim-chest-runtime",
        "SHITTIM_STATUS_PUBLISHER_FUNCTION": "shittim-status-publisher",
    }


def test_load_runtime_reconciler_settings_accepts_resource_names_only() -> None:
    settings = load_runtime_reconciler_settings(environment())

    assert settings.aws_region == DEFAULT_AWS_REGION
    assert settings.table_name == "ShittimChest-Prod-Stateful"
    assert settings.ecs_cluster == "shittim-chest-runtime"
    assert settings.ecs_service == "shittim-chest-runtime"
    assert settings.status_publisher_function == "shittim-status-publisher"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "us-east-1"),
        ("SHITTIM_DYNAMODB_TABLE", " "),
        ("SHITTIM_ECS_CLUSTER", "cluster/other"),
        ("SHITTIM_ECS_SERVICE", "service padded "),
        ("SHITTIM_STATUS_PUBLISHER_FUNCTION", "arn:aws:lambda:unsupported"),
    ],
)
def test_load_runtime_reconciler_settings_rejects_invalid_identifiers(
    name: str,
    value: str,
) -> None:
    values = environment()
    values[name] = value

    with pytest.raises(StartupConfigurationError) as caught:
        load_runtime_reconciler_settings(values)

    assert str(caught.value) == "startup_configuration_invalid"


def test_load_runtime_reconciler_settings_rejects_missing_values() -> None:
    values = environment()
    del values["SHITTIM_ECS_SERVICE"]

    with pytest.raises(StartupConfigurationError):
        load_runtime_reconciler_settings(values)
