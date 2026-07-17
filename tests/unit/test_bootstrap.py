from __future__ import annotations

import json
import logging

import pytest

import shittim_chest.bootstrap as bootstrap
from shittim_chest.bootstrap import ProductionRuntime, build_production_runtime
from shittim_chest.config import load_bootstrap_config


@pytest.mark.asyncio
async def test_production_composition_builds_and_closes_without_external_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local-placeholder")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local-placeholder")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    config = load_bootstrap_config(_environment())

    runtime = build_production_runtime(config)

    assert isinstance(runtime, ProductionRuntime)
    await runtime.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_from_environment_keeps_third_party_root_logging_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basic_config: dict[str, object] = {}
    application_levels: list[int] = []
    runtime_runs: list[str] = []

    class _ApplicationLogger:
        def setLevel(self, level: int) -> None:
            application_levels.append(level)

    class _Runtime:
        async def run(self) -> None:
            runtime_runs.append("run")

    def _configure_logging(*, level: int, format: str) -> None:
        basic_config.update(level=level, format=format)

    def _build(config: object) -> _Runtime:
        del config
        return _Runtime()

    environment = _environment()
    environment["SHITTIM_LOG_LEVEL"] = "DEBUG"
    monkeypatch.setattr(logging, "basicConfig", _configure_logging)
    monkeypatch.setattr(bootstrap, "_LOGGER", _ApplicationLogger())
    monkeypatch.setattr(bootstrap, "build_production_runtime", _build)

    await bootstrap.run_from_environment(environment)

    assert basic_config == {"level": logging.WARNING, "format": "%(message)s"}
    assert application_levels == [logging.DEBUG]
    assert runtime_runs == ["run"]


def _environment() -> dict[str, str]:
    values = {
        "SHITTIM_ENVIRONMENT": "production",
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "local-table",
        "OPENAI_API_KEY": "openai-key-placeholder",
        "DISCORD_TOKEN_MODERATOR": "token-moderator-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_A": "token-a-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_B": "token-b-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_C": "token-c-placeholder",
        "SHITTIM_RUNTIME_CONFIG_JSON": json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "guild_id": "11",
                "allowed_channel_ids": ["21"],
                "identities": [
                    {"slot": "moderator", "application_id": "31"},
                    {"slot": "participant-a", "application_id": "32"},
                    {"slot": "participant-b", "application_id": "33"},
                    {"slot": "participant-c", "application_id": "34"},
                ],
            }
        ),
    }
    persona_env = {
        "moderator": "SHITTIM_PERSONA_MODERATOR_JSON",
        "participant-a": "SHITTIM_PERSONA_PARTICIPANT_A_JSON",
        "participant-b": "SHITTIM_PERSONA_PARTICIPANT_B_JSON",
        "participant-c": "SHITTIM_PERSONA_PARTICIPANT_C_JSON",
    }
    for slot, name in persona_env.items():
        values[name] = json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "slot": slot,
                "display_name": f"Generic {slot}",
                "system_prompt": f"Generic instructions for {slot}.",
            }
        )
    return values
