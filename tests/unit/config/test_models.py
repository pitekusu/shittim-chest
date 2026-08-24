from __future__ import annotations

import json
from hashlib import sha256

import pytest

from shittim_chest.application import DiscordBotSlot
from shittim_chest.config import (
    RUNTIME_PROMPT_NAMES,
    RUNTIME_PROMPTS_ACTIVE_PARAMETER,
    StartupConfigurationError,
    load_bootstrap_config,
    parse_runtime_prompt_revision,
    runtime_prompt_parameter_names,
)
from shittim_chest.domain import ParticipantSlot

PROMPT_REVISION = "r" + "0" * 26


def test_load_bootstrap_config_validates_and_maps_private_inputs() -> None:
    config = load_bootstrap_config(_valid_environment())

    assert config.environment == "production"
    assert config.aws_region == "ap-northeast-1"
    assert config.table_name == "shittim-chest-production"
    assert config.status_publisher_function == "shittim-status-publisher"
    assert config.config_version == "v0001"
    assert config.runtime.guild_id == "101"
    assert config.runtime.allowed_channel_ids == frozenset({"201", "202"})
    assert config.runtime.farewell_channel_id == "201"
    assert config.discord_tokens[DiscordBotSlot.MODERATOR] == "token-moderator-placeholder"
    assert config.participant_prompts()[ParticipantSlot.PARTICIPANT_B] == (
        "Generic private prompt for participant-b."
    )
    assert config.participant_display_names() == {
        ParticipantSlot.PARTICIPANT_A: "Generic participant-a",
        ParticipantSlot.PARTICIPANT_B: "Generic participant-b",
        ParticipantSlot.PARTICIPANT_C: "Generic participant-c",
    }
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
        {"SHITTIM_STATUS_PUBLISHER_FUNCTION": "invalid/function"},
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


def test_load_bootstrap_config_rejects_renderer_incompatible_display_name() -> None:
    environment = _valid_environment()
    persona = json.loads(environment["SHITTIM_PERSONA_PARTICIPANT_A_JSON"])
    persona["display_name"] = "Generic\u200dA"
    environment["SHITTIM_PERSONA_PARTICIPANT_A_JSON"] = json.dumps(persona)

    with pytest.raises(StartupConfigurationError) as captured:
        load_bootstrap_config(environment)

    assert str(captured.value) == "startup_configuration_invalid"


@pytest.mark.parametrize(
    ("first_name", "second_name"),
    [
        ("\u00e9", "e\u0301"),
        ("A\tB", "A B"),
        ("A\rB", "A\nB"),
    ],
)
def test_load_bootstrap_config_rejects_display_equivalent_participant_names(
    first_name: str,
    second_name: str,
) -> None:
    environment = _valid_environment()
    participant_a = json.loads(environment["SHITTIM_PERSONA_PARTICIPANT_A_JSON"])
    participant_b = json.loads(environment["SHITTIM_PERSONA_PARTICIPANT_B_JSON"])
    participant_a["display_name"] = first_name
    participant_b["display_name"] = second_name
    environment["SHITTIM_PERSONA_PARTICIPANT_A_JSON"] = json.dumps(participant_a)
    environment["SHITTIM_PERSONA_PARTICIPANT_B_JSON"] = json.dumps(participant_b)

    with pytest.raises(StartupConfigurationError) as captured:
        load_bootstrap_config(environment)

    assert str(captured.value) == "startup_configuration_invalid"


def test_load_bootstrap_config_does_not_apply_vote_renderer_to_moderator_name() -> None:
    environment = _valid_environment()
    persona = json.loads(environment["SHITTIM_PERSONA_MODERATOR_JSON"])
    persona["display_name"] = "Generic\u200dModerator"
    environment["SHITTIM_PERSONA_MODERATOR_JSON"] = json.dumps(persona)

    config = load_bootstrap_config(environment)

    assert config.personas[DiscordBotSlot.MODERATOR].display_name == "Generic\u200dModerator"


def test_load_bootstrap_config_requires_one_matching_version_for_all_payloads() -> None:
    environment = _valid_environment()
    persona = json.loads(environment["SHITTIM_PERSONA_PARTICIPANT_C_JSON"])
    persona["config_version"] = "v0002"
    environment["SHITTIM_PERSONA_PARTICIPANT_C_JSON"] = json.dumps(persona)

    with pytest.raises(StartupConfigurationError):
        load_bootstrap_config(environment)


def test_runtime_prompt_revision_overlays_text_without_changing_display_names() -> None:
    prompts = {name: f"Canonical prompt for {name}." for name in RUNTIME_PROMPT_NAMES}
    revision = parse_runtime_prompt_revision(
        revision=PROMPT_REVISION,
        manifest_json=_prompt_manifest(prompts),
        prompts=prompts,
    )

    config = load_bootstrap_config(_valid_environment()).with_runtime_prompt_revision(revision)

    assert config.runtime_prompt_revision == PROMPT_REVISION
    assert config.system_prompt == prompts["system"]
    assert config.moderator_prompt == prompts["moderator"]
    assert config.participant_prompts() == {
        ParticipantSlot.PARTICIPANT_A: prompts["participant-a"],
        ParticipantSlot.PARTICIPANT_B: prompts["participant-b"],
        ParticipantSlot.PARTICIPANT_C: prompts["participant-c"],
    }
    assert config.participant_display_names()[ParticipantSlot.PARTICIPANT_A] == (
        "Generic participant-a"
    )
    assert "Canonical prompt" not in repr(config)


