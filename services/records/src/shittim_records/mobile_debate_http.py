"""Strict Bearer-only native debate API boundary."""

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from shittim_chest.application.discord import PanelAction

from shittim_records.auth import AuthFailure
from shittim_records.contracts import (
    AttachmentId,
    DebateAttachmentUploadRequest,
    DebateControlRequest,
    DebateRequestId,
    DebateStartRequest,
)
from shittim_records.debate_attachments import AttachmentFailure, DebateAttachmentService
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
    def __init__(
        self,
        service: MobileDebateService,
        *,
        allowed_origin: str,
        attachments: DebateAttachmentService | None = None,
    ) -> None:
        self._attachments = attachments
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
            control_actions = {
                f"POST {PREFIX}/{{requestId}}/cancel": PanelAction.CANCEL,
                f"POST {PREFIX}/{{requestId}}/retry": PanelAction.RETRY,
            }
            operation_route = f"GET {PREFIX}/{{requestId}}/operations/{{operationId}}"
            if request.route_key in control_actions or request.route_key == operation_route:
                if query:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                request_id = TypeAdapter(DebateRequestId).validate_python(
                    request.path_parameters.get("requestId"), strict=True
                )
                if request.route_key == operation_route:
                    _empty_body(event)
                    operation_id = TypeAdapter(DebateRequestId).validate_python(
                        request.path_parameters.get("operationId"), strict=True
                    )
                    result = asyncio.run(
                        self._service.get_operation(
                            token=token, request_id=request_id, operation_id=operation_id
                        )
                    )
                    return json_response(200, result.model_dump(by_alias=True, mode="json"))
                raw = event.get("body")
                if (
                    event.get("isBase64Encoded") is True
                    or not isinstance(raw, str)
                    or len(raw.encode()) > 1024
                    or request.headers.get("content-type", "").partition(";")[0].strip().lower()
                    != "application/json"
                ):
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                data = json.loads(raw, object_pairs_hook=_unique_fields)
                if not isinstance(data, dict) or set(data) != {"operationId", "attemptToken"}:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                payload = DebateControlRequest.model_validate(data, strict=True)
                result = asyncio.run(
                    self._service.control(
                        token=token,
                        request_id=request_id,
                        action=control_actions[request.route_key],
                        payload=payload,
                    )
                )
                return json_response(202, result.model_dump(by_alias=True, mode="json"))
            upload_prefix = "/api/v1/debate-attachments/uploads"
            if request.route_key.partition(" ")[2].startswith(upload_prefix):
                if self._attachments is None:
                    raise AttachmentFailure("ATTACHMENT_UNAVAILABLE", 503)
                if query:
                    raise DebateRequestFailure("REQUEST_INVALID", 400)
                session = asyncio.run(self._service.authenticate_upload(token))
                now = datetime.now(UTC)
                if request.route_key == f"POST {upload_prefix}":
                    raw = event.get("body")
                    if (
                        event.get("isBase64Encoded") is True
                        or not isinstance(raw, str)
                        or len(raw.encode()) > 4096
                        or request.headers.get("content-type", "").partition(";")[0].strip().lower()
                        != "application/json"
                    ):
                        raise DebateRequestFailure("REQUEST_INVALID", 400)
                    payload = DebateAttachmentUploadRequest.model_validate(
                        json.loads(raw, object_pairs_hook=_unique_fields), strict=True
                    )
                    result = self._attachments.reserve(session.requester_key, payload, now)
                else:
                    _empty_body(event)
                    attachment_id = TypeAdapter(AttachmentId).validate_python(
                        request.path_parameters.get("attachmentId"), strict=True
                    )
                    if request.route_key == f"GET {upload_prefix}/{{attachmentId}}":
                        result = self._attachments.status(session.requester_key, attachment_id, now)
                    elif request.route_key == f"POST {upload_prefix}/{{attachmentId}}/complete":
                        result = self._attachments.complete(
                            session.requester_key, attachment_id, now
                        )
                    else:
                        return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
                self._service._still_authorized(token, session)
                return json_response(200, result)
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
                if (
                    not isinstance(data, dict)
                    or not {"requestId", "question"} <= set(data)
                    or not set(data) <= {"requestId", "question", "attachmentIds"}
                ):
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
        except AttachmentFailure as error:
            return error_response(error.status, error.code, request.request_id)
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
