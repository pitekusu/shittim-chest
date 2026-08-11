"""Status Publisher configuration is fixed to the least-privilege parameter path."""

import json

import pytest

from shittim_chest.config.models import StartupConfigurationError
from shittim_chest.config.status_publisher import (
    MODERATOR_TOKEN_PARAMETER,
    load_status_publisher_settings,
    load_status_runtime_config,
)


def environment() -> dict[str, str]:
    return {
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "shittim-table",
        "SHITTIM_RUNTIME_CONFIG_PARAMETER": "/shittim-chest/production/runtime/v0001",
        "SHITTIM_MODERATOR_TOKEN_PARAMETER": MODERATOR_TOKEN_PARAMETER,
    }


def test_loads_only_resource_identifiers_and_the_fixed_moderator_path() -> None:
    settings = load_status_publisher_settings(environment())

    assert settings.aws_region == "ap-northeast-1"
    assert settings.table_name == "shittim-table"
    assert settings.runtime_config_parameter.endswith("/v0001")
    assert settings.moderator_token_parameter == MODERATOR_TOKEN_PARAMETER


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "us-east-1"),
        ("AWS_REGION", " ap-northeast-1"),
        ("SHITTIM_DYNAMODB_TABLE", " table"),
        ("SHITTIM_RUNTIME_CONFIG_PARAMETER", "/shittim-chest/production/runtime/latest"),
        (
            "SHITTIM_MODERATOR_TOKEN_PARAMETER",
            "/shittim-chest/production/discord/participant-a/token",
        ),
    ],
)
def test_rejects_unexpected_region_table_or_secret_path(name: str, value: str) -> None:
    source = environment()
    source[name] = value

    with pytest.raises(StartupConfigurationError):
        load_status_publisher_settings(source)


def test_rejects_missing_values_without_echoing_them() -> None:
    source = environment()
    del source["SHITTIM_MODERATOR_TOKEN_PARAMETER"]

    with pytest.raises(StartupConfigurationError) as caught:
        load_status_publisher_settings(source)
    assert str(caught.value) == "startup_configuration_invalid"


@pytest.mark.asyncio
async def test_runtime_parameter_version_must_match_its_payload() -> None:
    class Reader:
        async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
            del name, with_decryption
            return json.dumps(
                {
                    "schema_version": "2",
                    "config_version": "v0002",
                    "guild_id": "101",
                    "allowed_channel_ids": ["102"],
                    "farewell_channel_id": "102",
                    "identities": [
                        {"slot": "moderator", "application_id": "200"},
                        {"slot": "participant-a", "application_id": "201"},
                        {"slot": "participant-b", "application_id": "202"},
                        {"slot": "participant-c", "application_id": "203"},
                    ],
                }
            )

    with pytest.raises(StartupConfigurationError):
        await load_status_runtime_config(
            load_status_publisher_settings(environment()),
            Reader(),
        )