@pytest.mark.parametrize(
    "invalid_prompt",
    [" ", "line one\r\nline two", "e\u0301", "x" * 3_501],
)
def test_runtime_prompt_revision_rejects_noncanonical_or_oversized_text(
    invalid_prompt: str,
) -> None:
    prompts = {name: f"Canonical prompt for {name}." for name in RUNTIME_PROMPT_NAMES}
    manifest = _prompt_manifest(prompts)
    prompts["system"] = invalid_prompt

    with pytest.raises(StartupConfigurationError) as captured:
        parse_runtime_prompt_revision(
            revision=PROMPT_REVISION,
            manifest_json=manifest,
            prompts=prompts,
        )

    assert str(captured.value) == "startup_configuration_invalid"
    assert invalid_prompt not in str(captured.value)


def test_runtime_prompt_revision_requires_exact_checksum_verified_manifest() -> None:
    prompts = {name: f"Canonical prompt for {name}." for name in RUNTIME_PROMPT_NAMES}
    manifest = json.loads(_prompt_manifest(prompts))
    manifest["checksums"]["participant-c"] = "f" * 64

    with pytest.raises(StartupConfigurationError):
        parse_runtime_prompt_revision(
            revision=PROMPT_REVISION,
            manifest_json=json.dumps(manifest),
            prompts=prompts,
        )

    missing_base = json.loads(_prompt_manifest(prompts))
    missing_base.pop("base_revision")
    with pytest.raises(StartupConfigurationError):
        parse_runtime_prompt_revision(
            revision=PROMPT_REVISION,
            manifest_json=json.dumps(missing_base),
            prompts=prompts,
        )


def test_runtime_prompt_pointer_environment_is_optional_but_exact() -> None:
    legacy = load_bootstrap_config(_valid_environment())
    assert legacy.runtime_prompts_active_parameter is None

    environment = _valid_environment()
    environment["SHITTIM_RUNTIME_PROMPTS_ACTIVE_PARAMETER"] = RUNTIME_PROMPTS_ACTIVE_PARAMETER
    configured = load_bootstrap_config(environment)
    assert configured.runtime_prompts_active_parameter == RUNTIME_PROMPTS_ACTIVE_PARAMETER
    assert runtime_prompt_parameter_names(PROMPT_REVISION)["manifest"].endswith("/manifest")

    environment["SHITTIM_RUNTIME_PROMPTS_ACTIVE_PARAMETER"] += "/wrong"
    with pytest.raises(StartupConfigurationError):
        load_bootstrap_config(environment)


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": "1"},
        {"farewell_channel_id": "999"},
        {"farewell_channel_id": None},
    ],
)
def test_runtime_config_v2_requires_an_allowed_farewell_channel(
    mutation: dict[str, object],
) -> None:
    environment = _valid_environment()
    runtime = json.loads(environment["SHITTIM_RUNTIME_CONFIG_JSON"])
    runtime.update(mutation)
    if mutation.get("farewell_channel_id", object()) is None:
        runtime.pop("farewell_channel_id", None)
    environment["SHITTIM_RUNTIME_CONFIG_JSON"] = json.dumps(runtime)

    with pytest.raises(StartupConfigurationError):
        load_bootstrap_config(environment)


def _valid_environment() -> dict[str, str]:
    environment = {
        "SHITTIM_ENVIRONMENT": "production",
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "shittim-chest-production",
        "SHITTIM_STATUS_PUBLISHER_FUNCTION": "shittim-status-publisher",
        "SHITTIM_LOG_LEVEL": "INFO",
        "OPENAI_API_KEY": "openai-key-placeholder",
        "DISCORD_TOKEN_MODERATOR": "token-moderator-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_A": "token-participant-a-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_B": "token-participant-b-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_C": "token-participant-c-placeholder",
        "SHITTIM_PREVIOUS_COMMAND_SCHEMA_HASH": "a" * 64,
        "SHITTIM_RUNTIME_CONFIG_JSON": json.dumps(
            {
                "schema_version": "2",
                "config_version": "v0001",
                "guild_id": "101",
                "allowed_channel_ids": ["201", "202"],
                "farewell_channel_id": "201",
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


def _prompt_manifest(prompts: dict[str, str]) -> str:
    return json.dumps(
        {
            "schema_version": "1",
            "revision": PROMPT_REVISION,
            "created_at": "2026-08-24T00:00:00Z",
            "action": "publish",
            "base_revision": None,
            "checksums": {
                name: sha256(prompts[name].encode("utf-8")).hexdigest()
                for name in RUNTIME_PROMPT_NAMES
            },
        }
    )
