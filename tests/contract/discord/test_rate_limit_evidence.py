"""Contracts for content-free Discord rate-limit evidence."""

from __future__ import annotations

import hashlib
import json
import logging
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from shittim_chest.adapters.discord.rate_limit_evidence import (
    _record_discord_py_response,
    discord_rate_limit_operation,
    record_status_rate_limit_response,
)
from shittim_chest.application.ingress_drain import DiscordIngressOperation


def _payload(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    return cast(dict[str, object], json.loads(caplog.records[-1].message))


@pytest.mark.asyncio
async def test_discord_py_trace_records_all_requested_headers_without_raw_bucket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bucket = "opaque-provider-bucket"
    response = SimpleNamespace(
        status=429,
        headers={
            "X-RateLimit-Scope": "shared",
            "X-RateLimit-Bucket": bucket,
            "X-RateLimit-Limit": "1",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "300.125",
        },
    )

    with (
        caplog.at_level(logging.INFO, logger="shittim_chest"),
        discord_rate_limit_operation(DiscordIngressOperation.THREAD_CREATE),
    ):
        await _record_discord_py_response(
            cast(Any, None),
            cast(Any, None),
            cast(Any, SimpleNamespace(response=response)),
        )

    evidence = _payload(caplog)
    assert evidence == {
        "discord_operation": "thread_create",
        "event": "discord_rate_limit_evidence",
        "http_status": 429,
        "invalid_headers": [],
        "severity": "INFO",
        "x_rate_limit_bucket_sha256": hashlib.sha256(bucket.encode()).hexdigest(),
        "x_rate_limit_limit": 1,
        "x_rate_limit_remaining": 0,
        "x_rate_limit_reset_after_seconds": 300.125,
        "x_rate_limit_scope": "shared",
    }
    assert bucket not in caplog.text


@pytest.mark.asyncio
async def test_successful_response_records_bucket_reset_and_missing_scope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = SimpleNamespace(
        status=201,
        headers={
            "X-RateLimit-Bucket": "first-success-bucket",
            "X-RateLimit-Limit": "1",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset-After": "299.75",
        },
    )

    with (
        caplog.at_level(logging.INFO, logger="shittim_chest"),
        discord_rate_limit_operation(DiscordIngressOperation.THREAD_CREATE),
    ):
        await _record_discord_py_response(
            cast(Any, None),
            cast(Any, None),
            cast(Any, SimpleNamespace(response=response)),
        )

    evidence = _payload(caplog)
    assert evidence["http_status"] == 201
    assert evidence["x_rate_limit_scope"] is None
    assert evidence["x_rate_limit_limit"] == 1
    assert evidence["x_rate_limit_remaining"] == 0
    assert evidence["x_rate_limit_reset_after_seconds"] == 299.75


def test_status_rest_evidence_classifies_route_without_url_or_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request(
        "POST",
        "https://discord.com/api/v10/channels/123456789/messages",
    )
    response = httpx.Response(
        200,
        request=request,
        headers={
            "X-RateLimit-Bucket": "status-bucket",
            "X-RateLimit-Limit": "5",
            "X-RateLimit-Remaining": "4",
            "X-RateLimit-Reset-After": "1.25",
        },
    )

    with caplog.at_level(logging.INFO, logger="shittim_chest"):
        record_status_rate_limit_response(response)

    evidence = _payload(caplog)
    assert evidence["discord_operation"] == "status_message_create"
    assert evidence["x_rate_limit_limit"] == 5
    assert evidence["x_rate_limit_remaining"] == 4
    assert "123456789" not in caplog.text
    assert "discord.com" not in caplog.text


def test_invalid_provider_headers_are_classified_without_raw_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request("GET", "https://discord.com/api/v10/channels/123456789")
    response = httpx.Response(
        429,
        request=request,
        headers={
            "X-RateLimit-Scope": "private-scope",
            "X-RateLimit-Bucket": "",
            "X-RateLimit-Limit": "many",
            "X-RateLimit-Remaining": "-1",
            "X-RateLimit-Reset-After": "nan",
        },
    )

    with caplog.at_level(logging.INFO, logger="shittim_chest"):
        record_status_rate_limit_response(response)

    evidence = _payload(caplog)
    assert evidence["invalid_headers"] == [
        "scope",
        "bucket",
        "limit",
        "remaining",
        "reset_after",
    ]
    assert evidence["x_rate_limit_scope"] is None
    assert evidence["x_rate_limit_bucket_sha256"] is None
    assert "private-scope" not in caplog.text
    assert "many" not in caplog.text


@pytest.mark.asyncio
async def test_discord_py_trace_ignores_requests_outside_an_ingress_operation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = SimpleNamespace(
        status=200,
        headers={"X-RateLimit-Limit": "1"},
    )

    with caplog.at_level(logging.INFO, logger="shittim_chest"):
        await _record_discord_py_response(
            cast(Any, None),
            cast(Any, None),
            cast(Any, SimpleNamespace(response=response)),
        )

    assert caplog.records == []
