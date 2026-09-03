"""Authenticated owner-only HTTP boundary for the Memorial Lobby."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from shittim_records.contracts import (
    MemorialGenerateRequest,
    MemorialImage,
    MemorialMemoryResponse,
    MemorialMemorySummary,
    MemorialResetRequest,
    MemorialStateResponse,
    MemorialUploadFields,
    MemorialUploadRequest,
    MemorialUploadResponse,
)
from shittim_records.http_api import Request, error_response, json_response, parse_request
from shittim_records.memorial import (
    MemorialAuthorizer,
    MemorialFailure,
    MemorialService,
    MemorialSnapshot,
    MemorialUploadTicket,
    ResolvedMemorialMemory,
)

MAX_MEMORIAL_BODY_BYTES = 16 * 1024
_CYCLE = re.compile(r"^[1-9][0-9]{0,8}$")
_PARTICIPANT_NAMES = {
    "participant-a": "アロナ",
    "participant-b": "プラナ",
    "participant-c": "安倍晋三AI",
}


class MemorialHttpController:
    """Bind every Memorial operation to the authenticated session requester."""

    def __init__(
        self,
        *,
        authorizer: MemorialAuthorizer,
        memorial: MemorialService,
    ) -> None:
        self._authorizer = authorizer
        self._memorial = memorial

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        try:
            session = self._authorizer.authenticate(
                raw_session=request.cookies.get(SESSION_COOKIE_NAME),
                now=now,
            )
            if request.route_key == "GET /api/v1/memorial":
                _reject_read_shape(event=event, request=request, path_names=set())
                result = self._memorial.get_state(requester_key=session.requester_key)
                return _state_response(result)
            if request.route_key == "POST /api/v1/memorial/upload":
                _reject_query_and_path(request, path_names=set())
                idempotency_hash = self._authorize_write(request=request, session=session)
                payload = _body(event, request=request, model=MemorialUploadRequest)
                cycle, ticket = self._memorial.prepare_upload(
                    requester_key=session.requester_key,
                    expected_cycle=payload.expected_cycle,
                    content_type=payload.content_type,
                    size_bytes=payload.size_bytes,
                    sha256=payload.sha256,
                    idempotency_hash=idempotency_hash,
                    now=now,
                )
                return _upload_response(cycle=cycle, ticket=ticket)
            if request.route_key == "POST /api/v1/memorial/generate":
                _reject_query_and_path(request, path_names=set())
                idempotency_hash = self._authorize_write(request=request, session=session)
                payload = _body(event, request=request, model=MemorialGenerateRequest)
                result = self._memorial.queue_generation(
                    requester_key=session.requester_key,
                    expected_cycle=payload.expected_cycle,
                    confirmation=payload.confirmation,
                    idempotency_hash=idempotency_hash,
                    now=now,
                )
                return _state_response(result, status=202)
            if request.route_key == "GET /api/v1/memorial/memories/{cycle}":
                _reject_read_shape(event=event, request=request, path_names={"cycle"})
                result = self._memorial.get_memory(
                    requester_key=session.requester_key,
                    cycle=_cycle(request.path_parameters.get("cycle", "")),
                )
                return _memory_response(result)
            if request.route_key == "POST /api/v1/memorial/reset":
                _reject_query_and_path(request, path_names=set())
                idempotency_hash = self._authorize_write(request=request, session=session)
                payload = _body(event, request=request, model=MemorialResetRequest)
                result = self._memorial.reset(
                    requester_key=session.requester_key,
                    expected_cycle=payload.expected_cycle,
                    confirmation=payload.confirmation,
                    idempotency_hash=idempotency_hash,
                    now=now,
                )
                return _state_response(result)
            return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
        except MemorialFailure as error:
            return error_response(error.status, error.code, request.request_id)
        except ValidationError, ValueError:
            return error_response(503, "MEMORIAL_STATE_INVALID", request.request_id)

    def _authorize_write(self, *, request: Request, session: Any) -> str:
        return self._authorizer.authorize_write(
            session=session,
            raw_csrf=request.cookies.get(CSRF_COOKIE_NAME),
            csrf_header=request.headers.get("x-csrf-token"),
            origin=request.headers.get("origin"),
            idempotency_key=request.headers.get("x-idempotency-key"),
        )


def _state_response(snapshot: MemorialSnapshot, *, status: int = 200) -> dict[str, Any]:
    payload = MemorialStateResponse(
        schema_version=1,
        state=snapshot.state,
        cycle=snapshot.cycle,
        reset_count=snapshot.reset_count,
        unlocked_participant=snapshot.unlocked_participant,
        unlocked_at=snapshot.unlocked_at,
        upload_ready=snapshot.upload_ready,
        latest_ready_cycle=snapshot.latest_ready_cycle,
        memories=tuple(
            MemorialMemorySummary(
                cycle=memory.cycle,
                participant=memory.participant,
                unlocked_at=memory.unlocked_at,
                generated_at=memory.generated_at,
            )
            for memory in snapshot.memories
        ),
    )
    return json_response(status, payload.model_dump(by_alias=True, mode="json"))


def _upload_response(*, cycle: int, ticket: MemorialUploadTicket) -> dict[str, Any]:
    payload = MemorialUploadResponse(
        schema_version=1,
        cycle=cycle,
        method="POST",
        upload_url=ticket.upload_url,
        expires_at=ticket.expires_at,
        fields=MemorialUploadFields.model_validate(dict(ticket.fields)),
    )
    return json_response(
        200,
        payload.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def _memory_response(result: ResolvedMemorialMemory) -> dict[str, Any]:
    memory = result.memory
    payload = MemorialMemoryResponse(
        schema_version=1,
        cycle=memory.cycle,
        participant=memory.participant,
        unlocked_at=memory.unlocked_at,
        generated_at=memory.generated_at,
        image=MemorialImage(
            url=result.image_url,
            width=memory.width,
            height=memory.height,
            alt=f"{_PARTICIPANT_NAMES[memory.participant]}とのメモリアルロビー",
        ),
        narrative=memory.narrative,
    )
    return json_response(200, payload.model_dump(by_alias=True, mode="json"))


def _body(event: Mapping[str, Any], *, request: Request, model: type[BaseModel]) -> Any:
    if event.get("isBase64Encoded") is True:
        raise MemorialFailure("REQUEST_INVALID", 400)
    raw = event.get("body")
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_MEMORIAL_BODY_BYTES:
        raise MemorialFailure("REQUEST_INVALID", 400)
    content_type = request.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().casefold() != "application/json":
        raise MemorialFailure("REQUEST_INVALID", 400)
    try:
        payload = json.loads(raw)
        return model.model_validate(payload, strict=True)
    except TypeError, ValueError, json.JSONDecodeError, ValidationError:
        raise MemorialFailure("REQUEST_INVALID", 400) from None


def _reject_read_shape(
    *,
    event: Mapping[str, Any],
    request: Request,
    path_names: set[str],
) -> None:
    _reject_query_and_path(request, path_names=path_names)
    if event.get("isBase64Encoded") is True or event.get("body") not in (None, ""):
        raise MemorialFailure("REQUEST_INVALID", 400)


def _reject_query_and_path(request: Request, *, path_names: set[str]) -> None:
    if request.raw_query or set(request.path_parameters) != path_names:
        raise MemorialFailure("REQUEST_INVALID", 400)


def _cycle(value: str) -> int:
    if _CYCLE.fullmatch(value) is None:
        raise MemorialFailure("REQUEST_INVALID", 400)
    parsed = int(value)
    if parsed > 1_000_000_000:
        raise MemorialFailure("REQUEST_INVALID", 400)
    return parsed
