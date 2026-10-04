"""Isolated composition root for native debate acceptance and progress."""

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any

import boto3
from shittim_chest.adapters.aws import SsmParameterReader
from shittim_chest.application.discord import DiscordBotSlot
from shittim_chest.config import parse_discord_runtime_config

from shittim_records.auth import AuthFailure
from shittim_records.http_api import JSON_HEADERS, _read_bearer_token, error_response, parse_request
from shittim_records.lambda_handlers import SDK_CONFIG
from shittim_records.mobile_auth_adapters import DynamoMobileAuthStore
from shittim_records.mobile_debate import MobileDebateService
from shittim_records.mobile_debate_adapters import (
    DiscordDebatePermissionGuard,
    DynamoMobileDebateRepository,
)
from shittim_records.mobile_debate_http import MobileDebateHttpController

LOGGER = logging.getLogger(__name__)
_CONTROLLER: MobileDebateHttpController | None = None


def handler(event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    global _CONTROLLER
    request = parse_request(event)
    # Reject anonymous/mixed credentials before secret/config loading (including
    # cold starts or broken deployment configuration).
    try:
        if event.get("cookies") or "cookie" in request.headers:
            return error_response(400, "REQUEST_INVALID", request.request_id)
        if _read_bearer_token(event.get("headers") or {}, has_cookies=False) is None:
            raise AuthFailure("session_required")
    except AuthFailure as error:
        if error.code != "session_required":
            return error_response(400, "REQUEST_INVALID", request.request_id)
        response = error_response(401, "AUTHENTICATION_REQUIRED", request.request_id)
        response["headers"] = {**JSON_HEADERS, "WWW-Authenticate": "Bearer"}
        return response
    try:
        if _CONTROLLER is None:
            _CONTROLLER = asyncio.run(_controller())
        return _CONTROLLER.handle(event)
    except Exception:
        # Do not log exceptions: SDK failures may embed tokens, IDs or question input.
        LOGGER.warning("mobile_debate_api_unavailable")
        return error_response(503, "DEBATE_REQUESTS_UNAVAILABLE", request.request_id)


async def _controller() -> MobileDebateHttpController:
    reader = SsmParameterReader(client=boto3.client("ssm", config=SDK_CONFIG))
    names = (
        os.environ["IDENTITY_HMAC_PARAMETER_NAME"],
        os.environ["SESSION_KEY_PARAMETER_NAME"],
        os.environ["RUNTIME_CONFIG_PARAMETER_NAME"],
        os.environ["MODERATOR_TOKEN_PARAMETER_NAME"],
    )
    values = await reader.get_parameters(names, with_decryption=True)
    identity_key, session_key = values[names[0]].encode(), values[names[1]].encode()
    if len(identity_key) < 32 or len(session_key) < 32:
        raise ValueError("mobile_debate_configuration_invalid")
    runtime, version = parse_discord_runtime_config(values[names[2]])
    if not names[2].endswith(f"/{version}"):
        raise ValueError("mobile_debate_configuration_invalid")
    channel_id = os.environ.get("MOBILE_DEBATE_CHANNEL_ID", "")
    enabled = os.environ.get("MOBILE_DEBATE_ENABLED", "false") == "true"
    if enabled and channel_id not in runtime.allowed_channel_ids:
        raise ValueError("mobile_debate_configuration_invalid")
    client = boto3.client("dynamodb", config=SDK_CONFIG)
    service = MobileDebateService(
        sessions=DynamoMobileAuthStore(client, os.environ["SESSION_TABLE_NAME"]),
        repository=DynamoMobileDebateRepository(
            client=client,
            source_table=os.environ["SOURCE_TABLE_NAME"],
            session_table=os.environ["SESSION_TABLE_NAME"],
            archive_table=os.environ["ARCHIVE_TABLE_NAME"],
            identity_key=identity_key,
        ),
        permissions=DiscordDebatePermissionGuard(
            bot_token=values[names[3]],
            guild_id=runtime.guild_id,
            channel_id=channel_id,
            application_id=runtime.application_id_for(DiscordBotSlot.MODERATOR),
        ),
        runtime=runtime,
        channel_id=channel_id,
        enabled=enabled,
        session_key=session_key,
        identity_key=identity_key,
    )
    return MobileDebateHttpController(service, allowed_origin=os.environ["PUBLIC_ORIGIN"])
