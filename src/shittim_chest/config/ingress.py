"""Fail-closed, token-free configuration for the Discord ingress Lambda."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from shittim_chest.application.discord import DiscordRuntimeConfig
from shittim_chest.config.models import (
    DEFAULT_AWS_REGION,
    StartupConfigurationError,
    parse_discord_runtime_config,
)

_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")
_CONFIG_VERSION = re.compile(r"v[0-9]{4}\Z")
_PUBLIC_KEY = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class IngressBootstrapSettings:
    """Immutable public routing data plus the one DynamoDB resource name."""

    aws_region: str
    table_name: str
    discord: DiscordRuntimeConfig
    config_version: str
    public_key_hex: str = field(repr=False)


def load_ingress_bootstrap_settings(
    environ: Mapping[str, str],
) -> IngressBootstrapSettings:
    """Validate deploy-time injected values without any request-time AWS read."""

    try:
        region = environ.get("AWS_REGION", DEFAULT_AWS_REGION).strip()
        if region != DEFAULT_AWS_REGION:
            raise ValueError
        table_name = _required(environ, "SHITTIM_DYNAMODB_TABLE")
        if _TABLE_NAME.fullmatch(table_name) is None:
            raise ValueError
        expected_version = _required(environ, "SHITTIM_RUNTIME_CONFIG_VERSION")
        if _CONFIG_VERSION.fullmatch(expected_version) is None:
            raise ValueError
        runtime, actual_version = parse_discord_runtime_config(
            _required(environ, "SHITTIM_RUNTIME_CONFIG_JSON")
        )
        if actual_version != expected_version:
            raise ValueError
        public_key = _required(environ, "SHITTIM_DISCORD_PUBLIC_KEY_HEX")
        if _PUBLIC_KEY.fullmatch(public_key) is None:
            raise ValueError
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None
    return IngressBootstrapSettings(
        aws_region=region,
        table_name=table_name,
        discord=runtime,
        config_version=actual_version,
        public_key_hex=public_key,
    )


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name].strip()
    if not value:
        raise ValueError
    return value


__all__ = (
    "IngressBootstrapSettings",
    "load_ingress_bootstrap_settings",
)
