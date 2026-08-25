"""HTTP API v2 controller for the authenticated Records ADMIN status boundary."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from shittim_records.admin import AdminAuthorizer, AdminFailure
from shittim_records.admin_status import AdminStatusService
from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from shittim_records.http_api import error_response, json_response, parse_request


class AdminStatusHttpController:
    """Expose only sanitized read-only status snapshots."""

    def __init__(
        self,
        *,
        authorizer: AdminAuthorizer,
        status: AdminStatusService,
    ) -> None:
        self._authorizer = authorizer
        self._status = status

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        try:
            session = self._authorizer.authenticate(
                raw_session=request.cookies.get(SESSION_COOKIE_NAME),
                now=now,
            )
            if request.route_key == "GET /api/v1/admin/status":
                _reject_query(request.raw_query)
                response = self._status.get(now=now)
            elif request.route_key == "POST /api/v1/admin/status/refresh":
                _reject_query(request.raw_query)
                if event.get("body") not in (None, ""):
                    raise AdminFailure("REQUEST_INVALID", 400)
                self._authorizer.authorize_write(
                    session=session,
                    raw_csrf=request.cookies.get(CSRF_COOKIE_NAME),
                    csrf_header=request.headers.get("x-csrf-token"),
                    origin=request.headers.get("origin"),
                    idempotency_key=request.headers.get("x-idempotency-key"),
                )
                response = self._status.refresh(now=now)
            else:
                return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
            return json_response(200, response.model_dump(by_alias=True, mode="json"))
        except AdminFailure as error:
            return error_response(error.status, error.code, request.request_id)
        except ValidationError:
            return error_response(503, "ADMIN_STATUS_INVALID", request.request_id)


def _reject_query(raw_query: str) -> None:
    if raw_query:
        raise AdminFailure("REQUEST_INVALID", 400)
