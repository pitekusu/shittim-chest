"""API Gateway HTTP API v2 boundary for Records."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlencode

from shittim_records.auth import (
    CSRF_COOKIE_NAME,
    OAUTH_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthFailure,
    AuthService,
    AuthStore,
    csrf_hash,
    session_hash,
)
from shittim_records.contracts import (
    AnonymousSession,
    AuthenticatedSession,
    ErrorBody,
    ErrorResponse,
    ImageAvatarRef,
    PlaceholderAvatarRef,
    SessionResponse,
    SessionUser,
)
from shittim_records.read_api import (
    ListQuery,
    ReadFailure,
    RecordsReadService,
)

JSON_HEADERS = {
    "Cache-Control": "private, no-store",
    "Content-Type": "application/json; charset=utf-8",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
REDIRECT_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(frozen=True, slots=True)
class Request:
    route_key: str
    request_id: str
    raw_query: str
    headers: Mapping[str, str]
    cookies: Mapping[str, str]
    path_parameters: Mapping[str, str]


class AuthHttpController:
    """Expose only the four OAuth and session routes."""

    def __init__(self, service: AuthService) -> None:
        self._service = service

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        try:
            if request.route_key == "GET /api/v1/auth/discord/start":
                query = _query(request.raw_query)
                result = self._service.begin(return_to=_optional_single(query, "returnTo"), now=now)
                return redirect(result.location, cookies=[result.oauth_cookie])
            if request.route_key == "GET /api/v1/auth/discord/callback":
                query = _query(request.raw_query)
                result = self._service.complete(
                    code=_required_single(query, "code"),
                    state=_required_single(query, "state"),
                    browser_nonce=request.cookies.get(OAUTH_COOKIE_NAME, ""),
                    now=now,
                )
                return redirect(
                    result.location,
                    cookies=[result.session_cookie, result.csrf_cookie, result.clear_oauth_cookie],
                )
            if request.route_key == "GET /api/v1/session":
                return self._session(request=request, now=now)
            if request.route_key == "POST /api/v1/logout":
                clear = self._service.logout(
                    raw_session=request.cookies.get(SESSION_COOKIE_NAME),
                    raw_csrf=request.cookies.get(CSRF_COOKIE_NAME),
                    csrf_header=_header(request.headers, "x-csrf-token"),
                    origin=_header(request.headers, "origin"),
                    now=now,
                )
                return empty(204, cookies=list(clear))
            return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
        except AuthFailure as error:
            if request.route_key == "GET /api/v1/auth/discord/callback":
                origin = self._service.allowed_origin
                location = f"{origin}/login?{urlencode({'error': error.code})}"
                return redirect(location, cookies=[_clear_oauth_cookie()])
            status = {
                "return_to_invalid": 400,
                "oauth_request_invalid": 400,
                "session_required": 401,
                "csrf_invalid": 403,
                "origin_invalid": 403,
            }.get(error.code, 503)
            return error_response(status, _public_error_code(error.code), request.request_id)

    def _session(self, *, request: Request, now: datetime) -> dict[str, Any]:
        raw_session = request.cookies.get(SESSION_COOKIE_NAME)
        raw_csrf = request.cookies.get(CSRF_COOKIE_NAME)
        session = self._service.authenticate(raw_session=raw_session, now=now)
        if session is None or raw_csrf is None:
            payload = SessionResponse(root=AnonymousSession(schema_version=1, authenticated=False))
        else:
            expected = csrf_hash(self._service.session_hmac_key, raw_csrf)
            if not hmac.compare_digest(expected, session.csrf_hash):
                payload = SessionResponse(
                    root=AnonymousSession(schema_version=1, authenticated=False)
                )
            else:
                if session.avatar_asset_key is None:
                    avatar = PlaceholderAvatarRef(
                        kind="placeholder",
                        alt=f"{session.display_name}のアバター",
                        fallback_variant="cyan",
                    )
                else:
                    avatar = ImageAvatarRef(
                        kind="image",
                        url=self._service.avatar_url(asset_key=session.avatar_asset_key),
                        alt=f"{session.display_name}のアバター",
                        fallback_variant="cyan",
                    )
                payload = SessionResponse(
                    root=AuthenticatedSession(
                        schema_version=1,
                        authenticated=True,
                        user=SessionUser(display_name=session.display_name, avatar=avatar),
                        csrf_token=raw_csrf,
                    )
                )
        return json_response(200, payload.model_dump(by_alias=True, mode="json"))


class ReadHttpController:
    """Authenticate then expose Records and insights reads."""

    def __init__(
        self,
        *,
        store: AuthStore,
        session_key: bytes,
        records: RecordsReadService,
    ) -> None:
        self._store = store
        self._session_key = session_key
        self._records = records

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        raw_session = request.cookies.get(SESSION_COOKIE_NAME)
        if not raw_session:
            return error_response(401, "AUTHENTICATION_REQUIRED", request.request_id)
        try:
            session = self._store.get_session(
                session_hash=session_hash(self._session_key, raw_session)
            )
            if session is None or session.expires_at <= int(now.astimezone(UTC).timestamp()):
                return error_response(401, "AUTHENTICATION_REQUIRED", request.request_id)
            if request.route_key == "GET /api/v1/records":
                query = _query(request.raw_query)
                if not set(query).issubset({"cursor", "limit", "sort", "winner"}):
                    raise ReadFailure("REQUEST_INVALID", 400)
                result = self._records.list_records(
                    query=ListQuery(
                        limit=_limit(_optional_single(query, "limit")),
                        sort=_sort(_optional_single(query, "sort")),
                        winner=_winner(_optional_single(query, "winner")),
                        cursor=_optional_single(query, "cursor"),
                    ),
                    now=now,
                )
            elif request.route_key == "GET /api/v1/records/{recordId}":
                result = self._records.get_record(
                    record_id=request.path_parameters.get("recordId", ""),
                    now=now,
                )
            elif request.route_key == "GET /api/v1/insights/rankings":
                if request.raw_query:
                    raise ReadFailure("REQUEST_INVALID", 400)
                result = self._records.get_rankings(now=now)
            elif request.route_key == "GET /api/v1/insights/costs":
                query = _query(request.raw_query)
                if not set(query).issubset({"period"}):
                    raise ReadFailure("REQUEST_INVALID", 400)
                result = self._records.get_costs(
                    period=_cost_period(_optional_single(query, "period")),
                    now=now,
                )
            else:
                return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
            return json_response(200, result.model_dump(by_alias=True, mode="json"))
        except AuthFailure as error:
            status = 400 if error.code == "oauth_request_invalid" else 503
            code = "REQUEST_INVALID" if status == 400 else "RECORDS_UNAVAILABLE"
            return error_response(status, code, request.request_id)
        except ReadFailure as error:
            return error_response(error.status, error.code, request.request_id)


def parse_request(event: Mapping[str, Any]) -> Request:
    route_key = event.get("routeKey")
    request_context = event.get("requestContext")
    if not isinstance(route_key, str) or not isinstance(request_context, Mapping):
        raise ValueError("HTTP API event is invalid")
    request_id = request_context.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("HTTP API event has no request ID")
    headers = event.get("headers") or {}
    path = event.get("pathParameters") or {}
    if not isinstance(headers, Mapping) or not isinstance(path, Mapping):
        raise ValueError("HTTP API event fields are invalid")
    raw_cookies = event.get("cookies") or []
    if not isinstance(raw_cookies, list) or any(not isinstance(item, str) for item in raw_cookies):
        raise ValueError("HTTP API cookies are invalid")
    return Request(
        route_key=route_key,
        request_id=request_id,
        raw_query=str(event.get("rawQueryString") or ""),
        headers={str(key).lower(): str(value) for key, value in headers.items()},
        cookies=_cookies(raw_cookies),
        path_parameters={str(key): str(value) for key, value in path.items()},
    )


def json_response(status: int, payload: object) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": JSON_HEADERS,
        "body": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }


def error_response(status: int, code: str, request_id: str) -> dict[str, Any]:
    message = {
        "AUTHENTICATION_REQUIRED": "ログインが必要です。",
        "RECORD_NOT_FOUND": "指定された記録は見つかりませんでした。",
        "ROUTE_NOT_FOUND": "指定されたAPIは存在しません。",
        "REQUEST_INVALID": "リクエストが正しくありません。",
        "CURSOR_INVALID": "ページ情報が正しくありません。",
        "INSIGHTS_UNAVAILABLE": "集計を準備しています。",
    }.get(code, "議事録サービスを利用できません。")
    payload = ErrorResponse(error=ErrorBody(code=code, message=message, request_id=request_id))
    return json_response(status, payload.model_dump(by_alias=True, mode="json"))


def redirect(location: str, *, cookies: list[str]) -> dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {**REDIRECT_HEADERS, "Location": location},
        "cookies": cookies,
        "body": "",
    }


def empty(status: int, *, cookies: list[str]) -> dict[str, Any]:
    return {"statusCode": status, "headers": JSON_HEADERS, "cookies": cookies, "body": ""}


def _query(raw: str) -> dict[str, list[str]]:
    try:
        return parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=20)
    except ValueError:
        raise AuthFailure("oauth_request_invalid") from None


def _required_single(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name)
    if values is None or len(values) != 1 or not values[0]:
        raise AuthFailure("oauth_request_invalid")
    return values[0]


def _optional_single(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise AuthFailure("oauth_request_invalid")
    return values[0]


def _limit(value: str | None) -> int:
    if value is None:
        return 12
    try:
        parsed = int(value)
    except ValueError:
        raise ReadFailure("REQUEST_INVALID", 400) from None
    if str(parsed) != value or not 1 <= parsed <= 50:
        raise ReadFailure("REQUEST_INVALID", 400)
    return parsed


def _winner(value: str | None) -> Any:
    if value is None:
        return None
    if value not in {"participant-a", "participant-b", "participant-c"}:
        raise ReadFailure("REQUEST_INVALID", 400)
    return value


def _sort(value: str | None) -> Any:
    if value is None:
        return "newest"
    if value not in {"newest", "oldest"}:
        raise ReadFailure("REQUEST_INVALID", 400)
    return value


def _cost_period(value: str | None) -> Any:
    if value is None:
        return "week"
    if value not in {"today", "week", "month", "all"}:
        raise ReadFailure("REQUEST_INVALID", 400)
    return value


def _cookies(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        parsed = SimpleCookie()
        parsed.load(value)
        for name, morsel in parsed.items():
            if name in result:
                raise ValueError("duplicate HTTP cookie")
            result[name] = morsel.value
    return result


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return headers.get(name.lower())


def _public_error_code(code: str) -> str:
    return {
        "return_to_invalid": "REQUEST_INVALID",
        "oauth_request_invalid": "REQUEST_INVALID",
        "session_required": "AUTHENTICATION_REQUIRED",
        "csrf_invalid": "CSRF_INVALID",
        "origin_invalid": "ORIGIN_INVALID",
    }.get(code, "RECORDS_UNAVAILABLE")


def _clear_oauth_cookie() -> str:
    return f"{OAUTH_COOKIE_NAME}=; Path=/; Max-Age=0; Secure; SameSite=Lax; HttpOnly"
