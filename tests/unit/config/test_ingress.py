"""Token-free, fail-closed Discord ingress configuration."""

import json

import pytest

from shittim_chest.config.ingress import load_ingress_bootstrap_settings
from shittim_chest.config.models import StartupConfigurationError


def runtime_json(*, version: str = "v0001") -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "config_version": version,
            "guild_id": "101",
            "allowed_channel_ids": ["102"],
            "identities": [
                {"slot": "moderator", "application_id": "201"},
                {"slot": "participant-a", "application_id": "202"},
                {"slot": "participant-b", "application_id": "203"},
                {"slot": "participant-c", "application_id": "204"},
            ],
        }
    )


def environment() -> dict[str, str]:
    return {
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "test-table",
        "SHITTIM_RUNTIME_CONFIG_JSON": runtime_json(),
        "SHITTIM_RUNTIME_CONFIG_VERSION": "v0001",
        "SHITTIM_DISCORD_PUBLIC_KEY_HEX": "ab" * 32,
    }


def test_loads_deploy_time_runtime_and_public_key_without_parameter_reader() -> None:
    settings = load_ingress_bootstrap_settings(environment())

    assert settings.discord.guild_id == "101"
    assert settings.config_version == "v0001"
    assert settings.public_key_hex == "ab" * 32
    assert "abab" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "us-east-1"),
        ("SHITTIM_DYNAMODB_TABLE", "bad table"),
        ("SHITTIM_RUNTIME_CONFIG_JSON", "{}"),
        ("SHITTIM_RUNTIME_CONFIG_VERSION", "latest"),
        ("SHITTIM_DISCORD_PUBLIC_KEY_HEX", "not-a-public-key"),
        ("SHITTIM_DISCORD_PUBLIC_KEY_HEX", "AB" * 32),
    ],
)
def test_invalid_environment_fails_without_value_echo(name: str, value: str) -> None:
    values = environment()
    values[name] = value

    with pytest.raises(StartupConfigurationError) as caught:
        load_ingress_bootstrap_settings(values)

    if value:
        assert value not in str(caught.value)


def test_runtime_payload_version_must_match_deployment_version() -> None:
    values = environment()
    values["SHITTIM_RUNTIME_CONFIG_JSON"] = runtime_json(version="v0002")

    with pytest.raises(StartupConfigurationError):
        load_ingress_bootstrap_settings(values)
