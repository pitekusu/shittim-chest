"""API Gateway HTTP API v2 boundary tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.test_read_api import affection_records

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
    AffectionRankingEntry,
    AffectionRankingsResponse,
    CostBreakdown,
    CostConversion,
    CostsResponse,
    ParticipantAffectionRanking,
    PlaceholderAvatarRef,
    RankingEntry,
    RankingsResponse,
    RecordListResponse,
    RecordRequestersResponse,
    RecordSearchRequest,
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

    def is_admin(self, session: SessionRecord) -> bool:
        return session.requester_key == "requester"

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
        self.search_query: RecordSearchRequest | None = None
        self.requester_cursor: str | None = None

    def list_records(self, **_kwargs: Any) -> RecordListResponse:
        self.list_query = _kwargs["query"]
        return RecordListResponse(schema_version=1, items=())

    def search_records(self, **kwargs: Any) -> RecordListResponse:
        self.search_query = kwargs["query"]
        return RecordListResponse(schema_version=1, items=())

    def list_requesters(self, **kwargs: Any) -> RecordRequestersResponse:
        self.requester_cursor = kwargs["cursor"]
        return RecordRequestersResponse(schema_version=1, items=())

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

    def get_affection_rankings(self, **_kwargs: Any) -> AffectionRankingsResponse:
        rankings = tuple(
            ParticipantAffectionRanking(
                participant=cast(Any, slot),
                display_name=name,
                entries=(
                    AffectionRankingEntry(
                        rank=1,
                        display_name="Requester",
                        avatar=PlaceholderAvatarRef(
                            kind="placeholder",
                            alt="Requester avatar",
                            fallback_variant="cyan",
                        ),
                        score=500,
                    ),
                ),
            )
            for slot, name in (
                ("participant-a", "Arona"),
                ("participant-b", "Plana"),
                ("participant-c", "Participant C"),
            )
        )
        return AffectionRankingsResponse(
            schema_version=1,
            generated_at=NOW,
            default_score=500,
            max_score=1000,
            rankings=cast(Any, rankings),
            next_cursor=None,
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


def _momotalk_image_controller(
    moods: tuple[str, str, str],
) -> tuple[ReadHttpController, list[str]]:
    from shittim_records.momotalk import Room
    from shittim_records.momotalk_read import MomotalkReadService

    slots = ("participant-a", "participant-b", "participant-c")
    order = (0, 1, 0, 2, 1, 2, 0, 1, 2)
    room = Room.model_validate(
        {
            "week_id": "2026-08-16",
            "room_id": "r" * 43,
            "display_name": "架空の先生",
            "question_count": 1,
            "state": "ready",
            "plan": {
                "summary": "架空の週次会話",
                "turns": [{"participant": slots[index], "topic": "散歩"} for index in order],
                "images": [
                    {"mood": mood, "record_id": "q" * 43, "brief": "散歩"} for mood in moods
                ],
            },
            "messages": [{"participant": slots[index], "text": "会話"} for index in order],
            "images": [
                {"participant": slot, "mood": mood, "state": "ready"}
                for slot, mood in zip(slots, moods, strict=True)
            ],
        }
    )
    signed: list[str] = []

    def image_url(_room: Any, image: Any, **kwargs: Any) -> str:
        signed.append(image.participant)
        return f"https://media.example.invalid/{image.participant}/{kwargs}"

    momotalk = MomotalkReadService(
        cast(
            Any,
            SimpleNamespace(
                get_week=lambda _: {
                    "week": {
                        "week_id": "2026-08-16",
                        "period_start": "2026-08-09T09:00:00Z",
                        "period_end": "2026-08-16T09:00:00Z",
                        "publish_at": "2026-08-16T11:00:00Z",
                    }
                },
                get_room=lambda *_: room,
            ),
        ),
        cast(Any, SimpleNamespace(image_url=image_url)),
        cast(Any, SimpleNamespace(avatar_url=lambda **_: "https://media.example.invalid/avatar")),
    )
    return (
        ReadHttpController(
            store=cast(Any, FakeSessionStore(session())),
            session_key=SESSION_KEY,
            records=cast(Any, None),
            momotalk=momotalk,
        ),
        signed,
    )


@pytest.mark.parametrize("moods", (("happy", "happy", "happy"), ("unhappy", "happy", "happy")))
@pytest.mark.parametrize("negotiated", (False, True))
def test_momotalk_image_contract_preserves_legacy_mood_selection_before_signing(
    moods: tuple[str, str, str], negotiated: bool
) -> None:
    controller, signed = _momotalk_image_controller(moods)
    response = controller.handle(
        event(
            "GET /api/v1/momotalk/weeks/{weekId}/rooms/{roomId}",
            query="contract=momotalk-images-v2" if negotiated else "",
            path={"weekId": "2026-08-16", "roomId": "r" * 43},
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    payload = json.loads(response["body"])
    expected = (
        ["participant-a", "participant-b", "participant-c"]
        if negotiated
        else ["participant-a"]
        if len(set(moods)) == 1
        else ["participant-a", "participant-b"]
    )
    assert [image["participant"] for image in payload["images"]] == expected
    assert signed == [participant for participant in expected for _ in range(3)]
    assert len(payload["messages"]) == 9
    if not negotiated:
        assert len({image["mood"] for image in payload["images"]}) == len(payload["images"]) <= 2


@pytest.mark.parametrize(
    "query",
    (
        "contract=unknown",
        "contract=",
        "contract=momotalk-images-v2&contract=momotalk-images-v2",
        "contract=momotalk-images-v2&extra=private",
        "limit=1",
        "cursor=opaque",
    ),
)
def test_momotalk_detail_rejects_invalid_image_contract_before_reading(query: str) -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, None),
        momotalk=object(),
    )
    response = controller.handle(
        event(
            "GET /api/v1/momotalk/weeks/{weekId}/rooms/{roomId}",
            query=query,
            path={"weekId": "2026-08-16", "roomId": "r" * 43},
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )
    assert response["statusCode"] == 400


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
            query="contract=admin-v1",
            cookies=[
                f"{SESSION_COOKIE_NAME}=session-token",
                f"{CSRF_COOKIE_NAME}=csrf-token",
            ],
        ),
        now=NOW,
    )

    payload = json.loads(response["body"])
    assert payload["authenticated"] is True
    assert payload["isAdmin"] is True
    assert payload["csrfToken"] == "csrf-token"
    assert payload["user"]["avatar"]["kind"] == "image"
    assert "requesterKey" not in repr(payload)


def test_authenticated_legacy_session_response_omits_admin_capability() -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService(session())))

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

    assert "isAdmin" not in json.loads(response["body"])


@pytest.mark.parametrize(
    "query",
    (
        "contract=unknown",
        "contract=admin-v1&contract=admin-v1",
        "unexpected=value",
    ),
)
def test_session_rejects_unknown_or_ambiguous_contract_negotiation(query: str) -> None:
    controller = AuthHttpController(cast(Any, FakeAuthService()))

    response = controller.handle(event("GET /api/v1/session", query=query), now=NOW)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


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


@pytest.mark.parametrize("contract", (False, True))
def test_record_detail_negotiates_affection_reasons_without_changing_legacy_response(
    contract: bool,
) -> None:
    records, reader = affection_records()
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=records,
    )
    response = controller.handle(
        event(
            "GET /api/v1/records/{recordId}",
            query="contract=affection-reasons-v1" if contract else "",
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
            path={"recordId": reader.record_id},
        ),
        now=NOW,
    )
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    payload = json.loads(response["body"])
    assert payload["schemaVersion"] == 2
    participants = payload["affection"]["participants"]
    if contract:
        assert participants[0]["reason"] == "\U0002000b" * 500
        assert [item["reasonStatus"] for item in participants] == [
            "available",
            "unavailable",
            "not_recorded",
        ]
        assert participants[1]["reason"] is None and participants[2]["reason"] is None
    else:
        assert all("reason" not in item and "reasonStatus" not in item for item in participants)
    assert "requesterKey" not in response["body"]
    assert "discord-user-id" not in response["body"]


@pytest.mark.parametrize(
    "query",
    (
        "contract=unknown",
        "contract=",
        "contract=affection-reasons-v1&contract=affection-reasons-v1",
        "unexpected=value",
    ),
)
def test_record_detail_rejects_unknown_or_duplicate_contract_before_loading(query: str) -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, FakeRecords()),
    )
    response = controller.handle(
        event(
            "GET /api/v1/records/{recordId}",
            query=query,
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
            path={"recordId": "r" * 43},
        ),
        now=NOW,
    )
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


def test_affection_rankings_route_is_authenticated_no_store_and_has_no_internal_key() -> None:
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, FakeRecords()),
    )
    route = "GET /api/v1/insights/affection-rankings"
    cookies = [f"{SESSION_COOKIE_NAME}=session-token"]

    unauthorized = controller.handle(event(route), now=NOW)
    successful = controller.handle(event(route, cookies=cookies), now=NOW)
    limited = controller.handle(event(route, query="limit=1", cookies=cookies), now=NOW)
    unknown = controller.handle(event(route, query="internal=true", cookies=cookies), now=NOW)
    duplicate = controller.handle(
        event(route, query="limit=1&limit=2", cookies=cookies),
        now=NOW,
    )

    assert unauthorized["statusCode"] == 401
    assert successful["statusCode"] == 200
    assert successful["headers"]["Cache-Control"] == "private, no-store"
    payload = json.loads(successful["body"])
    assert payload["defaultScore"] == 500
    assert len(payload["rankings"]) == 3
    assert all(
        set(entry) == {"rank", "displayName", "avatar", "score"}
        for ranking in payload["rankings"]
        for entry in ranking["entries"]
    )
    assert "requesterKey" not in successful["body"]
    assert limited["statusCode"] == 200
    assert unknown["statusCode"] == 400
    assert duplicate["statusCode"] == 400


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


def search_event(body: str = "{}") -> dict[str, Any]:
    request = event(
        "POST /api/v1/records/search",
        cookies=[f"{SESSION_COOKIE_NAME}=session-token", f"{CSRF_COOKIE_NAME}=csrf-token"],
        headers={
            "origin": "https://records.example.invalid",
            "x-csrf-token": "csrf-token",
            "content-type": "application/json",
        },
    )
    request["body"] = body
    return request


def search_controller(
    records: FakeRecords | None = None, *, expired: bool = False
) -> ReadHttpController:
    return ReadHttpController(
        store=cast(Any, FakeSessionStore(session(expired=expired))),
        session_key=SESSION_KEY,
        allowed_origin="https://records.example.invalid",
        records=cast(Any, records or FakeRecords()),
    )


def test_search_post_uses_private_cookie_session_and_existing_csrf() -> None:
    records = FakeRecords()
    response = search_controller(records).handle(
        search_event(
            json.dumps({"search": "Private question", "requester": "Requester", "sort": "oldest"})
        ),
        now=NOW,
    )
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert json.loads(response["body"]) == {"schemaVersion": 1, "items": [], "nextCursor": None}
    assert records.search_query is not None
    assert records.search_query.search == "Private question"
    assert records.search_query.sort == "oldest"
    assert "Private" not in repr(records.search_query)


@pytest.mark.parametrize("route", ("GET /api/v1/records", "POST /api/v1/records/search"))
@pytest.mark.parametrize("count", (0, 3))
def test_record_list_attachment_count_requires_contract_without_changing_legacy_fields(
    route: str, count: int
) -> None:
    from shittim_chest.adapters.dynamodb.serializer import serialize_debate_image
    from tests.test_debate_attachments import image
    from tests.test_read_api import service

    records, reader = service()
    if count:
        reader.meta["schema_version"] = 4
        reader.meta["attachments"] = [
            serialize_debate_image(image(f"00000000-0000-4000-8000-{index:012d}"))
            for index in range(count)
        ]
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        allowed_origin="https://records.example.invalid",
        records=records,
    )
    request = (
        search_event()
        if route.startswith("POST")
        else event(route, cookies=[f"{SESSION_COOKIE_NAME}=session-token"])
    )
    legacy = controller.handle(request, now=NOW)
    request["rawQueryString"] = "contract=record-attachments-v1"
    negotiated = controller.handle(request, now=NOW)

    assert legacy["statusCode"] == negotiated["statusCode"] == 200
    old_payload, new_payload = (json.loads(response["body"]) for response in (legacy, negotiated))
    assert set(old_payload["items"][0]) == {
        "schemaVersion",
        "recordId",
        "completedAt",
        "questionPreview",
        "requester",
        "participants",
        "result",
    }
    assert new_payload["items"][0].pop("attachmentCount") == count
    assert old_payload == new_payload


@pytest.mark.parametrize("route", ("GET /api/v1/records", "POST /api/v1/records/search"))
@pytest.mark.parametrize(
    "query",
    (
        "contract=unknown",
        "contract=",
        "contract=record-attachments-v1&contract=record-attachments-v1",
        "contract=record-attachments-v1&extra=private",
    ),
)
def test_record_list_rejects_invalid_contract_before_loading(route: str, query: str) -> None:
    records = FakeRecords()
    request = (
        search_event()
        if route.startswith("POST")
        else event(route, cookies=[f"{SESSION_COOKIE_NAME}=session-token"])
    )
    request["rawQueryString"] = query
    result = search_controller(records).handle(request, now=NOW)
    assert result["statusCode"] == 400
    assert records.list_query is None and records.search_query is None


@pytest.mark.parametrize(
    "change",
    (
        {"origin": "https://foreign.example.invalid"},
        {"origin": ""},
        {"x-csrf-token": ""},
        {"x-csrf-token": "different"},
    ),
)
def test_search_rejects_foreign_origin_and_missing_or_mismatched_csrf(
    change: dict[str, str],
) -> None:
    request = search_event()
    request["headers"].update(change)
    response = search_controller().handle(request, now=NOW)
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error"]["code"] in {"ORIGIN_INVALID", "CSRF_INVALID"}


@pytest.mark.parametrize(
    "body",
    (
        "",
        "{",
        "[]",
        '{"search":"Private phrase","search":"other"}',
        '{"search":null}',
        '{"unknown":"Private phrase"}',
        '{"requester":" "}',
        '{"limit":true}',
        '{"limit":"12"}',
        '{"limit":0}',
        '{"limit":51}',
        '{"sort":"unknown"}',
        '{"winner":"participant-z"}',
        '{"cursor":""}',
        json.dumps({"search": "x" * 1001}),
        json.dumps({"search": "x" * 17000}),
    ),
)
def test_search_rejects_malformed_or_excessive_json_without_returning_private_inputs(
    body: str,
) -> None:
    records = FakeRecords()
    response = search_controller(records).handle(search_event(body), now=NOW)
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"
    assert "Private phrase" not in response["body"]
    assert records.search_query is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("rawQueryString", "search=private"),
        ("isBase64Encoded", True),
        ("pathParameters", {"extra": "private"}),
    ),
)
def test_search_rejects_non_body_search_values_and_base64(field: str, value: Any) -> None:
    request = search_event()
    request[field] = value
    assert search_controller().handle(request, now=NOW)["statusCode"] == 400


def test_search_requires_json_cookie_auth_and_live_session() -> None:
    request = search_event()
    request["headers"]["content-type"] = "text/plain"
    assert search_controller().handle(request, now=NOW)["statusCode"] == 400
    assert search_controller(expired=True).handle(search_event(), now=NOW)["statusCode"] == 401
    request = search_event()
    request["cookies"] = []
    request["headers"]["authorization"] = "Bearer " + "x" * 43
    assert search_controller().handle(request, now=NOW)["statusCode"] == 401
    request = search_event()
    request["cookies"] = [f"{SESSION_COOKIE_NAME}=session-token"]
    assert search_controller().handle(request, now=NOW)["statusCode"] == 403


def test_requester_route_is_authenticated_and_accepts_only_cursor() -> None:
    records = FakeRecords()
    controller = search_controller(records)
    request = event(
        "GET /api/v1/records/requesters",
        query="cursor=opaque",
        cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
    )
    response = controller.handle(request, now=NOW)
    assert response["statusCode"] == 200
    assert records.requester_cursor == "opaque"
    assert json.loads(response["body"]) == {"schemaVersion": 1, "items": [], "nextCursor": None}
    request["rawQueryString"] = "limit=250"
    assert controller.handle(request, now=NOW)["statusCode"] == 400
    assert controller.handle(event("GET /api/v1/records/requesters"), now=NOW)["statusCode"] == 401


def test_search_http_to_archive_finds_text_beyond_card_preview() -> None:
    from tests.test_read_api import PagedSearchReader, service

    reader = PagedSearchReader(15)
    reader.rows[0]["question"] = "prefix " * 30 + "complete question ending"
    records, _ = service(reader)
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        allowed_origin="https://records.example.invalid",
        records=records,
    )
    response = controller.handle(search_event('{"search":"complete question ending"}'), now=NOW)
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert [item["recordId"] for item in payload["items"]] == [reader.rows[0]["record_id"]]
    assert payload["nextCursor"] is None
    assert "complete question ending" not in payload["items"][0]["questionPreview"]
