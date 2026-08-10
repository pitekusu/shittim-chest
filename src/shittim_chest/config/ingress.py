"""Fail-closed, token-free configuration for the Discord ingress Lambda."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from shittim_chest.application.discord import DiscordRuntimeConfig
from shittim_chest.application.ports import ParameterReader
from shittim_chest.config.models import (
    DEFAULT_AWS_REGION,
    StartupConfigurationError,
    parse_discord_runtime_config,
)

_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_RUNTIME_PARAMETER = re.compile(r"/shittim-chest/production/runtime/(?P<version>v[0-9]{4})\Z")
_PUBLIC_KEY_PARAMETER = "/shittim-chest/production/discord/moderator/public-key"


@dataclass(frozen=True, slots=True)
class IngressBootstrapSettings:
    """Resource identifiers needed before the SnapStart checkpoint."""

    aws_region: str
    table_name: str
    runtime_config_parameter: str
    discord_public_key_parameter: str


@dataclass(frozen=True, slots=True)
class IngressRuntimeSettings:
    """Validated token-free routing data captured in the encrypted snapshot."""

    discord: DiscordRuntimeConfig
    public_key_hex: str = field(repr=False)


def load_ingress_bootstrap_settings(
    environ: Mapping[str, str],
) -> IngressBootstrapSettings:
    """Validate immutable parameter names without echoing invalid values."""

    try:
        region = environ.get("AWS_REGION", DEFAULT_AWS_REGION).strip()
        if region != DEFAULT_AWS_REGION:
            raise ValueError
        table_name = _required(environ, "SHITTIM_DYNAMODB_TABLE")
        if _TABLE_NAME.fullmatch(table_name) is None:
            raise ValueError
        runtime_parameter = _required(environ, "SHITTIM_RUNTIME_CONFIG_PARAMETER")
        if _RUNTIME_PARAMETER.fullmatch(runtime_parameter) is None:
            raise ValueError
        public_key_parameter = _required(
            environ,
            "SHITTIM_DISCORD_PUBLIC_KEY_PARAMETER",
        )
        if public_key_parameter != _PUBLIC_KEY_PARAMETER:
            raise ValueError
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None
    return IngressBootstrapSettings(
        aws_region=region,
        table_name=table_name,
        runtime_config_parameter=runtime_parameter,
        discord_public_key_parameter=public_key_parameter,
    )


async def load_ingress_runtime_settings(
    settings: IngressBootstrapSettings,
    reader: ParameterReader,
) -> IngressRuntimeSettings:
    """Resolve the two SecureStrings once during Lambda initialization."""

    runtime_json, public_key = await asyncio.gather(
        reader.get_parameter(settings.runtime_config_parameter),
        reader.get_parameter(settings.discord_public_key_parameter),
    )
    runtime, config_version = parse_discord_runtime_config(runtime_json)
    match = _RUNTIME_PARAMETER.fullmatch(settings.runtime_config_parameter)
    if match is None or match.group("version") != config_version:
        raise StartupConfigurationError
    if re.fullmatch(r"[0-9a-f]{64}", public_key) is None:
        raise StartupConfigurationError
    return IngressRuntimeSettings(discord=runtime, public_key_hex=public_key)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name].strip()
    if not value:
        raise ValueError
    return value


__all__ = (
    "IngressBootstrapSettings",
    "IngressRuntimeSettings",
    "load_ingress_bootstrap_settings",
    "load_ingress_runtime_settings",
)
