"""HTTP API v2 authorization and wire tests for ADMIN status routes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from shittim_records.admin import AdminFailure
from shittim_records.admin_http import AdminStatusHttpController
from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SessionRecord
from shittim_records.contracts import AdminStatusOverall, AdminStatusResponse, AdminStatusSection

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)


def event(route: str, *, query: str = "") -> dict[str, Any]:
    return {
        "routeKey": route,
        "requestContext": {"requestId": "opaque-request"},
        "rawQueryString": query,
        "cookies": [
            f"{SESSION_COOKIE_NAME}=session-token",
            f"{CSRF_COOKIE_NAME}=csrf-token",
        ],
        "headers": {
            "origin": "https://records.example.invalid",
            "x-csrf-token": "csrf-token",
            "x-idempotency-key": "idempotency-key-1",
        },
        "pathParameters": {},
    }


class Authorizer:
    def __init__(self, failure: AdminFailure | None = None) -> None:
        self.failure = failure
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
        assert kwargs["raw_csrf"] == "csrf-token"
        assert kwargs["csrf_header"] == "csrf-token"
        assert kwargs["origin"] == "https://records.example.invalid"
        assert kwargs["idempotency_key"] == "idempotency-key-1"
        self.writes += 1
        return "b" * 64


def status_response() -> AdminStatusResponse:
    services = (
        "ecs",
        "ecr",
        "inspector",
        "s3",
        "dynamodb",
        "lambda",
        "cloudfront",
        "sqs",
        "apigateway",
        "eventbridge",
        "cloudformation",
        "sns",
        "ssm",
        "cost_governance",
        "signer",
        "external",
    )
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


@pytest.mark.parametrize(
    "route",
    ("GET /api/v1/admin/status", "POST /api/v1/admin/status/refresh"),
)
def test_status_routes_authorize_and_return_sanitized_snapshot(route: str) -> None:
    authorizer = Authorizer()
    controller = AdminStatusHttpController(
        authorizer=cast(Any, authorizer),
        status=cast(Any, StatusService()),
    )

    response = controller.handle(event(route), now=NOW)

    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert authorizer.writes == (1 if route.startswith("POST") else 0)


@pytest.mark.parametrize(
    "status,code", ((401, "AUTHENTICATION_REQUIRED"), (403, "ADMIN_ACCESS_DENIED"))
)
@pytest.mark.parametrize("route", ("GET /api/v1/admin/status", "POST /api/v1/admin/status/refresh"))
def test_every_admin_route_rejects_unauthorized_sessions(
    route: str,
    status: int,
    code: str,
) -> None:
    controller = AdminStatusHttpController(
        authorizer=cast(Any, Authorizer(AdminFailure(code, status))),
        status=cast(Any, StatusService()),
    )

    response = controller.handle(event(route), now=NOW)

    assert response["statusCode"] == status
    assert json.loads(response["body"])["error"]["code"] == code


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
