"""HTTP API v2 controllers for authenticated status and prompt administration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from pydantic import ValidationError

from shittim_records.admin import (
    AdminAuthorizer,
    AdminFailure,
    AdminPromptService,
    PromptRevision,
    PromptRevisionSummary,
    PromptValues,
)
from shittim_records.admin_status import AdminStatusService
from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from shittim_records.contracts import (
    AdminPromptApplyRequest,
    AdminPromptApplyResponse,
    AdminPromptRevisionResponse,
    AdminPromptRevisionsResponse,
    AdminPromptRevisionSummary,
    AdminPromptRollbackRequest,
    AdminPromptsResponse,
    AdminPromptValues,
)
from shittim_records.http_api import Request, error_response, json_response, parse_request

MAX_ADMIN_BODY_BYTES = 64 * 1024


class AdminConfigHttpController:
    """Allow authenticated reads while restricting prompt changes to the administrator."""

    def __init__(
        self,
        *,
        authorizer: AdminAuthorizer,
        prompts: AdminPromptService,
    ) -> None:
        self._authorizer = authorizer
        self._prompts = prompts

    def handle(self, event: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
        request = parse_request(event)
        try:
            session = self._authorizer.authenticate(
                raw_session=request.cookies.get(SESSION_COOKIE_NAME),
                now=now,
            )
            if request.route_key == "GET /api/v1/admin/prompts":
                _reject_query(request)
                return json_response(
                    200, self._current_response().model_dump(by_alias=True, mode="json")
                )
            if request.route_key == "POST /api/v1/admin/prompts/apply":
                _reject_query(request)
                idempotency = self._authorize_write(request=request, session=session)
                payload = _body(event, AdminPromptApplyRequest)
                summary = self._prompts.apply(
                    base_revision=payload.base_revision,
                    prompts=_prompt_mapping(payload.prompts),
                    system_confirmation=payload.system_confirmation,
                    idempotency_hash=idempotency,
                    now=now,
                )
                return json_response(
                    200,
                    AdminPromptApplyResponse(
                        schema_version=1,
                        revision=summary.revision,
                        state="saved",
                    ).model_dump(by_alias=True, mode="json"),
                )
            if request.route_key == "GET /api/v1/admin/prompts/revisions":
                query = _admin_query(request.raw_query, allowed={"cursor", "limit"})
                page = self._prompts.list_revisions(
                    cursor=_optional_single(query, "cursor"),
                    limit=_limit(_optional_single(query, "limit")),
                )
                response = AdminPromptRevisionsResponse(
                    schema_version=1,
                    items=tuple(_summary_contract(item) for item in page.items),
                    next_cursor=page.next_cursor,
                )
                return json_response(200, response.model_dump(by_alias=True, mode="json"))
            if request.route_key == "GET /api/v1/admin/prompts/revisions/{revision}":
                _reject_query(request)
                summary, revision = self._prompts.get_revision(
                    request.path_parameters.get("revision", "")
                )
                response = _revision_contract(summary, revision)
                return json_response(200, response.model_dump(by_alias=True, mode="json"))
            if request.route_key == "POST /api/v1/admin/prompts/rollback":
                _reject_query(request)
                idempotency = self._authorize_write(request=request, session=session)
                payload = _body(event, AdminPromptRollbackRequest)
                summary = self._prompts.rollback(
                    base_revision=payload.base_revision,
                    source_revision=payload.source_revision,
                    system_confirmation=payload.system_confirmation,
                    idempotency_hash=idempotency,
                    now=now,
                )
                return json_response(
                    200,
                    AdminPromptApplyResponse(
                        schema_version=1,
                        revision=summary.revision,
                        state="saved",
                    ).model_dump(by_alias=True, mode="json"),
                )
            return error_response(404, "ROUTE_NOT_FOUND", request.request_id)
        except AdminFailure as error:
            return error_response(error.status, error.code, request.request_id)

    def _current_response(self) -> AdminPromptsResponse:
        current = self._prompts.get_current()
        manifest = None if current.revision is None else current.revision.manifest
        return AdminPromptsResponse(
            schema_version=1,
            mode=current.mode,
            active_revision=None if manifest is None else manifest.revision,
            created_at=None if manifest is None else manifest.created_at,
            action=None if manifest is None else manifest.action,
            prompts=_prompt_contract(current.prompts),
        )

    def _authorize_write(self, *, request: Request, session: Any) -> str:
        return self._authorizer.authorize_write(
            session=session,
            raw_csrf=request.cookies.get(CSRF_COOKIE_NAME),
            csrf_header=request.headers.get("x-csrf-token"),
            origin=request.headers.get("origin"),
            idempotency_key=request.headers.get("x-idempotency-key"),
        )


class AdminStatusHttpController:
    """Expose sanitized status snapshots to authenticated Records members."""

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
                _reject_query(request)
                response = self._status.get(now=now)
            elif request.route_key == "POST /api/v1/admin/status/refresh":
                _reject_query(request)
                if event.get("body") not in (None, ""):
                    raise AdminFailure("REQUEST_INVALID", 400)
                self._authorizer.authorize_status_refresh(
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


def _body(event: Mapping[str, Any], model: Any) -> Any:
    if event.get("isBase64Encoded") is True:
        raise AdminFailure("REQUEST_INVALID", 400)
    raw = event.get("body")
    if not isinstance(raw, str) or not raw or len(raw.encode()) > MAX_ADMIN_BODY_BYTES:
        raise AdminFailure("REQUEST_INVALID", 400)
    headers = event.get("headers") or {}
    content_type = ""
    if isinstance(headers, Mapping):
        content_type = str(
            next(
                (value for key, value in headers.items() if str(key).casefold() == "content-type"),
                "",
            )
        )
    if content_type.partition(";")[0].strip().casefold() != "application/json":
        raise AdminFailure("REQUEST_INVALID", 400)
    try:
        payload = json.loads(raw)
    except TypeError, ValueError, json.JSONDecodeError:
        raise AdminFailure("REQUEST_INVALID", 400) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise AdminFailure("REQUEST_INVALID", 400) from None


def _prompt_mapping(value: AdminPromptValues) -> dict[str, str]:
    return {
        "system": value.system,
        "moderator": value.moderator,
        "participant-a": value.participant_a,
        "participant-b": value.participant_b,
        "participant-c": value.participant_c,
    }


def _prompt_contract(value: PromptValues) -> AdminPromptValues:
    return AdminPromptValues(
        system=value.system,
        moderator=value.moderator,
        participant_a=value.participant_a,
        participant_b=value.participant_b,
        participant_c=value.participant_c,
    )


def _summary_contract(value: PromptRevisionSummary) -> AdminPromptRevisionSummary:
    return AdminPromptRevisionSummary(
        revision=value.revision,
        created_at=value.created_at,
        action=value.action,
        base_revision=value.base_revision,
        source_revision=value.source_revision,
        checksum=value.checksum,
    )


def _revision_contract(
    summary: PromptRevisionSummary,
    revision: PromptRevision,
) -> AdminPromptRevisionResponse:
    return AdminPromptRevisionResponse(
        schema_version=1,
        revision=summary.revision,
        created_at=summary.created_at,
        action=summary.action,
        base_revision=summary.base_revision,
        source_revision=summary.source_revision,
        checksum=summary.checksum,
        prompts=_prompt_contract(revision.prompts),
    )


def _reject_query(request: Request) -> None:
    if request.raw_query:
        raise AdminFailure("REQUEST_INVALID", 400)


def _admin_query(raw: str, *, allowed: set[str]) -> dict[str, list[str]]:
    try:
        query = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=5)
    except ValueError:
        raise AdminFailure("REQUEST_INVALID", 400) from None
    if not set(query).issubset(allowed):
        raise AdminFailure("REQUEST_INVALID", 400)
    return query


def _optional_single(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0]:
        raise AdminFailure("REQUEST_INVALID", 400)
    return values[0]


def _limit(value: str | None) -> int:
    if value is None:
        return 20
    try:
        parsed = int(value)
    except ValueError:
        raise AdminFailure("REQUEST_INVALID", 400) from None
    if str(parsed) != value or not 1 <= parsed <= 50:
        raise AdminFailure("REQUEST_INVALID", 400)
    return parsed
