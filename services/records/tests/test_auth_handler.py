"""Regression coverage for the dependencies opened by startup session checks."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from shittim_chest.adapters.dynamodb.codec import marshal_item
from tests.test_auth import configuration
from tests.test_http_api import event

from shittim_records import auth_handler
from shittim_records.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    csrf_hash,
    session_hash,
)


def _unexpected_dependency(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("Session check opened an unrelated dependency")


def test_anonymous_web_session_does_not_open_aws_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_handler, "_AUTH_CONTROLLER", None)
    monkeypatch.setattr(auth_handler, "environment", _unexpected_dependency)
    monkeypatch.setattr(auth_handler.boto3, "client", _unexpected_dependency)
    monkeypatch.setattr(auth_handler.boto3, "resource", _unexpected_dependency)
    response = auth_handler.auth_handler(
        event("GET /api/v1/session", query="contract=admin-v1"),
        object(),
    )

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert json.loads(response["body"]) == {
        "schemaVersion": 1,
        "authenticated": False,
        "user": None,
        "csrfToken": None,
        "isAdmin": False,
    }


def test_browser_session_without_avatar_keeps_discord_and_s3_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = configuration()
    now = datetime.now(UTC)
    clients: list[str] = []

    def get_item(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"] is True
        assert kwargs["Key"]["PK"]["S"] == f"SESSION#{session_hash(config.session_hmac_key, 'web')}"
        return {
            "Item": marshal_item(
                {
                    "schema_version": 1,
                    "record_type": "session",
                    "requester_key": config.admin_requester_key,
                    "display_name": "Fixture",
                    "csrf_hash": csrf_hash(config.session_hmac_key, "csrf"),
                    "guild_verified_at": now.isoformat(),
                    "expiresAt": int(now.timestamp()) + 300,
                }
            )
        }

    def client(service: str, **_kwargs: Any) -> object:
        assert service in {"dynamodb", "ssm"}
        clients.append(service)
        return SimpleNamespace(get_item=get_item) if service == "dynamodb" else object()

    monkeypatch.setattr(auth_handler, "_AUTH_CONTROLLER", None)
    monkeypatch.setattr(auth_handler, "environment", str.lower)
    monkeypatch.setattr(auth_handler.boto3, "client", client)
    monkeypatch.setattr(auth_handler.boto3, "resource", _unexpected_dependency)
    monkeypatch.setattr(auth_handler, "regional_s3_client", _unexpected_dependency)
    monkeypatch.setattr(auth_handler, "_discord_http_client", _unexpected_dependency)
    monkeypatch.setattr(
        auth_handler,
        "AuthConfigurationRepository",
        lambda *_args, **_kwargs: SimpleNamespace(load=lambda: config),
    )
    request = event(
        "GET /api/v1/session",
        query="contract=admin-v1",
        cookies=[f"{SESSION_COOKIE_NAME}=web", f"{CSRF_COOKIE_NAME}=csrf"],
    )

    for _ in range(2):
        response = auth_handler.auth_handler(request, object())
        assert response["statusCode"] == 200
        payload = json.loads(response["body"])
        assert payload["authenticated"] is True
        assert payload["isAdmin"] is True
        assert payload["user"]["avatar"]["kind"] == "placeholder"
    assert clients == ["dynamodb", "ssm"]
