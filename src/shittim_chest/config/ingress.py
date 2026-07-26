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

_LAMBDA_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_RUNTIME_PARAMETER = re.compile(r"/shittim-chest/production/runtime/(?P<version>v[0-9]{4})\Z")
_PUBLIC_KEY_PARAMETER = "/shittim-chest/production/discord/moderator/public-key"


@dataclass(frozen=True, slots=True)
class IngressBootstrapSettings:
    """Resource identifiers only; no Discord token, API key, or private content."""

    aws_region: str
    table_name: str
    runtime_config_parameter: str
    discord_public_key_parameter: str
    status_publisher_function: str
    runtime_reconciler_function: str


@dataclass(frozen=True, slots=True)
class IngressRuntimeSettings:
    """Validated runtime allowlist plus its public interaction verification key."""

    discord: DiscordRuntimeConfig
    public_key_hex: str = field(repr=False)


def load_ingress_bootstrap_settings(
    environ: Mapping[str, str],
) -> IngressBootstrapSettings:
    """Validate Lambda environment identifiers without echoing invalid values."""

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
        status_function = _function_name(environ, "SHITTIM_STATUS_PUBLISHER_FUNCTION")
        reconciler_function = _function_name(environ, "SHITTIM_RUNTIME_RECONCILER_FUNCTION")
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None
    return IngressBootstrapSettings(
        aws_region=region,
        table_name=table_name,
        runtime_config_parameter=runtime_parameter,
        discord_public_key_parameter=public_key_parameter,
        status_publisher_function=status_function,
        runtime_reconciler_function=reconciler_function,
    )


async def load_ingress_runtime_settings(
    settings: IngressBootstrapSettings,
    reader: ParameterReader,
) -> IngressRuntimeSettings:
    """Resolve only the two versioned parameters required by signed ingress."""

    runtime_json, public_key = await asyncio.gather(
        reader.get_parameter(settings.runtime_config_parameter),
        reader.get_parameter(settings.discord_public_key_parameter),
    )
    runtime, config_version = parse_discord_runtime_config(runtime_json)
    match = _RUNTIME_PARAMETER.fullmatch(settings.runtime_config_parameter)
    if match is None or match.group("version") != config_version:
        raise StartupConfigurationError
    return IngressRuntimeSettings(discord=runtime, public_key_hex=public_key)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name].strip()
    if not value:
        raise ValueError
    return value


def _function_name(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if _LAMBDA_FUNCTION_NAME.fullmatch(value) is None:
        raise ValueError
    return value


__all__ = (
    "IngressBootstrapSettings",
    "IngressRuntimeSettings",
    "load_ingress_bootstrap_settings",
    "load_ingress_runtime_settings",
)
