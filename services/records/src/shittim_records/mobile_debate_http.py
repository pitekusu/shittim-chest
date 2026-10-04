"""Strict Bearer-only native debate API boundary."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from shittim_records.auth import AuthFailure
from shittim_records.contracts import DebateStartRequest
from shittim_records.http_api import (
    JSON_HEADERS,
    _query,
    _read_bearer_token,
    _required_single,
    error_response,
    json_response,
    parse_request,
)
from shittim_records.mobile_debate import DebateRequestFailure, MobileDebateService
from shittim_records.mobile_http import _empty_body, _unique_fields

PREFIX = "/api/v1/debate-requests"


class MobileDebateHttpController:
    def __init__(self, service: MobileDebateService, *, allowed_origin: str) -> None:
        self._service = service
        self._origin = allowed_origin

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        request = parse_request(event)
        try:
            if event.get("cookies") or "cookie" in request.headers:
                raise DebateRequestFailure("REQUEST_INVALID", 400)
            if "origin" in request.headers and request.headers["origin"] != self._origin:
                raise DebateRequestFailure("ORIGIN_INVALID", 403)
            token = _read_bearer_token(event.get("headers") or {}, has_cookies=False)
            if token is None:
                raise AuthFailure("session_required")
            if len(request.raw_query.encode()) > 4096:
                raise DebateRequestFailure("REQUEST_INVALID", 400)
            query = _query(request.raw_query)
            if request.route_key == f"POST {PREFIX}":
                if query:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                raw = event.get("body")
                if (
                    event.get("isBase64Encoded") is True
                    or not isinstance(raw, str)
                    or len(raw.encode("utf-8")) > 16384
                    or request.headers.get("content-type", "").partition(";")[0].lower().strip()
                    != "application/json"
                ):
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                data = json.loads(raw, object_pairs_hook=_unique_fields)
                if not isinstance(data, dict) or set(data) != {"requestId", "question"}:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                payload = DebateStartRequest.model_validate(data, strict=True)
                response, _created = asyncio.run(self._service.start(token=token, payload=payload))
                return json_response(202, response.model_dump(by_alias=True, mode="json"))
            _empty_body(event)
            if request.route_key == f"GET {PREFIX}/{{requestId}}":
                if query:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                request_id = request.path_parameters.get("requestId")
                payload = DebateStartRequest.model_validate(
                    {"requestId": request_id, "question": "lookup"},
                    strict=True,
                )
                response = asyncio.run(
                    self._service.get(token=token, request_id=payload.request_id)
                )
            elif request.route_key == f"GET {PREFIX}":
                if not set(query) <= {"limit", "cursor"}:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                limit = 20 if "limit" not in query else int(_required_single(query, "limit"))
                cursor = None if "cursor" not in query else _required_single(query, "cursor")
                response = asyncio.run(self._service.list(token=token, limit=limit, cursor=cursor))
            else:
                return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
            return json_response(200, response.model_dump(by_alias=True, mode="json"))
        except DebateRequestFailure as error:
            return error_response(error.status, error.code, request.request_id)
        except AuthFailure as error:
            status, code = (
                (401, "AUTHENTICATION_REQUIRED")
                if error.code == "session_required"
                else (400, "REQUEST_INVALID")
            )
            response = error_response(status, code, request.request_id)
            if status == 401:
                response["headers"] = {**JSON_HEADERS, "WWW-Authenticate": "Bearer"}
            return response
        except ValidationError, ValueError, TypeError, RecursionError:
            return error_response(400, "REQUEST_INVALID", request.request_id)
