"""Fail-closed resource identifiers for the public status Lambda."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from shittim_chest.application.discord import DiscordRuntimeConfig
from shittim_chest.application.ports import ParameterReader
from shittim_chest.config.models import (
    DEFAULT_AWS_REGION,
    StartupConfigurationError,
    parse_discord_runtime_config,
)

MODERATOR_TOKEN_PARAMETER = "/shittim-chest/production/discord/moderator/token"  # noqa: S105 - path, not credential.

_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_RUNTIME_PARAMETER = re.compile(r"/shittim-chest/production/runtime/(?P<version>v[0-9]{4})\Z")


@dataclass(frozen=True, slots=True)
class StatusPublisherSettings:
    """Non-secret Lambda settings with one fixed moderator-token path."""

    aws_region: str
    table_name: str
    runtime_config_parameter: str
    moderator_token_parameter: str


def load_status_publisher_settings(
    environ: Mapping[str, str],
) -> StatusPublisherSettings:
    """Reject missing, padded, or unexpected resource identifiers."""

    try:
        region = environ.get("AWS_REGION", DEFAULT_AWS_REGION)
        if region != DEFAULT_AWS_REGION:
            raise ValueError
        table_name = environ["SHITTIM_DYNAMODB_TABLE"]
        if table_name != table_name.strip() or _TABLE_NAME.fullmatch(table_name) is None:
            raise ValueError
        runtime_parameter = environ["SHITTIM_RUNTIME_CONFIG_PARAMETER"]
        if _RUNTIME_PARAMETER.fullmatch(runtime_parameter) is None:
            raise ValueError
        token_parameter = environ["SHITTIM_MODERATOR_TOKEN_PARAMETER"]
        if token_parameter != MODERATOR_TOKEN_PARAMETER:
            raise ValueError
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None
    return StatusPublisherSettings(
        aws_region=region,
        table_name=table_name,
        runtime_config_parameter=runtime_parameter,
        moderator_token_parameter=token_parameter,
    )


async def load_status_runtime_config(
    settings: StatusPublisherSettings,
    reader: ParameterReader,
) -> DiscordRuntimeConfig:
    """Read and version-check the shared token-free Discord boundary."""

    raw = await reader.get_parameter(settings.runtime_config_parameter)
    runtime, config_version = parse_discord_runtime_config(raw)
    match = _RUNTIME_PARAMETER.fullmatch(settings.runtime_config_parameter)
    if match is None or match.group("version") != config_version:
        raise StartupConfigurationError
    return runtime


__all__ = (
    "MODERATOR_TOKEN_PARAMETER",
    "StatusPublisherSettings",
    "load_status_publisher_settings",
    "load_status_runtime_config",
)
