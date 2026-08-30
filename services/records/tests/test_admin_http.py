"""HTTP API v2 authorization and wire tests for every ADMIN route."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from shittim_records.admin import (
    AdminFailure,
    PromptCurrent,
    PromptHistoryPage,
    PromptManifest,
    PromptRevision,
    PromptRevisionSummary,
    PromptValues,
)
from shittim_records.admin_http import AdminConfigHttpController, AdminStatusHttpController
from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SessionRecord
from shittim_records.contracts import AdminStatusOverall, AdminStatusResponse, AdminStatusSection

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
REVISION = "r01k3gqp6g00000000000000000"
SOURCE_REVISION = "r01k3gqp6g00000000000000001"
CHECKSUM = "a" * 64


def prompts() -> PromptValues:
    return PromptValues.from_mapping(
        {
            "system": "system",
            "moderator": "moderator",
            "participant-a": "a",
            "participant-b": "b",
            "participant-c": "c",
        }
    )


def summary(*, revision: str = REVISION) -> PromptRevisionSummary:
    return PromptRevisionSummary(
        revision=revision,
        created_at=NOW,
        action="publish",
        base_revision=None,
        source_revision=None,
        checksum=CHECKSUM,
    )


def event(
    route: str,
    *,
    body: dict[str, Any] | None = None,
    query: str = "",
    path: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        "content-type": "application/json",
        "origin": "https://records.example.invalid",
        "x-csrf-token": "csrf-token",
        "x-idempotency-key": "idempotency-key-1",
    }
    value: dict[str, Any] = {
        "routeKey": route,
        "requestContext": {"requestId": "opaque-request"},
        "rawQueryString": query,
        "cookies": [
            f"{SESSION_COOKIE_NAME}=session-token",
            f"{CSRF_COOKIE_NAME}=csrf-token",
        ],
        "headers": headers,
        "pathParameters": path or {},
    }
    if body is not None:
        value["body"] = json.dumps(body)
    return value


class Authorizer:
    def __init__(self, failure: AdminFailure | None = None, *, is_admin: bool = True) -> None:
        self.failure = failure
        self.is_admin = is_admin
        self.writes = 0

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord:
        assert raw_session == "session-token"
        assert now == NOW
        if self.failure is not None:
            raise self.failure
        return SessionRecord(
            requester_key="opaque",
            display_name="private",
            avatar_asset_key=None,
            csrf_hash="opaque-csrf",
            guild_verified_at=NOW.isoformat(),
            expires_at=int((NOW + timedelta(hours=1)).timestamp()),
        )

    def authorize_write(self, **kwargs: Any) -> str:
        if not self.is_admin:
            raise AdminFailure("ADMIN_ACCESS_DENIED", 403)
        return self._authorize_action(**kwargs)

    def authorize_status_refresh(self, **kwargs: Any) -> str:
        return self._authorize_action(**kwargs)

    def _authorize_action(self, **kwargs: Any) -> str:
        assert kwargs["raw_csrf"] == "csrf-token"
        assert kwargs["csrf_header"] == "csrf-token"
        assert kwargs["origin"] == "https://records.example.invalid"
        assert kwargs["idempotency_key"] == "idempotency-key-1"
        self.writes += 1
        return "b" * 64


class PromptService:
    def get_current(self) -> PromptCurrent:
        return PromptCurrent(mode="legacy", revision=None, prompts=prompts())

    def apply(self, **kwargs: Any) -> PromptRevisionSummary:
        assert kwargs["idempotency_hash"] == "b" * 64
        return summary()

    def list_revisions(self, **kwargs: Any) -> PromptHistoryPage:
        assert kwargs == {"cursor": None, "limit": 20}
        return PromptHistoryPage(items=(summary(),), next_cursor=None)

    def get_revision(self, revision: str) -> tuple[PromptRevisionSummary, PromptRevision]:
        assert revision == REVISION
        value = prompts()
        metadata = summary()
        return (
            metadata,
            PromptRevision(
                manifest=PromptManifest(
                    revision=REVISION,
                    created_at=NOW,
                    action="publish",
                    base_revision=None,
                    checksums=value.checksums(),
                ),
                prompts=value,
            ),
        )

    def rollback(self, **kwargs: Any) -> PromptRevisionSummary:
        assert kwargs["source_revision"] == SOURCE_REVISION
        assert kwargs["system_confirmation"] is None
        return summary()


def status_response() -> AdminStatusResponse:
    services = ("ecs", "ecr", "inspector", "s3", "dynamodb", "lambda", "cloudfront", "sqs")
    return AdminStatusResponse(
        schema_version=1,
        generated_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        stale=False,
        overall=AdminStatusOverall(
            state="healthy",
            critical_alarms=0,
            warning_alarms=0,
            partial=False,
        ),
        sections=tuple(
            AdminStatusSection(
                service=cast(Any, service),
                state="healthy",
                summary="正常です。",
                metrics=(),
            )
            for service in services
        ),
    )


class StatusService:
    def get(self, *, now: datetime) -> AdminStatusResponse:
        assert now == NOW
        return status_response()

    def refresh(self, *, now: datetime) -> AdminStatusResponse:
        assert now == NOW
        return status_response()


APPLY_BODY = {
    "schemaVersion": 1,
    "baseRevision": None,
    "prompts": {
        "system": "system",
        "moderator": "moderator",
        "participantA": "a",
        "participantB": "b",
        "participantC": "c",
    },
    "systemConfirmation": None,
}
ROLLBACK_BODY = {
    "schemaVersion": 1,
    "baseRevision": REVISION,
    "sourceRevision": SOURCE_REVISION,
    "systemConfirmation": None,
}


@pytest.mark.parametrize(
    ("route", "body", "path"),
    [
        ("GET /api/v1/admin/prompts", None, None),
        ("POST /api/v1/admin/prompts/apply", APPLY_BODY, None),
        ("GET /api/v1/admin/prompts/revisions", None, None),
        (
            "GET /api/v1/admin/prompts/revisions/{revision}",
            None,
            {"revision": REVISION},
        ),
        ("POST /api/v1/admin/prompts/rollback", ROLLBACK_BODY, None),
    ],
)
def test_config_routes_authorize_and_return_strict_json(
    route: str,
    body: dict[str, Any] | None,
    path: dict[str, str] | None,
) -> None:
    authorizer = Authorizer()
    controller = AdminConfigHttpController(
        authorizer=cast(Any, authorizer),
        prompts=cast(Any, PromptService()),
    )

    response = controller.handle(event(route, body=body, path=path), now=NOW)

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert "private" not in response["body"]
    assert authorizer.writes == (1 if route.startswith("POST") else 0)


@pytest.mark.parametrize(
    "route",
    ("GET /api/v1/admin/status", "POST /api/v1/admin/status/refresh"),
)
@pytest.mark.parametrize("is_admin", (True, False))
def test_status_routes_authorize_and_return_sanitized_snapshot(
    route: str,
    is_admin: bool,
) -> None:
    authorizer = Authorizer(is_admin=is_admin)
    controller = AdminStatusHttpController(
        authorizer=cast(Any, authorizer),
        status=cast(Any, StatusService()),
    )

    response = controller.handle(event(route), now=NOW)

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert authorizer.writes == (1 if route.startswith("POST") else 0)


@pytest.mark.parametrize(
    "route",
    (
        "GET /api/v1/admin/prompts",
        "POST /api/v1/admin/prompts/apply",
        "GET /api/v1/admin/prompts/revisions",
        "GET /api/v1/admin/prompts/revisions/{revision}",
        "POST /api/v1/admin/prompts/rollback",
        "GET /api/v1/admin/status",
        "POST /api/v1/admin/status/refresh",
    ),
)
def test_every_admin_route_rejects_anonymous_sessions(route: str) -> None:
    authorizer = Authorizer(AdminFailure("AUTHENTICATION_REQUIRED", 401))
    if "status" in route:
        controller: Any = AdminStatusHttpController(
            authorizer=cast(Any, authorizer),
            status=cast(Any, StatusService()),
        )
    else:
        controller = AdminConfigHttpController(
            authorizer=cast(Any, authorizer),
            prompts=cast(Any, PromptService()),
        )
    body = (
        APPLY_BODY
        if route.endswith("apply")
        else ROLLBACK_BODY
        if route.endswith("rollback")
        else None
    )

    response = controller.handle(
        event(route, body=body, path={"revision": REVISION}),
        now=NOW,
    )

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize(
    ("route", "path"),
    (
        ("GET /api/v1/admin/prompts", None),
        ("GET /api/v1/admin/prompts/revisions", None),
        (
            "GET /api/v1/admin/prompts/revisions/{revision}",
            {"revision": REVISION},
        ),
    ),
)
def test_authenticated_non_admin_can_read_prompt_configuration(
    route: str,
    path: dict[str, str] | None,
) -> None:
    controller = AdminConfigHttpController(
        authorizer=cast(Any, Authorizer(is_admin=False)),
        prompts=cast(Any, PromptService()),
    )

    response = controller.handle(event(route, path=path), now=NOW)

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    ("route", "body"),
    (
        ("POST /api/v1/admin/prompts/apply", APPLY_BODY),
        ("POST /api/v1/admin/prompts/rollback", ROLLBACK_BODY),
    ),
)
def test_authenticated_non_admin_cannot_change_prompts(
    route: str,
    body: dict[str, Any],
) -> None:
    controller = AdminConfigHttpController(
        authorizer=cast(Any, Authorizer(is_admin=False)),
        prompts=cast(Any, PromptService()),
    )

    response = controller.handle(event(route, body=body), now=NOW)

    assert response["statusCode"] == 403
    error = json.loads(response["body"])["error"]
    assert error["code"] == "ADMIN_ACCESS_DENIED"
    assert error["message"] == "この操作を実行する権限がありません。"


def test_invalid_prompt_body_returns_content_free_error() -> None:
    controller = AdminConfigHttpController(
        authorizer=cast(Any, Authorizer()),
        prompts=cast(Any, PromptService()),
    )
    private_prompt = "private-prompt-value"
    invalid_prompts = dict(cast(dict[str, str], APPLY_BODY["prompts"]))
    invalid_prompts["system"] = private_prompt
    invalid = {**APPLY_BODY, "prompts": invalid_prompts}
    invalid["unexpected"] = private_prompt

    response = controller.handle(
        event("POST /api/v1/admin/prompts/apply", body=invalid),
        now=NOW,
    )

    assert response["statusCode"] == 400
    assert private_prompt not in response["body"]
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


@pytest.mark.parametrize(
    ("route", "query", "body", "path"),
    [
        ("GET /api/v1/admin/prompts", "unexpected=1", None, None),
        ("POST /api/v1/admin/prompts/apply", "unexpected=1", APPLY_BODY, None),
        ("GET /api/v1/admin/prompts/revisions", "limit=1&limit=2", None, None),
        (
            "GET /api/v1/admin/prompts/revisions/{revision}",
            "unexpected=1",
            None,
            {"revision": REVISION},
        ),
        ("POST /api/v1/admin/prompts/rollback", "unexpected=1", ROLLBACK_BODY, None),
    ],
)
def test_config_routes_reject_unknown_or_duplicate_query_values(
    route: str,
    query: str,
    body: dict[str, Any] | None,
    path: dict[str, str] | None,
) -> None:
    controller = AdminConfigHttpController(
        authorizer=cast(Any, Authorizer()),
        prompts=cast(Any, PromptService()),
    )

    response = controller.handle(event(route, body=body, query=query, path=path), now=NOW)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


@pytest.mark.parametrize("invalid_body", ["base64", "oversized"])
def test_prompt_writes_reject_base64_and_bodies_over_64_kib(invalid_body: str) -> None:
    controller = AdminConfigHttpController(
        authorizer=cast(Any, Authorizer()),
        prompts=cast(Any, PromptService()),
    )
    request = event("POST /api/v1/admin/prompts/apply", body=APPLY_BODY)
    if invalid_body == "base64":
        request["isBase64Encoded"] = True
    else:
        request["body"] = json.dumps({"padding": "x" * (64 * 1024)})

    response = controller.handle(request, now=NOW)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


def test_invalid_status_snapshot_returns_content_free_503() -> None:
    private_value = "private-provider-input"

    class InvalidStatusService(StatusService):
        def get(self, *, now: datetime) -> AdminStatusResponse:
            del now
            return AdminStatusResponse.model_validate(
                {
                    "schemaVersion": 1,
                    "generatedAt": private_value,
                    "expiresAt": private_value,
                    "stale": False,
                    "overall": {},
                    "sections": [],
                }
            )

    controller = AdminStatusHttpController(
        authorizer=cast(Any, Authorizer()),
        status=cast(Any, InvalidStatusService()),
    )

    response = controller.handle(event("GET /api/v1/admin/status"), now=NOW)

    assert response["statusCode"] == 503
    assert private_value not in response["body"]
    assert json.loads(response["body"])["error"]["code"] == "ADMIN_STATUS_INVALID"
