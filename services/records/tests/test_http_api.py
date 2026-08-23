"""API Gateway HTTP API v2 boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from shittim_records.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthFailure,
    OAuthCompletion,
    OAuthStart,
    SessionRecord,
    csrf_hash,
)
from shittim_records.auth import (
    session_hash as hash_session,
)
from shittim_records.contracts import (
    CostBreakdown,
    CostConversion,
    CostsResponse,
    PlaceholderAvatarRef,
    RankingEntry,
    RankingsResponse,
    RecordListResponse,
)
from shittim_records.http_api import AuthHttpController, ReadHttpController
from shittim_records.read_api import ListQuery

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
SESSION_KEY = b"s" * 32


def event(
    route: str,
    *,
    query: str = "",
    cookies: list[str] | None = None,
    headers: dict[str, str] | None = None,
    path: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "routeKey": route,
        "requestContext": {"requestId": "opaque-request"},
        "rawQueryString": query,
        "cookies": cookies or [],
        "headers": headers or {},
        "pathParameters": path or {},
    }


class FakeAuthService:
    allowed_origin = "https://records.example.invalid"
    session_hmac_key = SESSION_KEY

    def __init__(self, session: SessionRecord | None = None) -> None:
        self.session = session

    def begin(self, *, return_to: str | None, now: datetime) -> OAuthStart:
        del now
        assert return_to == "/"
        return OAuthStart("https://discord.example.invalid", "oauth-cookie")

    def complete(self, **kwargs: Any) -> OAuthCompletion:
        assert kwargs["code"] == "code"
        assert kwargs["state"] == "state"
        return OAuthCompletion(
            location=f"{self.allowed_origin}/",
            session_cookie="session-cookie",
            csrf_cookie="csrf-cookie",
            clear_oauth_cookie="clear-oauth-cookie",
        )

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord | None:
        del now
        return self.session if raw_session == "session-token" else None

    def avatar_url(self, *, asset_key: str) -> str:
        return f"https://media.example.invalid/{asset_key}"

    def logout(self, **kwargs: Any) -> tuple[str, str]:
        assert kwargs["origin"] == self.allowed_origin
        return "session-clear", "csrf-clear"


class FakeSessionStore:
    def __init__(self, session: SessionRecord | None) -> None:
        self.session = session

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        assert session_hash == hash_session(SESSION_KEY, "session-token")
        return self.session


class FakeRecords:
    def __init__(self) -> None:
        self.list_query: ListQuery | None = None

    def list_records(self, **_kwargs: Any) -> RecordListResponse:
        self.list_query = _kwargs["query"]
        return RecordListResponse(schema_version=1, items=())

    def get_record(self, **_kwargs: Any) -> None:
        raise AssertionError("detail should not be called")

    def get_rankings(self, **_kwargs: Any) -> RankingsResponse:
        return RankingsResponse(
            schema_version=1,
            wins=(
                RankingEntry(
                    rank=1,
                    display_name="Arona",
                    avatar=PlaceholderAvatarRef(
                        kind="placeholder",
                        alt="Arona avatar",
                        fallback_variant="cyan",
                    ),
                    count=3,
                ),
            ),
            requests=(),
            generated_at=NOW,
        )

    def get_costs(self, **kwargs: Any) -> CostsResponse:
        return CostsResponse(
            schema_version=1,
            period=kwargs["period"],
            time_zone="Asia/Tokyo",
            start_date=NOW.date(),
            end_date=NOW.date(),
            currency="JPY",
            total="1.000000",
            breakdown=CostBreakdown(
                fargate="1.000000",
                lambda_="0.000000",
                openai="0.000000",
                other_aws="0.000000",
            ),
            conversion=CostConversion(
                source="frankfurter-v2",
                method="daily-reference-rate",
                base_currency="USD",
                updated_at=NOW,
            ),
            updated_at=NOW,
            status="partial",
        )


def session(*, avatar: bool = False, expired: bool = False) -> SessionRecord:
    raw_csrf = "csrf-token"
    return SessionRecord(
        requester_key="requester",
        display_name="Requester",
        avatar_asset_key="requesters/opaque/avatar.webp" if avatar else None,
        csrf_hash=csrf_hash(SESSION_KEY, raw_csrf),
        guild_verified_at=NOW.isoformat(),
        expires_at=int(
            (NOW + (-timedelta(seconds=1) if expired else timedelta(hours=1))).timestamp()
        ),
    )


def test_anonymous_session_is_always_200_no_store_without_cookie() -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService()))

    response = controller.handle(event("GET /api/v1/session"), now=NOW)

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert "cookies" not in response
    assert json.loads(response["body"]) == {
        "schemaVersion": 1,
        "authenticated": False,
        "user": None,
        "csrfToken": None,
    }


def test_authenticated_session_returns_csrf_and_short_lived_avatar_url() -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService(session(avatar=True))))

    response = controller.handle(
        event(
            "GET /api/v1/session",
            cookies=[
                f"{SESSION_COOKIE_NAME}=session-token",
                f"{CSRF_COOKIE_NAME}=csrf-token",
            ],
        ),
        now=NOW,
    )

    payload = json.loads(response["body"])
    assert payload["authenticated"] is True
    assert payload["csrfToken"] == "csrf-token"
    assert payload["user"]["avatar"]["kind"] == "image"
    assert "requesterKey" not in repr(payload)


def test_callback_requires_exactly_one_code_and_state() -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService()))

    response = controller.handle(
        event(
            "GET /api/v1/auth/discord/callback",
            query="code=one&code=two&state=state",
        ),
        now=NOW,
    )

    assert response["statusCode"] == 302
    assert response["headers"]["Location"].endswith("/login?error=oauth_request_invalid")
    assert response["cookies"][0].startswith("__Host-shittim-records-oauth=;")


def test_logout_returns_two_clearing_cookies() -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService(session())))

    response = controller.handle(
        event(
            "POST /api/v1/logout",
            cookies=[
                f"{SESSION_COOKIE_NAME}=session-token",
                f"{CSRF_COOKIE_NAME}=csrf-token",
            ],
            headers={"origin": "https://records.example.invalid", "x-csrf-token": "csrf-token"},
        ),
        now=NOW,
    )

    assert response["statusCode"] == 204
    assert response["cookies"] == ["session-clear", "csrf-clear"]


def test_protected_records_reject_missing_or_expired_session() -> None:
    for stored in (None, session(expired=True)):
        controller = ReadHttpController(
            store=cast(Any, FakeSessionStore(stored)),
            session_key=SESSION_KEY,
            records=cast(Any, FakeRecords()),
        )
        cookies = [] if stored is None else [f"{SESSION_COOKIE_NAME}=session-token"]

        response = controller.handle(
            event("GET /api/v1/records", cookies=cookies),
            now=NOW,
        )

        assert response["statusCode"] == 401
        assert json.loads(response["body"])["error"]["code"] == "AUTHENTICATION_REQUIRED"


def test_protected_records_returns_only_public_contract() -> None:
    records = FakeRecords()
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, records),
    )

    response = controller.handle(
        event(
            "GET /api/v1/records",
            query="limit=12&sort=oldest",
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "schemaVersion": 1,
        "items": [],
        "nextCursor": None,
    }
    assert records.list_query is not None
    assert records.list_query.sort == "oldest"


def test_records_defaults_to_newest_and_rejects_unknown_sort() -> None:
    records = FakeRecords()
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, records),
    )
    cookies = [f"{SESSION_COOKIE_NAME}=session-token"]

    newest = controller.handle(event("GET /api/v1/records", cookies=cookies), now=NOW)
    invalid = controller.handle(
        event("GET /api/v1/records", query="sort=sideways", cookies=cookies),
        now=NOW,
    )

    assert newest["statusCode"] == 200
    assert records.list_query is not None
    assert records.list_query.sort == "newest"
    assert invalid["statusCode"] == 400


def test_records_rejects_removed_date_filters() -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, FakeRecords()),
    )

    response = controller.handle(
        event(
            "GET /api/v1/records",
            query="from=2026-08-01T00%3A00%3A00Z",
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


def test_rankings_route_requires_authentication_and_rejects_query_parameters() -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, FakeRecords()),
    )
    route = "GET /api/v1/insights/rankings"

    unauthorized = controller.handle(event(route), now=NOW)
    successful = controller.handle(
        event(route, cookies=[f"{SESSION_COOKIE_NAME}=session-token"]),
        now=NOW,
    )
    invalid = controller.handle(
        event(
            route,
            query="unexpected=value",
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )

    assert unauthorized["statusCode"] == 401
    assert successful["statusCode"] == 200
    assert json.loads(successful["body"])["wins"][0] == {
        "rank": 1,
        "displayName": "Arona",
        "avatar": {
            "kind": "placeholder",
            "url": None,
            "alt": "Arona avatar",
            "fallbackVariant": "cyan",
        },
        "count": 3,
    }
    assert invalid["statusCode"] == 400


def test_costs_route_defaults_to_week_and_rejects_unknown_or_duplicate_period() -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, FakeRecords()),
    )
    route = "GET /api/v1/insights/costs"
    cookies = [f"{SESSION_COOKIE_NAME}=session-token"]

    unauthorized = controller.handle(event(route), now=NOW)
    default = controller.handle(event(route, cookies=cookies), now=NOW)
    today = controller.handle(event(route, query="period=today", cookies=cookies), now=NOW)
    invalid = controller.handle(
        event(route, query="period=week&period=all", cookies=cookies), now=NOW
    )
    extra = controller.handle(
        event(route, query="period=week&internal=true", cookies=cookies), now=NOW
    )

    assert unauthorized["statusCode"] == 401
    assert json.loads(default["body"])["period"] == "week"
    assert json.loads(today["body"])["period"] == "today"
    assert invalid["statusCode"] == 400
    assert extra["statusCode"] == 400
    assert "amountUsd" not in default["body"]


def test_unknown_auth_failure_is_content_free() -> None:
    class FailingService(FakeAuthService):
        def begin(self, *, return_to: str | None, now: datetime) -> OAuthStart:
            del return_to, now
            raise AuthFailure("provider_private_detail")

    response = AuthHttpController(cast(Any, FailingService())).handle(
        event("GET /api/v1/auth/discord/start", query="returnTo=%2F"),
        now=NOW,
    )

    assert response["statusCode"] == 503
    assert "provider_private_detail" not in response["body"]
