"""Content-free Discord rate-limit response evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import aiohttp
import httpx

from shittim_chest.application.ingress_drain import DiscordIngressOperation

_LOGGER = logging.getLogger("shittim_chest")
_CURRENT_OPERATION: ContextVar[str | None] = ContextVar(
    "discord_rate_limit_operation",
    default=None,
)
_ALLOWED_SCOPES = frozenset({"user", "global", "shared"})
_MAX_BUCKET_LENGTH = 512
_SNOWFLAKE_PATH = r"[0-9]{1,20}"
_STATUS_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"GET /oauth2/applications/@me\Z"), "status_application_fetch"),
    (re.compile(r"GET /users/@me\Z"), "status_bot_fetch"),
    (
        re.compile(rf"GET /guilds/{_SNOWFLAKE_PATH}/members/{_SNOWFLAKE_PATH}\Z"),
        "status_member_fetch",
    ),
    (re.compile(rf"GET /guilds/{_SNOWFLAKE_PATH}/roles\Z"), "status_roles_fetch"),
    (
        re.compile(rf"GET /channels/{_SNOWFLAKE_PATH}/messages/{_SNOWFLAKE_PATH}\Z"),
        "status_message_fetch",
    ),
    (
        re.compile(rf"GET /channels/{_SNOWFLAKE_PATH}/messages\Z"),
        "status_message_history",
    ),
    (
        re.compile(rf"POST /channels/{_SNOWFLAKE_PATH}/messages\Z"),
        "status_message_create",
    ),
    (
        re.compile(rf"PATCH /channels/{_SNOWFLAKE_PATH}/messages/{_SNOWFLAKE_PATH}\Z"),
        "status_message_edit",
    ),
    (re.compile(rf"GET /channels/{_SNOWFLAKE_PATH}\Z"), "status_channel_fetch"),
    (re.compile(rf"PATCH /channels/{_SNOWFLAKE_PATH}\Z"), "status_thread_unarchive"),
)


def build_discord_rate_limit_trace() -> aiohttp.TraceConfig:
    """Build one trace config for a single discord.py HTTP session."""

    trace = aiohttp.TraceConfig()
    trace.on_request_end.append(_record_discord_py_response)
    return trace


@contextmanager
def discord_rate_limit_operation(operation: DiscordIngressOperation) -> Iterator[None]:
    """Associate one allowlisted operation with its discord.py response."""

    token = _CURRENT_OPERATION.set(operation.value)
    try:
        yield
    finally:
        _CURRENT_OPERATION.reset(token)


def record_status_rate_limit_response(response: httpx.Response) -> None:
    """Record safe rate-limit headers from the moderator status REST adapter."""

    operation = _classify_status_operation(response.request.method, response.request.url.path)
    _log_rate_limit_evidence(
        operation=operation,
        status=response.status_code,
        headers=response.headers,
    )


async def _record_discord_py_response(
    _session: aiohttp.ClientSession,
    _trace_context: Any,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    operation = _CURRENT_OPERATION.get()
    if operation is None:
        return
    _log_rate_limit_evidence(
        operation=operation,
        status=params.response.status,
        headers=params.response.headers,
    )


def _log_rate_limit_evidence(
    *,
    operation: str,
    status: int,
    headers: Mapping[str, str],
) -> None:
    raw_scope = headers.get("X-RateLimit-Scope")
    raw_bucket = headers.get("X-RateLimit-Bucket")
    raw_limit = headers.get("X-RateLimit-Limit")
    raw_remaining = headers.get("X-RateLimit-Remaining")
    raw_reset_after = headers.get("X-RateLimit-Reset-After")
    if status != 429 and all(
        value is None
        for value in (raw_scope, raw_bucket, raw_limit, raw_remaining, raw_reset_after)
    ):
        return

    invalid: list[str] = []
    scope = _scope(raw_scope, invalid)
    bucket_sha256 = _bucket_sha256(raw_bucket, invalid)
    limit = _integer(raw_limit, "limit", invalid)
    remaining = _integer(raw_remaining, "remaining", invalid)
    reset_after = _seconds(raw_reset_after, invalid)
    payload = {
        "discord_operation": operation,
        "event": "discord_rate_limit_evidence",
        "http_status": status,
        "invalid_headers": invalid,
        "severity": "INFO",
        "x_rate_limit_bucket_sha256": bucket_sha256,
        "x_rate_limit_limit": limit,
        "x_rate_limit_remaining": remaining,
        "x_rate_limit_reset_after_seconds": reset_after,
        "x_rate_limit_scope": scope,
    }
    _LOGGER.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _classify_status_operation(method: str, path: str) -> str:
    if path.startswith("/api/v10/"):
        path = path.removeprefix("/api/v10")
    candidate = f"{method.upper()} {path}"
    for pattern, operation in _STATUS_ROUTES:
        if pattern.fullmatch(candidate):
            return operation
    return "status_unknown"


def _scope(value: str | None, invalid: list[str]) -> str | None:
    if value is None:
        return None
    if value in _ALLOWED_SCOPES:
        return value
    invalid.append("scope")
    return None


def _bucket_sha256(value: str | None, invalid: list[str]) -> str | None:
    if value is None:
        return None
    if not value or len(value) > _MAX_BUCKET_LENGTH:
        invalid.append("bucket")
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _integer(value: str | None, label: str, invalid: list[str]) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        invalid.append(label)
        return None
    if parsed < 0:
        invalid.append(label)
        return None
    return parsed


def _seconds(value: str | None, invalid: list[str]) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        invalid.append("reset_after")
        return None
    if not math.isfinite(parsed) or parsed < 0:
        invalid.append("reset_after")
        return None
    return parsed
