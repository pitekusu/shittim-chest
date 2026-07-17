from __future__ import annotations

import json

import pytest

from shittim_chest.application import DiscordBotSlot
from shittim_chest.config import StartupConfigurationError, load_bootstrap_config
from shittim_chest.domain import ParticipantSlot


def test_load_bootstrap_config_validates_and_maps_private_inputs() -> None:
    config = load_bootstrap_config(_valid_environment())

    assert config.environment == "production"
    assert config.aws_region == "ap-northeast-1"
    assert config.table_name == "shittim-chest-production"
    assert config.config_version == "v0001"
    assert config.runtime.guild_id == "101"
    assert config.runtime.allowed_channel_ids == frozenset({"201", "202"})
    assert config.discord_tokens[DiscordBotSlot.MODERATOR] == "token-moderator-placeholder"
    assert config.participant_prompts()[ParticipantSlot.PARTICIPANT_B] == (
        "Generic private prompt for participant-b."
    )
    rendered = repr(config)
    assert "openai-key-placeholder" not in rendered
    assert "token-moderator-placeholder" not in rendered
    assert "Generic private prompt" not in rendered


@pytest.mark.parametrize(
    "mutation",
    (
        {"SHITTIM_ENVIRONMENT": "development"},
        {"AWS_REGION": "us-east-1"},
        {"SHITTIM_DYNAMODB_TABLE": ""},
        {"SHITTIM_DYNAMODB_TABLE": "invalid/table"},
        {"OPENAI_API_KEY": ""},
        {"DISCORD_TOKEN_PARTICIPANT_C": "token-moderator-placeholder"},
        {"SHITTIM_PREVIOUS_COMMAND_SCHEMA_HASH": "not-a-hash"},
    ),
)
def test_load_bootstrap_config_fails_closed_for_invalid_process_inputs(
    mutation: dict[str, str],
) -> None:
    environment = _valid_environment()
    environment.update(mutation)

    with pytest.raises(StartupConfigurationError) as captured:
        load_bootstrap_config(environment)

    assert str(captured.value) == "startup_configuration_invalid"


def test_load_bootstrap_config_redacts_invalid_private_values() -> None:
    environment = _valid_environment()
    private_marker = "private-prompt-marker"
    environment["SHITTIM_PERSONA_PARTICIPANT_A_JSON"] = json.dumps(
        {
            "schema_version": "1",
            "config_version": "v0001",
            "slot": "participant-a",
            "display_name": "Generic A",
            "system_prompt": private_marker,
            "unexpected": private_marker,
        }
    )

    with pytest.raises(StartupConfigurationError) as captured:
        load_bootstrap_config(environment)

    assert private_marker not in str(captured.value)
    assert private_marker not in repr(captured.value)


def test_load_bootstrap_config_requires_one_matching_version_for_all_payloads() -> None:
    environment = _valid_environment()
    persona = json.loads(environment["SHITTIM_PERSONA_PARTICIPANT_C_JSON"])
    persona["config_version"] = "v0002"
    environment["SHITTIM_PERSONA_PARTICIPANT_C_JSON"] = json.dumps(persona)

    with pytest.raises(StartupConfigurationError):
        load_bootstrap_config(environment)


def _valid_environment() -> dict[str, str]:
    environment = {
        "SHITTIM_ENVIRONMENT": "production",
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "shittim-chest-production",
        "SHITTIM_LOG_LEVEL": "INFO",
        "OPENAI_API_KEY": "openai-key-placeholder",
        "DISCORD_TOKEN_MODERATOR": "token-moderator-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_A": "token-participant-a-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_B": "token-participant-b-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_C": "token-participant-c-placeholder",
        "SHITTIM_PREVIOUS_COMMAND_SCHEMA_HASH": "a" * 64,
        "SHITTIM_RUNTIME_CONFIG_JSON": json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "guild_id": "101",
                "allowed_channel_ids": ["201", "202"],
                "identities": [
                    {"slot": "moderator", "application_id": "301"},
                    {"slot": "participant-a", "application_id": "302"},
                    {"slot": "participant-b", "application_id": "303"},
                    {"slot": "participant-c", "application_id": "304"},
                ],
            }
        ),
    }
    for slot in DiscordBotSlot:
        environment[
            {
                DiscordBotSlot.MODERATOR: "SHITTIM_PERSONA_MODERATOR_JSON",
                DiscordBotSlot.PARTICIPANT_A: "SHITTIM_PERSONA_PARTICIPANT_A_JSON",
                DiscordBotSlot.PARTICIPANT_B: "SHITTIM_PERSONA_PARTICIPANT_B_JSON",
                DiscordBotSlot.PARTICIPANT_C: "SHITTIM_PERSONA_PARTICIPANT_C_JSON",
            }[slot]
        ] = json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "slot": slot.value,
                "display_name": f"Generic {slot.value}",
                "system_prompt": f"Generic private prompt for {slot.value}.",
            }
        )
    return environment
