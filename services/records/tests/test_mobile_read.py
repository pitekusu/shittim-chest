"""Bearer reads share the existing public response, never the Web session namespace."""

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from tests.test_auth import FakeStore
from tests.test_contracts import _record_detail_payload
from tests.test_http_api import NOW, SESSION_KEY, FakeRecords, FakeSessionStore, event, session
from tests.test_mobile_auth_adapters import mobile_session
from tests.test_mobile_session import SessionStore

from shittim_records.auth import SESSION_COOKIE_NAME, AuthFailure, _digest
from shittim_records.contracts import RecordDetailResponse, RecordSyncIndexResponse
from shittim_records.http_api import ReadHttpController

TOKEN = "t" * 43
BEARER = {"Authorization": f"Bearer {TOKEN}"}
COOKIE = [f"{SESSION_COOKIE_NAME}=session-token"]
DIGEST = _digest(SESSION_KEY, "mobile-session", TOKEN)


class Records(FakeRecords):
    def get_sync_index(self, **kwargs: Any) -> RecordSyncIndexResponse:
        return RecordSyncIndexResponse(schema_version=1, items=())

    def get_record(self, **kwargs: Any) -> Any:
        assert kwargs["record_id"] == "r" * 43
        return RecordDetailResponse.model_validate(_record_detail_payload())


@pytest.fixture
def read():
    store = SessionStore()
    store.sessions[DIGEST] = mobile_session(now_epoch=int(NOW.timestamp()))
    records = Records()
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, records),
        mobile_store=store,
        clock=lambda: NOW,
    )
    return controller, store, records


@pytest.mark.parametrize(
    "route",
    ["GET /api/v1/records", "GET /api/v1/records/{recordId}", "GET /api/v1/records/sync-index"],
)
def test_cookie_and_bearer_return_the_same_public_records_without_cookies(read, route):
    controller, store, _records = read
    path = {"recordId": "r" * 43}
    cookie = controller.handle(event(route, cookies=COOKIE, path=path), now=NOW)
    bearer = controller.handle(event(route, headers=BEARER, path=path), now=NOW)
    assert bearer == cookie and bearer["statusCode"] == 200
    assert bearer["headers"]["Cache-Control"] == "private, no-store"
    assert "cookies" not in bearer
    assert TOKEN not in bearer["body"] and DIGEST not in bearer["body"]
    assert store.reads == [DIGEST]
    assert len(store.sessions) == 1 and not store.deleted


def test_sync_index_requires_auth_and_rejects_unknown_or_duplicate_parameters(read):
    controller, _store, _records = read
    route = "GET /api/v1/records/sync-index"
    assert controller.handle(event(route), now=NOW)["statusCode"] == 401
    for query in ("limit=100", "cursor=a&cursor=b"):
        assert (
            controller.handle(event(route, query=query, headers=BEARER), now=NOW)["statusCode"]
            == 400
        )


@pytest.mark.parametrize(
    "headers,cookies,status",
    [
        ({"Authorization": ""}, [], 401),
        ({"Authorization": f"Basic {TOKEN}"}, [], 401),
        ({"Authorization": f"Bearer {TOKEN},Bearer {TOKEN}"}, [], 401),
        ({"Authorization": f"Bearer {TOKEN}\n"}, [], 401),
        ({**BEARER, "authorization": f"Bearer {TOKEN}"}, [], 400),
        (BEARER, COOKIE, 400),
        (BEARER, [f"{SESSION_COOKIE_NAME}="], 400),
        ({"Authorization": "invalid"}, COOKIE, 400),
        ({**BEARER, "Cookie": COOKIE[0]}, [], 400),
    ],
)
def test_mixed_or_malformed_credentials_never_fall_back_to_cookie(read, headers, cookies, status):
    controller, store, records = read
    response = controller.handle(
        event("GET /api/v1/records", headers=headers, cookies=cookies), now=NOW
    )
    assert response["statusCode"] == status
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert TOKEN not in response["body"]
    assert not store.reads and records.list_query is None
    if status == 401:
        assert response["headers"]["WWW-Authenticate"] == "Bearer"


def test_bearer_scheme_is_case_insensitive_and_query_validation_is_unchanged(read):
    controller, _store, records = read
    headers = {"authorization": f"bearer {TOKEN}"}
    response = controller.handle(
        event("GET /api/v1/records", headers=headers, query="limit=5&sort=oldest"), now=NOW
    )
    assert response["statusCode"] == 200
    assert records.list_query.limit == 5 and records.list_query.sort == "oldest"
    invalid = controller.handle(
        event("GET /api/v1/records", headers=headers, query="limit=5&limit=10"), now=NOW
    )
    assert invalid["statusCode"] == 400


@pytest.mark.parametrize("state", ["expired", "deleted", "corrupt"])
def test_mobile_revocation_expiry_and_corruption_never_read_records(read, state, monkeypatch):
    controller, store, records = read
    if state == "expired":
        controller._clock = lambda: datetime.fromtimestamp(store.sessions[DIGEST].expires_at, UTC)
    elif state == "deleted":
        store.delete_session(session_hash=DIGEST)
    else:

        def corrupt(**kwargs):
            raise AuthFailure("mobile_session_record_invalid")

        monkeypatch.setattr(store, "get_session", corrupt)
    response = controller.handle(event("GET /api/v1/records", headers=BEARER), now=NOW)
    assert response["statusCode"] == (503 if state == "corrupt" else 401)
    assert records.list_query is None
    assert json.loads(response["body"])["error"]["code"] == (
        "RECORDS_UNAVAILABLE" if state == "corrupt" else "AUTHENTICATION_REQUIRED"
    )


@pytest.mark.parametrize(
    "route",
    [
        "GET /api/v1/insights/rankings",
        "GET /api/v1/momotalk/weeks",
        "GET /api/v1/admin/status",
        "POST /api/v1/admin/prompts/apply",
        "POST /api/v1/records",
    ],
)
def test_mobile_token_does_not_open_additional_routes(read, route):
    controller, store, _records = read
    assert controller.handle(event(route, headers=BEARER), now=NOW)["statusCode"] == 401
    assert not store.reads


def test_token_in_url_body_or_web_cookie_is_not_a_bearer_credential(read):
    controller, store, _records = read
    for request in (
        event("GET /api/v1/records", query=f"access_token={TOKEN}"),
        {**event("GET /api/v1/records"), "body": json.dumps({"accessToken": TOKEN})},
    ):
        assert controller.handle(request, now=NOW)["statusCode"] == 401
    # A token placed in a Web cookie must only look in the separate Web namespace.
    web_store = FakeStore()
    web_store.sessions[DIGEST] = session()
    controller._store = web_store
    response = controller.handle(
        event("GET /api/v1/records", cookies=[f"{SESSION_COOKIE_NAME}={TOKEN}"]), now=NOW
    )
    assert response["statusCode"] == 401
    assert not store.reads
