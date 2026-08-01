"""Release-only Discord endpoint and Guild command bootstrap Lambda."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique

from shittim_chest.adapters.aws import SsmParameterReader, create_ssm_client
from shittim_chest.adapters.discord.bootstrap_api import (
    DiscordBootstrapApi,
    DiscordBootstrapError,
    DiscordBootstrapInspection,
    DiscordBootstrapService,
    create_discord_bootstrap_http_client,
)
from shittim_chest.adapters.discord.command_schema import command_schema_hash
from shittim_chest.application.ports import ParameterReadUnavailable
from shittim_chest.config import parse_discord_runtime_config
from shittim_chest.config.models import StartupConfigurationError

LOGGER = logging.getLogger(__name__)
_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")


@unique
class BootstrapOperation(StrEnum):
    """Explicit release operations; only two values can write Discord state."""

    INSPECT = "inspect"
    RECONCILE_ENDPOINT = "reconcile_endpoint"
    ASSESS_COMMAND = "assess_command"
    RECONCILE_COMMAND = "reconcile_command"
    VERIFY = "verify"


_ACKNOWLEDGEMENTS = {
    BootstrapOperation.RECONCILE_ENDPOINT: "reconcile-discord-endpoint",
    BootstrapOperation.RECONCILE_COMMAND: "reconcile-discord-command",
}


class DiscordBootstrapInvocationError(RuntimeError):
    """Content-free Lambda failure visible to the synchronous invoker."""

    def __init__(self) -> None:
        super().__init__("discord_bootstrap_invocation_failed")


@dataclass(frozen=True, slots=True)
class DiscordBootstrapRequest:
    operation: BootstrapOperation
    expected_commit_sha: str
    expected_command_schema_hash: str


@dataclass(frozen=True, slots=True)
class DiscordBootstrapSettings:
    aws_region: str
    expected_commit_sha: str
    expected_command_schema_hash: str
    expected_endpoint: str
    moderator_token_parameter: str
    moderator_public_key_parameter: str
    runtime_config_parameter: str


class DiscordBootstrapLambda:
    """Execute one strict bootstrap operation against an injected service."""

    __slots__ = ("_commit", "_hash", "_service")

    def __init__(
        self,
        *,
        service: DiscordBootstrapService,
        expected_commit_sha: str,
        expected_command_schema_hash: str,
    ) -> None:
        if _SHA40.fullmatch(expected_commit_sha) is None:
            raise ValueError("expected commit SHA is invalid")
        if _SHA64.fullmatch(expected_command_schema_hash) is None:
            raise ValueError("expected command schema hash is invalid")
        if expected_command_schema_hash != command_schema_hash():
            raise ValueError("expected command schema hash does not match local schema")
        self._service = service
        self._commit = expected_commit_sha
        self._hash = expected_command_schema_hash

    def handle(self, event: object) -> dict[str, object]:
        request = _parse_request(event)
        if (
            request.expected_commit_sha != self._commit
            or request.expected_command_schema_hash != self._hash
        ):
            raise ValueError("bootstrap request identity does not match deployment")
        endpoint_changed = False
        command_changed = False
        if request.operation is BootstrapOperation.INSPECT:
            inspection = self._service.inspect()
        elif request.operation is BootstrapOperation.RECONCILE_ENDPOINT:
            endpoint_changed = self._service.reconcile_endpoint()
            inspection = self._service.inspect()
        elif request.operation is BootstrapOperation.ASSESS_COMMAND:
            self._service.assess_command()
            inspection = self._service.inspect()
        elif request.operation is BootstrapOperation.RECONCILE_COMMAND:
            command_changed = self._service.reconcile_command()
            inspection = self._service.inspect()
        else:
            inspection = self._service.verify()
        changed = endpoint_changed or command_changed
        return _response(
            request=request,
            inspection=inspection,
            status="CHANGED" if changed else "PASS",
            endpoint_changed=endpoint_changed,
            command_changed=command_changed,
        )


_handler: DiscordBootstrapLambda | None = None


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """AWS entrypoint that logs only a stable category and request ID."""

    try:
        return _get_handler().handle(event)
    except Exception as error:
        LOGGER.error(
            "discord_bootstrap_failed category=%s request_id=%s",
            _failure_category(error),
            _request_id(context),
        )
        raise DiscordBootstrapInvocationError from None


def _get_handler() -> DiscordBootstrapLambda:
    global _handler
    if _handler is not None:
        return _handler
    settings = _load_settings(os.environ)
    reader = SsmParameterReader(client=create_ssm_client(region_name=settings.aws_region))
    names = (
        settings.moderator_token_parameter,
        settings.moderator_public_key_parameter,
        settings.runtime_config_parameter,
    )
    values = asyncio.run(reader.get_parameters(names, with_decryption=True))
    runtime, _ = parse_discord_runtime_config(values[settings.runtime_config_parameter])
    client = create_discord_bootstrap_http_client()
    service = DiscordBootstrapService(
        api=DiscordBootstrapApi(
            client=client,
            bot_token=values[settings.moderator_token_parameter],
        ),
        runtime=runtime,
        expected_public_key=values[settings.moderator_public_key_parameter],
        expected_endpoint=settings.expected_endpoint,
    )
    _handler = DiscordBootstrapLambda(
        service=service,
        expected_commit_sha=settings.expected_commit_sha,
        expected_command_schema_hash=settings.expected_command_schema_hash,
    )
    return _handler


def _load_settings(environ: Mapping[str, str]) -> DiscordBootstrapSettings:
    try:
        settings = DiscordBootstrapSettings(
            aws_region=_required(environ, "AWS_REGION"),
            expected_commit_sha=_required(environ, "SHITTIM_EXPECTED_COMMIT_SHA"),
            expected_command_schema_hash=_required(
                environ,
                "SHITTIM_EXPECTED_COMMAND_SCHEMA_HASH",
            ),
            expected_endpoint=_required(environ, "SHITTIM_EXPECTED_INTERACTIONS_ENDPOINT"),
            moderator_token_parameter=_parameter(
                environ,
                "SHITTIM_DISCORD_MODERATOR_TOKEN_PARAMETER",
            ),
            moderator_public_key_parameter=_parameter(
                environ,
                "SHITTIM_DISCORD_MODERATOR_PUBLIC_KEY_PARAMETER",
            ),
            runtime_config_parameter=_parameter(environ, "SHITTIM_RUNTIME_CONFIG_PARAMETER"),
        )
        if settings.aws_region != "ap-northeast-1":
            raise ValueError("unsupported region")
        if _SHA40.fullmatch(settings.expected_commit_sha) is None:
            raise ValueError("invalid commit")
        if _SHA64.fullmatch(settings.expected_command_schema_hash) is None:
            raise ValueError("invalid schema hash")
        if not settings.expected_endpoint.startswith("https://"):
            raise ValueError("invalid endpoint")
        return settings
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None


def _parse_request(event: object) -> DiscordBootstrapRequest:
    if not isinstance(event, Mapping) or set(event) != {
        "schema_version",
        "operation",
        "expected_commit_sha",
        "expected_command_schema_hash",
        "acknowledge_write",
    }:
        raise ValueError("bootstrap event shape is invalid")
    if event.get("schema_version") != 1:
        raise ValueError("bootstrap event schema is invalid")
    operation_value = event.get("operation")
    if not isinstance(operation_value, str):
        raise ValueError("bootstrap operation is invalid")
    try:
        operation = BootstrapOperation(operation_value)
    except ValueError:
        raise ValueError("bootstrap operation is invalid") from None
    expected_acknowledgement = _ACKNOWLEDGEMENTS.get(operation)
    if event.get("acknowledge_write") != expected_acknowledgement:
        raise ValueError("bootstrap acknowledgement is invalid")
    commit_sha = event.get("expected_commit_sha")
    schema_hash = event.get("expected_command_schema_hash")
    if not isinstance(commit_sha, str) or _SHA40.fullmatch(commit_sha) is None:
        raise ValueError("bootstrap commit identity is invalid")
    if not isinstance(schema_hash, str) or _SHA64.fullmatch(schema_hash) is None:
        raise ValueError("bootstrap schema identity is invalid")
    return DiscordBootstrapRequest(
        operation=operation,
        expected_commit_sha=commit_sha,
        expected_command_schema_hash=schema_hash,
    )


def _response(
    *,
    request: DiscordBootstrapRequest,
    inspection: DiscordBootstrapInspection,
    status: str,
    endpoint_changed: bool,
    command_changed: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": request.operation.value,
        "status": status,
        "expected_commit_sha": request.expected_commit_sha,
        "application_match": True,
        "guild_access": True,
        "channel_count": inspection.channel_count,
        "endpoint_match": inspection.endpoint_matches,
        "endpoint_sha256": inspection.endpoint_sha256,
        "global_command_count": inspection.global_command_count,
        "guild_command_count": inspection.guild_command_count,
        "command_schema_hash": command_schema_hash(),
        "command_state": inspection.command_state.value,
        "endpoint_changed": endpoint_changed,
        "command_changed": command_changed,
    }


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name]
    if not value or value != value.strip():
        raise ValueError("required setting is invalid")
    return value


def _parameter(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    if not value.startswith("/shittim-chest/production/"):
        raise ValueError("parameter path is invalid")
    return value


def _request_id(context: object) -> str:
    value = getattr(context, "aws_request_id", None)
    if not isinstance(value, str) or not value or len(value) > 128:
        return "unknown"
    return value


def _failure_category(error: Exception) -> str:
    if isinstance(error, DiscordBootstrapError):
        return error.category.value
    if isinstance(error, ParameterReadUnavailable):
        return "configuration_unavailable"
    if isinstance(error, StartupConfigurationError):
        return "configuration_invalid"
    if isinstance(error, ValueError):
        return "invalid_invocation"
    return "internal_error"


__all__ = (
    "BootstrapOperation",
    "DiscordBootstrapInvocationError",
    "DiscordBootstrapLambda",
    "lambda_handler",
)
