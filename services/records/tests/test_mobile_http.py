"""Mobile HTTP input and credential boundaries; no network or production state."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.test_auth import FakeAvatars, configuration
from tests.test_http_api import event
from tests.test_mobile_login import NOW, REQUEST, MemoryStore
from tests.test_mobile_session import SessionStore

from shittim_records.mobile_http import MAX_INPUT_BYTES, MobileAuthHttpController
from shittim_records.mobile_login import MobileLoginService
from shittim_records.mobile_session import MobileSessionService

PREFIX = "/api/v1/auth/mobile/"


@pytest.fixture
def mobile():
    config = configuration()
    store = MemoryStore()

    def unexpected(**kwargs):
        raise AssertionError("invalid input reached a service")

    controller = MobileAuthHttpController(
        login=MobileLoginService(store=store, oauth=config.oauth, hmac_key=config.session_hmac_key),
        callback=cast(Any, SimpleNamespace(complete=unexpected)),
        exchange=cast(Any, SimpleNamespace(exchange=unexpected)),
        sessions=MobileSessionService(
            store=SessionStore(),
            avatars=FakeAvatars(),
            session_key=config.session_hmac_key,
            admin_requester_key=config.admin_requester_key,
            clock=lambda: NOW,
        ),
        allowed_origin=config.oauth.allowed_origin,
    )
    return controller, store


def start_event(**overrides):
    return {
        **event(f"POST {PREFIX}start", headers={"content-type": "application/json"}),
        "body": REQUEST.model_dump_json(by_alias=True),
        **overrides,
    }


def test_start_is_no_store_and_returns_no_browser_cookie(mobile):
    controller, store = mobile
    response = controller.handle(start_event(), now=NOW)
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert "cookies" not in response and len(store.states) == 1
    payload = json.loads(response["body"])
    assert set(payload) == {"schemaVersion", "transactionId", "authorizePath", "expiresAt"}
    assert REQUEST.state not in response["body"]


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({"body": "{"}, 400),
        ({"body": "[]"}, 400),
        ({"body": '{"state":"a","state":"b"}'}, 400),
        ({"body": '{"unknown":true}'}, 400),
        ({"body": REQUEST.model_dump_json()}, 400),  # snake_case is not the wire contract
        ({"body": " " * (MAX_INPUT_BYTES + 1)}, 400),
        ({"body": None}, 400),
        ({"isBase64Encoded": True}, 400),
        ({"headers": {"content-type": "text/plain"}}, 400),
        ({"headers": {"content-type": "application/json", "origin": "null"}}, 403),
        ({"headers": {"content-type": "application/json", "origin": "https://other.invalid"}}, 403),
        (
            {
                "headers": {
                    "content-type": "application/json",
                    "authorization": "Bearer " + "t" * 43,
                }
            },
            400,
        ),
        ({"headers": {"content-type": "application/json", "cookie": "ignored=1"}}, 400),
        ({"cookies": ["ignored=1"]}, 400),
        ({"rawQueryString": "extra=1"}, 400),
        ({"rawQueryString": "transaction=a&transaction=b"}, 400),
    ],
)
def test_invalid_start_never_creates_a_transaction(mobile, overrides, status):
    controller, store = mobile
    response = controller.handle(start_event(**overrides), now=NOW)
    assert response["statusCode"] == status
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert not store.states


@pytest.mark.parametrize("route", [f"GET {PREFIX}session", f"POST {PREFIX}logout"])
def test_session_routes_require_bearer_and_reject_query_body_and_cookie(mobile, route):
    controller, _store = mobile
    anonymous = controller.handle(event(route), now=NOW)
    assert anonymous["statusCode"] == 401
    assert anonymous["headers"]["WWW-Authenticate"] == "Bearer"
    for overrides in (
        {"rawQueryString": "access_token=" + "t" * 43},
        {"body": "{}"},
        {"isBase64Encoded": True},
        {"cookies": ["ignored=1"]},
        {"headers": {"Authorization": "Bearer " + "t" * 43, "authorization": "bad"}},
    ):
        response = controller.handle({**event(route), **overrides}, now=NOW)
        assert response["statusCode"] == 400
        assert response["headers"]["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("query", ["", "transaction=bad", "transaction=a&transaction=b", "extra=1"])
def test_authorize_rejects_invalid_or_extra_query_before_storage(mobile, query):
    controller, store = mobile
    response = controller.handle(event(f"GET {PREFIX}authorize", query=query), now=NOW)
    assert response["statusCode"] == 400
    assert not store.states
