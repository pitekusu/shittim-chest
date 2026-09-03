"""HTTP authorization and wire tests for the Memorial Lobby routes."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from shittim_records.auth import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, SessionRecord
from shittim_records.memorial import (
    MemorialFailure,
    MemorialMemory,
    MemorialMemorySummary,
    MemorialSnapshot,
    MemorialUploadTicket,
    ResolvedMemorialMemory,
)
from shittim_records.memorial_http import MemorialHttpController

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
REQUESTER_KEY = "a" * 43
UPLOAD_BODY = {
    "schemaVersion": 1,
    "expectedCycle": 1,
    "contentType": "image/png",
    "sizeBytes": 1024,
    "sha256": "b" * 64,
}
GENERATE_BODY = {
    "schemaVersion": 1,
    "expectedCycle": 1,
    "confirmation": "GENERATE MEMORIAL",
}
RESET_BODY = {
    "schemaVersion": 1,
    "expectedCycle": 1,
    "confirmation": "RESET AFFECTION",
}


def event(
    route: str,
    *,
    body: dict[str, Any] | None = None,
    query: str = "",
    path: dict[str, str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "routeKey": route,
        "requestContext": {"requestId": "opaque-request"},
        "rawQueryString": query,
        "cookies": [
            f"{SESSION_COOKIE_NAME}=session-token",
            f"{CSRF_COOKIE_NAME}=csrf-token",
        ],
        "headers": {
            "content-type": "application/json",
            "origin": "https://records.example.invalid",
            "x-csrf-token": "csrf-token",
            "x-idempotency-key": "idempotency-key-1",
        },
        "pathParameters": path or {},
    }
    if body is not None:
        value["body"] = json.dumps(body)
    return value


class Authorizer:
    def __init__(self, failure: MemorialFailure | None = None) -> None:
        self.failure = failure
        self.writes = 0

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord:
        assert raw_session == "session-token"
        assert now == NOW
        if self.failure is not None:
            raise self.failure
        return SessionRecord(
            requester_key=REQUESTER_KEY,
            display_name="質問者",
            avatar_asset_key=None,
            csrf_hash="private-csrf-hash",
            guild_verified_at=NOW.isoformat(),
            expires_at=int((NOW + timedelta(hours=1)).timestamp()),
        )

    def authorize_write(self, **kwargs: Any) -> str:
        assert kwargs["session"].requester_key == REQUESTER_KEY
        assert kwargs["raw_csrf"] == "csrf-token"
        assert kwargs["csrf_header"] == "csrf-token"
        assert kwargs["origin"] == "https://records.example.invalid"
        assert kwargs["idempotency_key"] == "idempotency-key-1"
        self.writes += 1
        return "c" * 64


def memory() -> MemorialMemory:
    return MemorialMemory(
        cycle=1,
        participant="participant-a",
        unlocked_at=NOW,
        generated_at=NOW + timedelta(minutes=2),
        image_asset_key="private/generated",
        narrative="質問者との大切な思い出です。",
    )


def ready_summary() -> MemorialMemorySummary:
    value = memory()
    return MemorialMemorySummary(
        cycle=value.cycle,
        participant=value.participant,
        unlocked_at=value.unlocked_at,
        generated_at=value.generated_at,
    )


def state(name: str, *, cycle: int = 1) -> MemorialSnapshot:
    ready = (ready_summary(),) if cycle > 1 else ()
    return MemorialSnapshot(
        requester_key=REQUESTER_KEY,
        state=cast(Any, name),
        cycle=cycle,
        reset_count=cycle - 1,
        unlocked_participant=None if name == "locked" else "participant-a",
        unlocked_at=None if name == "locked" else NOW,
        upload_ready=name == "unlocked",
        latest_ready_cycle=1 if ready else None,
        memories=ready,
    )


class Service:
    def get_state(self, *, requester_key: str) -> MemorialSnapshot:
        assert requester_key == REQUESTER_KEY
        return state("unlocked")

    def prepare_upload(self, **kwargs: Any) -> tuple[int, MemorialUploadTicket]:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["expected_cycle"] == 1
        assert kwargs["idempotency_hash"] == "c" * 64
        return (
            1,
            MemorialUploadTicket(
                upload_url="https://upload.example.invalid/",
                expires_at=NOW + timedelta(minutes=5),
                fields={
                    "key": "opaque/upload",
                    "Content-Type": "image/png",
                    "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex("b" * 64)).decode(),
                    "x-amz-algorithm": "AWS4-HMAC-SHA256",
                    "x-amz-credential": "credential/scope",
                    "x-amz-date": "20260903T010000Z",
                    "policy": "cG9saWN5",
                    "x-amz-signature": "d" * 64,
                },
            ),
        )

    def queue_generation(self, **kwargs: Any) -> MemorialSnapshot:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["expected_cycle"] == 1
        assert kwargs["confirmation"] == "GENERATE MEMORIAL"
        return state("queued")

    def get_memory(self, *, requester_key: str, cycle: int) -> ResolvedMemorialMemory:
        assert (requester_key, cycle) == (REQUESTER_KEY, 1)
        return ResolvedMemorialMemory(
            memory=memory(),
            image_url="https://media.example.invalid/memory.png",
        )

    def reset(self, **kwargs: Any) -> MemorialSnapshot:
        assert kwargs["requester_key"] == REQUESTER_KEY
        assert kwargs["expected_cycle"] == 1
        assert kwargs["confirmation"] == "RESET AFFECTION"
        return state("locked", cycle=2)


@pytest.mark.parametrize(
    ("route", "body", "path", "expected_status"),
    (
        ("GET /api/v1/memorial", None, None, 200),
        ("POST /api/v1/memorial/upload", UPLOAD_BODY, None, 200),
        ("POST /api/v1/memorial/generate", GENERATE_BODY, None, 202),
        ("GET /api/v1/memorial/memories/{cycle}", None, {"cycle": "1"}, 200),
        ("POST /api/v1/memorial/reset", RESET_BODY, None, 200),
    ),
)
def test_owner_routes_return_private_no_store_without_internal_identity(
    route: str,
    body: dict[str, Any] | None,
    path: dict[str, str] | None,
    expected_status: int,
) -> None:
    authorizer = Authorizer()
    controller = MemorialHttpController(
        authorizer=cast(Any, authorizer),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(event(route, body=body, path=path), now=NOW)

    assert response["statusCode"] == expected_status
    assert response["headers"]["Cache-Control"] == "private, no-store"
    assert response["headers"]["Content-Type"] == "application/json; charset=utf-8"
    for forbidden in (REQUESTER_KEY, "requesterKey", "ownerKey", "private/generated"):
        assert forbidden not in response["body"]
    assert authorizer.writes == (1 if route.startswith("POST") else 0)


def test_upload_response_is_a_strict_presigned_post_capability() -> None:
    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(
        event("POST /api/v1/memorial/upload", body=UPLOAD_BODY),
        now=NOW,
    )
    payload = json.loads(response["body"])

    assert payload["method"] == "POST"
    assert set(payload["fields"]) == {
        "key",
        "Content-Type",
        "x-amz-checksum-sha256",
        "x-amz-algorithm",
        "x-amz-credential",
        "x-amz-date",
        "policy",
        "x-amz-signature",
    }


@pytest.mark.parametrize(
    ("route", "body", "path"),
    (
        ("GET /api/v1/memorial", None, None),
        ("POST /api/v1/memorial/upload", UPLOAD_BODY, None),
        ("POST /api/v1/memorial/generate", GENERATE_BODY, None),
        ("GET /api/v1/memorial/memories/{cycle}", None, {"cycle": "1"}),
        ("POST /api/v1/memorial/reset", RESET_BODY, None),
    ),
)
def test_every_route_rejects_anonymous_session(
    route: str,
    body: dict[str, Any] | None,
    path: dict[str, str] | None,
) -> None:
    controller = MemorialHttpController(
        authorizer=cast(
            Any,
            Authorizer(MemorialFailure("AUTHENTICATION_REQUIRED", 401)),
        ),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(event(route, body=body, path=path), now=NOW)

    assert response["statusCode"] == 401
    assert json.loads(response["body"])["error"]["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda request: request.update(rawQueryString="owner=private"),
        lambda request: request.update(isBase64Encoded=True),
        lambda request: request["headers"].update({"content-type": "text/plain"}),
        lambda request: request.update(body=json.dumps({**UPLOAD_BODY, "requesterKey": "private"})),
        lambda request: request.update(pathParameters={"requesterId": "private"}),
    ),
)
def test_write_rejects_query_base64_wrong_media_unknown_body_and_owner_path(
    mutate: Any,
) -> None:
    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, Service()),
    )
    request = event("POST /api/v1/memorial/upload", body=UPLOAD_BODY)
    mutate(request)

    response = controller.handle(request, now=NOW)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"
    assert "private" not in response["body"]


@pytest.mark.parametrize(
    ("route", "body"),
    (
        (
            "POST /api/v1/memorial/upload",
            {**UPLOAD_BODY, "schemaVersion": True},
        ),
        (
            "POST /api/v1/memorial/upload",
            {**UPLOAD_BODY, "schemaVersion": 1.0},
        ),
        (
            "POST /api/v1/memorial/upload",
            {**UPLOAD_BODY, "sizeBytes": True},
        ),
        (
            "POST /api/v1/memorial/upload",
            {**UPLOAD_BODY, "sizeBytes": 1024.0},
        ),
        (
            "POST /api/v1/memorial/upload",
            {**UPLOAD_BODY, "expectedCycle": True},
        ),
        (
            "POST /api/v1/memorial/generate",
            {**GENERATE_BODY, "schemaVersion": 1.0},
        ),
        (
            "POST /api/v1/memorial/generate",
            {**GENERATE_BODY, "expectedCycle": 1.0},
        ),
        (
            "POST /api/v1/memorial/reset",
            {**RESET_BODY, "schemaVersion": True},
        ),
    ),
)
def test_write_rejects_coerced_boolean_and_float_integers(
    route: str,
    body: dict[str, Any],
) -> None:
    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(event(route, body=body), now=NOW)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"]["code"] == "REQUEST_INVALID"


@pytest.mark.parametrize("cycle", ("0", "01", "1000000001", "owner"))
def test_memory_route_rejects_noncanonical_cycle(cycle: str) -> None:
    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(
        event(
            "GET /api/v1/memorial/memories/{cycle}",
            path={"cycle": cycle},
        ),
        now=NOW,
    )

    assert response["statusCode"] == 400


@pytest.mark.parametrize(
    "request_event",
    (
        event("GET /api/v1/memorial", query="owner=private"),
        {**event("GET /api/v1/memorial"), "body": "{}"},
        event("GET /api/v1/memorial", path={"requesterId": "private"}),
        event(
            "GET /api/v1/memorial/memories/{cycle}",
            path={"cycle": "1", "requesterId": "private"},
        ),
    ),
)
def test_reads_reject_query_body_and_noncontract_path_fields(
    request_event: dict[str, Any],
) -> None:
    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, Service()),
    )

    response = controller.handle(request_event, now=NOW)

    assert response["statusCode"] == 400
    assert "private" not in response["body"]


def test_invalid_domain_snapshot_returns_content_free_503() -> None:
    class InvalidService(Service):
        def get_state(self, *, requester_key: str) -> Any:
            assert requester_key == REQUESTER_KEY
            return SimpleNamespace(
                state="ready",
                cycle=1,
                reset_count=0,
                unlocked_participant="participant-a",
                unlocked_at=NOW,
                upload_ready=False,
                latest_ready_cycle=None,
                memories=(),
            )

    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, InvalidService()),
    )

    response = controller.handle(event("GET /api/v1/memorial"), now=NOW)

    assert response["statusCode"] == 503
    assert json.loads(response["body"])["error"]["code"] == "MEMORIAL_STATE_INVALID"


@pytest.mark.parametrize(
    ("code", "message"),
    (
        (
            "MEMORIAL_RECOVERY_REQUIRED",
            "生成結果が残っています。もう一度メモリアルロビーを開放してください。",
        ),
        (
            "MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED",
            "画像生成の試行上限に達しました。親愛度をリセットして再度開放してください。",
        ),
    ),
)
def test_actionable_generation_conflicts_return_specific_messages(
    code: str,
    message: str,
) -> None:
    class FailedService(Service):
        def queue_generation(self, **kwargs: Any) -> MemorialSnapshot:
            del kwargs
            raise MemorialFailure(code, 409)

    controller = MemorialHttpController(
        authorizer=cast(Any, Authorizer()),
        memorial=cast(Any, FailedService()),
    )

    response = controller.handle(
        event("POST /api/v1/memorial/generate", body=GENERATE_BODY),
        now=NOW,
    )
    payload = json.loads(response["body"])

    assert response["statusCode"] == 409
    assert payload["error"]["code"] == code
    assert payload["error"]["message"] == message
