"""Validate injected production configuration without exposing private values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

CONFIG_SCHEMA_VERSION = "1"
PRODUCTION_ENVIRONMENT = "production"
DEFAULT_AWS_REGION = "ap-northeast-1"

_RUNTIME_CONFIG_ENV = "SHITTIM_RUNTIME_CONFIG_JSON"
_PERSONA_ENV = {
    DiscordBotSlot.MODERATOR: "SHITTIM_PERSONA_MODERATOR_JSON",
    DiscordBotSlot.PARTICIPANT_A: "SHITTIM_PERSONA_PARTICIPANT_A_JSON",
    DiscordBotSlot.PARTICIPANT_B: "SHITTIM_PERSONA_PARTICIPANT_B_JSON",
    DiscordBotSlot.PARTICIPANT_C: "SHITTIM_PERSONA_PARTICIPANT_C_JSON",
}
_TOKEN_ENV = {
    DiscordBotSlot.MODERATOR: "DISCORD_TOKEN_MODERATOR",
    DiscordBotSlot.PARTICIPANT_A: "DISCORD_TOKEN_PARTICIPANT_A",
    DiscordBotSlot.PARTICIPANT_B: "DISCORD_TOKEN_PARTICIPANT_B",
    DiscordBotSlot.PARTICIPANT_C: "DISCORD_TOKEN_PARTICIPANT_C",
}


class StartupConfigurationError(RuntimeError):
    """Stable, content-free startup configuration failure."""

    code = "startup_configuration_invalid"

    def __init__(self) -> None:
        super().__init__(self.code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DiscordIdentityPayload(_StrictModel):
    slot: DiscordBotSlot
    application_id: str


class RuntimeConfigPayload(_StrictModel):
    schema_version: Literal["1"]
    config_version: str = Field(pattern=r"^v[0-9]{4}$")
    guild_id: str
    allowed_channel_ids: tuple[str, ...] = Field(min_length=1)
    identities: tuple[DiscordIdentityPayload, ...] = Field(min_length=4, max_length=4)


class PersonaConfigPayload(_StrictModel):
    schema_version: Literal["1"]
    config_version: str = Field(pattern=r"^v[0-9]{4}$")
    slot: DiscordBotSlot
    display_name: str = Field(min_length=1, max_length=80)
    system_prompt: str = Field(min_length=1)

    @field_validator("display_name", "system_prompt")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("system_prompt")
    @classmethod
    def _limit_prompt_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 3_500:
            raise ValueError("prompt exceeds private configuration limit")
        return value


@dataclass(frozen=True, slots=True)
class PersonaConfig:
    """Validated private display and instruction settings for one Bot slot."""

    schema_version: str
    config_version: str
    slot: DiscordBotSlot
    display_name: str = field(repr=False)
    system_prompt: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class BootstrapConfig:
    """All validated process inputs required by the production composition root."""

    environment: str
    aws_region: str
    table_name: str
    log_level: str
    runtime: DiscordRuntimeConfig
    config_version: str
    personas: Mapping[DiscordBotSlot, PersonaConfig] = field(repr=False)
    discord_tokens: Mapping[DiscordBotSlot, str] = field(repr=False)
    openai_api_key: str = field(repr=False)
    previous_command_schema_hash: str | None = None

    def participant_prompts(self) -> Mapping[ParticipantSlot, str]:
        """Map private participant slots to their validated prompt text."""

        return MappingProxyType(
            {
                participant: self.personas[DiscordBotSlot(participant.value)].system_prompt
                for participant in PARTICIPANTS
            }
        )


def load_bootstrap_config(environ: Mapping[str, str]) -> BootstrapConfig:
    """Load only injected values; invalid or incomplete input fails without value echoing."""

    try:
        environment = _required(environ, "SHITTIM_ENVIRONMENT")
        if environment != PRODUCTION_ENVIRONMENT:
            raise ValueError("unsupported environment")
        aws_region = environ.get("AWS_REGION", DEFAULT_AWS_REGION).strip()
        if aws_region != DEFAULT_AWS_REGION:
            raise ValueError("unsupported AWS region")
        table_name = _required(environ, "SHITTIM_DYNAMODB_TABLE")
        if re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name) is None:
            raise ValueError("invalid DynamoDB table name")
        log_level = environ.get("SHITTIM_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")

        runtime_payload = RuntimeConfigPayload.model_validate_json(
            _required(environ, _RUNTIME_CONFIG_ENV)
        )
        runtime = DiscordRuntimeConfig(
            guild_id=runtime_payload.guild_id,
            allowed_channel_ids=frozenset(runtime_payload.allowed_channel_ids),
            identities=tuple(
                DiscordIdentityConfig(slot=item.slot, application_id=item.application_id)
                for item in runtime_payload.identities
            ),
            schema_version=runtime_payload.schema_version,
        )
        personas = {
            slot: _persona_from_json(_required(environ, env_name))
            for slot, env_name in _PERSONA_ENV.items()
        }
        if any(persona.slot is not slot for slot, persona in personas.items()):
            raise ValueError("persona slot mismatch")
        versions = {runtime_payload.config_version} | {
            persona.config_version for persona in personas.values()
        }
        if len(versions) != 1:
            raise ValueError("configuration version mismatch")

        tokens = {slot: _required(environ, env_name) for slot, env_name in _TOKEN_ENV.items()}
        if len(set(tokens.values())) != len(DISCORD_BOT_SLOTS):
            raise ValueError("Discord Bot tokens must be distinct")
        previous_hash = environ.get("SHITTIM_PREVIOUS_COMMAND_SCHEMA_HASH")
        if previous_hash is not None:
            previous_hash = previous_hash.strip() or None
            if previous_hash is not None and (
                len(previous_hash) != 64
                or any(character not in "0123456789abcdef" for character in previous_hash)
            ):
                raise ValueError("invalid command schema hash")

        return BootstrapConfig(
            environment=environment,
            aws_region=aws_region,
            table_name=table_name,
            log_level=log_level,
            runtime=runtime,
            config_version=runtime_payload.config_version,
            personas=MappingProxyType(personas),
            discord_tokens=MappingProxyType(tokens),
            openai_api_key=_required(environ, "OPENAI_API_KEY"),
            previous_command_schema_hash=previous_hash,
        )
    except KeyError, TypeError, ValueError, ValidationError:
        raise StartupConfigurationError from None


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name]
    if not value.strip():
        raise ValueError("required environment value is blank")
    return value


def _persona_from_json(raw: str) -> PersonaConfig:
    payload = PersonaConfigPayload.model_validate_json(raw)
    return PersonaConfig(
        schema_version=payload.schema_version,
        config_version=payload.config_version,
        slot=payload.slot,
        display_name=payload.display_name,
        system_prompt=payload.system_prompt,
    )
