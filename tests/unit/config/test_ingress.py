"""Token-free, fail-closed Discord ingress configuration."""

import json

import pytest

from shittim_chest.config.ingress import (
    load_ingress_bootstrap_settings,
    load_ingress_runtime_settings,
)
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
        "SHITTIM_RUNTIME_CONFIG_PARAMETER": "/shittim-chest/production/runtime/v0001",
        "SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER": (
            "/shittim-chest/production/discord/moderator/public-key"
        ),
    }


def test_loads_only_versioned_parameter_names_before_snapshot() -> None:
    settings = load_ingress_bootstrap_settings(environment())

    assert settings.runtime_config_parameter.endswith("/runtime/v0001")
    assert settings.discord_public_key_parameter.endswith("/moderator/public-key")


@pytest.mark.asyncio
async def test_resolves_and_validates_secure_strings_before_snapshot() -> None:
    class Reader:
        async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
            assert with_decryption
            if name.endswith("/runtime/v0001"):
                return runtime_json()
            if name.endswith("/moderator/public-key"):
                return "ab" * 32
            raise AssertionError("unexpected parameter")

    runtime = await load_ingress_runtime_settings(
        load_ingress_bootstrap_settings(environment()),
        Reader(),
    )

    assert runtime.discord.guild_id == "101"
    assert runtime.public_key_hex == "ab" * 32
    assert "abab" not in repr(runtime)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_REGION", "us-east-1"),
        ("SHITTIM_DYNAMODB_TABLE", "bad table"),
        ("SHITTIM_RUNTIME_CONFIG_PARAMETER", "/wrong/runtime/v0001"),
        ("SHITTIM_RUNTIME_CONFIG_PARAMETER", "/shittim-chest/production/runtime/latest"),
        ("SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER", "/wrong/public-key"),
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
async def test_runtime_payload_version_must_match_parameter_version() -> None:
    class Reader:
        async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
            del with_decryption
            return "ab" * 32 if name.endswith("public-key") else runtime_json(version="v0002")

    with pytest.raises(StartupConfigurationError):
        await load_ingress_runtime_settings(
            load_ingress_bootstrap_settings(environment()),
            Reader(),
        )
