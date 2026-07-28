"""Token-free, fail-closed Discord ingress configuration."""

import json

import pytest

from shittim_chest.config.ingress import (
    load_ingress_bootstrap_settings,
    load_ingress_runtime_settings,
)
from shittim_chest.config.models import StartupConfigurationError


def environment() -> dict[str, str]:
    return {
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "test-table",
        "SHITTIM_RUNTIME_CONFIG_PARAMETER": ("/shittim-chest/production/runtime/v0001"),
        "SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER": (
            "/shittim-chest/production/discord/moderator/public-key"
        ),
        "SHITTIM_STATUS_PUBLISHER_FUNCTION": "test-status-publisher",
        "SHITTIM_RUNTIME_RECONCILER_FUNCTION": "test-runtime-reconciler",
    }


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


class FakeReader:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.names: list[str] = []

    async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
        assert with_decryption
        self.names.append(name)
        return self.values[name]


@pytest.mark.asyncio
async def test_loads_only_runtime_and_public_key_parameters() -> None:
    settings = load_ingress_bootstrap_settings(environment())
    reader = FakeReader(
        {
            settings.runtime_config_parameter: runtime_json(),
            settings.discord_public_key_parameter: "ab" * 32,
        }
    )

    resolved = await load_ingress_runtime_settings(settings, reader)

    assert resolved.discord.guild_id == "101"
    assert resolved.public_key_hex == "ab" * 32
    assert "abab" not in repr(resolved)
    assert set(reader.names) == {
        settings.runtime_config_parameter,
        settings.discord_public_key_parameter,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "us-east-1"),
        ("SHITTIM_DYNAMODB_TABLE", "bad table"),
        ("SHITTIM_RUNTIME_CONFIG_PARAMETER", "/runtime/latest"),
        ("SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER", "/another/public-key"),
        ("SHITTIM_RUNTIME_RECONCILER_FUNCTION", ""),
        ("SHITTIM_STATUS_PUBLISHER_FUNCTION", "arn:aws:lambda:ap-northeast-1:123:function:x"),
        ("SHITTIM_STATUS_PUBLISHER_FUNCTION", "x" * 65),
    ],
)
def test_invalid_environment_fails_without_value_echo(name: str, value: str) -> None:
    values = environment()
    values[name] = value

    with pytest.raises(StartupConfigurationError) as caught:
        load_ingress_bootstrap_settings(values)

    if value:
        assert value not in str(caught.value)


@pytest.mark.asyncio
async def test_runtime_payload_version_must_match_parameter_path() -> None:
    settings = load_ingress_bootstrap_settings(environment())
    reader = FakeReader(
        {
            settings.runtime_config_parameter: runtime_json(version="v0002"),
            settings.discord_public_key_parameter: "ab" * 32,
        }
    )

    with pytest.raises(StartupConfigurationError):
        await load_ingress_runtime_settings(settings, reader)
