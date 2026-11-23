"""Exercise the deployed read entry point with synthetic AWS clients."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tests.test_http_api import NOW, SESSION_KEY, FakeSessionStore, event, session

from shittim_records import momotalk_adapters, read_handler
from shittim_records.auth import SESSION_COOKIE_NAME


def test_read_handler_builds_momotalk_only_for_a_valid_authenticated_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = object()
    clients: list[str] = []
    asset_clients: list[object] = []
    dynamodb = SimpleNamespace(query=lambda **_: {"Items": []})
    ssm = SimpleNamespace(
        get_parameters=lambda **_: {
            "Parameters": [{"Name": "session_key_parameter_name", "Value": SESSION_KEY.decode()}]
        }
    )

    def client(service: str, **_: Any) -> object:
        clients.append(service)
        return {"dynamodb": dynamodb, "ssm": ssm}[service]

    def assets(s3_client: object, _bucket: str) -> object:
        asset_clients.append(s3_client)
        return object()

    monkeypatch.setattr(read_handler, "_READ_CONTROLLER", None)
    monkeypatch.setattr(read_handler, "environment", str.lower)
    monkeypatch.setattr(read_handler, "regional_s3_client", lambda: s3)
    monkeypatch.setattr(read_handler, "boto3", SimpleNamespace(client=client))
    monkeypatch.setattr(read_handler, "DynamoAuthStore", lambda *_: FakeSessionStore(session()))
    monkeypatch.setattr(read_handler, "datetime", SimpleNamespace(now=lambda _: NOW))
    monkeypatch.setattr(momotalk_adapters, "MomotalkAssets", assets)
    cookies = [f"{SESSION_COOKIE_NAME}=session-token"]

    def status(route: str, **kwargs: Any) -> int:
        return read_handler.read_handler(event(route, **kwargs), object())["statusCode"]

    assert status("GET /api/v1/records", cookies=cookies) == 200
    assert status("GET /api/v1/momotalk/weeks") == 401
    assert status("GET /api/v1/momotalk/weeks", query="extra=x", cookies=cookies) == 400
    assert not asset_clients

    assert status("GET /api/v1/momotalk/weeks", cookies=cookies) == 200
    assert status("GET /api/v1/momotalk/weeks", cookies=cookies) == 200
    assert asset_clients == [s3]
    assert clients == ["dynamodb", "ssm"]
