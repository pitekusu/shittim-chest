"""Strict HTTP boundary for device login; Web cookies never authorize mobile API calls."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from shittim_records.auth import AuthFailure, _clear_cookie
from shittim_records.http_api import (
    JSON_HEADERS,
    Request,
    _query,
    _read_bearer_token,
    _required_single,
    error_response,
    json_response,
    parse_request,
    redirect,
)
from shittim_records.mobile_auth import (
    MobileAuthorizeRequest,
    MobileExchangeRequest,
    MobileModel,
    MobileStartRequest,
    parse_mobile_request,
)
from shittim_records.mobile_callback import MobileCallbackService
from shittim_records.mobile_exchange import MobileExchangeService
from shittim_records.mobile_login import MOBILE_OAUTH_COOKIE_NAME, MobileLoginService
from shittim_records.mobile_session import MobileSessionService

MOBILE_API_PREFIX = "/api/v1/auth/mobile/"
DISCORD_CALLBACK_ROUTE = "GET /api/v1/auth/discord/callback"
MAX_INPUT_BYTES = 4096


class MobileAuthHttpController:
    def __init__(
        self,
        *,
        login: MobileLoginService,
        callback: MobileCallbackService,
        exchange: MobileExchangeService,
        sessions: MobileSessionService,
        allowed_origin: str,
    ) -> None:
        self._login = login
        self._callback = callback
        self._exchange = exchange
        self._sessions = sessions
        self._allowed_origin = allowed_origin

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        try:
            if len(request.raw_query.encode("utf-8")) > MAX_INPUT_BYTES:
                raise AuthFailure("mobile_request_invalid")
            query = _query(request.raw_query)
            if request.route_key == DISCORD_CALLBACK_ROUTE:
                _empty_body(event)
                if "authorization" in request.headers or not set(query) <= {
                    "code",
                    "state",
                    "error",
                    "error_description",
                }:
                    raise AuthFailure("mobile_request_invalid")
                # Provider cancellation/details are never echoed or turned into an app grant.
                if "error" in query:
                    raise AuthFailure("mobile_grant_invalid")
                if set(query) != {"code", "state"}:
                    raise AuthFailure("mobile_request_invalid")
                result = self._callback.complete(
                    code=_required_single(query, "code"),
                    state=_required_single(query, "state"),
                    browser_nonce=request.cookies.get(MOBILE_OAUTH_COOKIE_NAME),
                )
                return redirect(result.location, cookies=[result.clear_oauth_cookie])
            if request.route_key == f"GET {MOBILE_API_PREFIX}authorize":
                _empty_body(event)
                if "authorization" in request.headers or set(query) != {"transaction"}:
                    raise AuthFailure("mobile_request_invalid")
                payload = parse_mobile_request(
                    MobileAuthorizeRequest, {"transaction": _required_single(query, "transaction")}
                )
                browser = self._login.authorize(payload, now=now)
                return redirect(browser.location, cookies=[browser.oauth_cookie])

            if query:
                raise AuthFailure("mobile_request_invalid")
            # Native requests have no ambient credentials or Origin. If a browser supplies
            # Origin it must match exactly; Cookie auth/CSRF are never used on these routes.
            if "origin" in request.headers and request.headers["origin"] != self._allowed_origin:
                raise AuthFailure("origin_invalid")
            if event.get("cookies") or "cookie" in request.headers:
                raise AuthFailure("mobile_request_invalid")
            if request.route_key in {
                f"POST {MOBILE_API_PREFIX}start",
                f"POST {MOBILE_API_PREFIX}exchange",
            }:
                if "authorization" in request.headers:
                    raise AuthFailure("mobile_request_invalid")
                if request.route_key.endswith("/start"):
                    response = self._login.begin(_body(event, request, MobileStartRequest), now=now)
                else:
                    response = self._exchange.exchange(_body(event, request, MobileExchangeRequest))
                return json_response(200, response.model_dump(by_alias=True, mode="json"))
            if request.route_key in {
                f"GET {MOBILE_API_PREFIX}session",
                f"POST {MOBILE_API_PREFIX}logout",
            }:
                _empty_body(event)
                token = _read_bearer_token(event.get("headers") or {}, has_cookies=False)
                if request.route_key.endswith("/session"):
                    session = self._sessions.session(raw_token=token)
                    return json_response(200, session.model_dump(by_alias=True, mode="json"))
                self._sessions.logout(raw_token=token)
                return {"statusCode": 204, "headers": JSON_HEADERS, "body": ""}
            return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
        except AuthFailure as error:
            status, code = {
                "mobile_request_invalid": (400, "REQUEST_INVALID"),
                "oauth_request_invalid": (400, "REQUEST_INVALID"),
                "mobile_grant_invalid": (400, "MOBILE_GRANT_INVALID"),
                "session_required": (401, "AUTHENTICATION_REQUIRED"),
                "origin_invalid": (403, "ORIGIN_INVALID"),
                "guild_membership_required": (403, "GUILD_MEMBERSHIP_REQUIRED"),
            }.get(error.code, (503, "RECORDS_UNAVAILABLE"))
            if request.route_key == DISCORD_CALLBACK_ROUTE:
                return _callback_error(status)
            response = error_response(status, code, request.request_id)
            if status == 401:
                response["headers"] = {**JSON_HEADERS, "WWW-Authenticate": "Bearer"}
            return response


def _body[Model: MobileModel](
    event: Mapping[str, Any], request: Request, model: type[Model]
) -> Model:
    raw = event.get("body")
    if (
        event.get("isBase64Encoded") is True
        or not isinstance(raw, str)
        or request.headers.get("content-type", "").partition(";")[0].strip().lower()
        != "application/json"
    ):
        raise AuthFailure("mobile_request_invalid")
    try:
        if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
            raise AuthFailure("mobile_request_invalid")
        payload = json.loads(raw, object_pairs_hook=_unique_fields)
    except ValueError, RecursionError:
        raise AuthFailure("mobile_request_invalid") from None
    return parse_mobile_request(model, payload)


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in pairs:
        if key in fields:
            raise AuthFailure("mobile_request_invalid")
        fields[key] = value
    return fields


def _empty_body(event: Mapping[str, Any]) -> None:
    if event.get("isBase64Encoded") is True or event.get("body") not in (None, ""):
        raise AuthFailure("mobile_request_invalid")


def _callback_error(status: int) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            **JSON_HEADERS,
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
        "cookies": [_clear_cookie(MOBILE_OAUTH_COOKIE_NAME)],
        "body": (
            '<!doctype html><html lang="ja"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>アプリのログインを完了できませんでした</title>"
            "<h1>アプリのログインを完了できませんでした</h1>"
            "<p>アプリに戻り、ログインをやり直してください。</p></html>"
        ),
    }
