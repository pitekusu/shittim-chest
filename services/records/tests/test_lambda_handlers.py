"""Content-free Lambda handler tests."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError

from shittim_records import lambda_handlers
from shittim_records.costs import (
    CollectionSummary,
    CostCollectionFailed,
    CostProviderUnavailable,
)
from shittim_records.projector import BackfillResult, ProjectionResult


def test_lambda_handlers_import_without_boto3_type_stubs() -> None:
    script = """
import builtins

original_import = builtins.__import__


def reject_type_stubs(name, globals=None, locals=None, fromlist=(), level=0):
    if name.startswith("mypy_boto3_"):
        raise ModuleNotFoundError(name)
    return original_import(name, globals, locals, fromlist, level)


builtins.__import__ = reject_type_stubs
import shittim_records.lambda_handlers
"""

    result = subprocess.run(  # noqa: S603 - fixed interpreter and local script.
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


class FakeProjector:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.partitions: list[str] = []

    def project_partition(self, partition_key: str, *, now: object) -> ProjectionResult:
        del now
        self.partitions.append(partition_key)
        if self.fail:
            raise ValueError("invalid source")
        return ProjectionResult(created=True)


class FakeBackfill:
    def run_page(self, *, apply: bool, now: object, page_limit: int) -> BackfillResult:
        del now
        assert apply is False
        assert page_limit == 25
        return BackfillResult(
            candidates=2,
            validated=2,
            projected=0,
            skipped=0,
            complete=False,
        )


class FakeRanking:
    def refresh(self, *, now: object) -> object:
        del now
        return SimpleNamespace(
            archive_count=54,
            wins=(object(), object(), object()),
            requests=(object(), object()),
        )


class FakeCosts:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.modes: list[str] = []

    def refresh(self, *, mode: str, now: object) -> tuple[CollectionSummary, ...]:
        del now
        self.modes.append(mode)
        if self.fail:
            raise CostCollectionFailed(
                summaries=(CollectionSummary("FRANKFURTER", 1, 7, True),),
                failures=(CostProviderUnavailable("AWS", "provider_unavailable"),),
            )
        source = "OPENAI" if mode == "openai" else "AWS"
        return (CollectionSummary(cast(Any, source), 2, 60, True),)


class FakeHttpController:
    def handle(self, event: object, *, now: object) -> dict[str, object]:
        del now
        return {"statusCode": 200, "event": event}


def stream_event() -> dict[str, object]:
    return {
        "Records": [
            {
                "eventID": "opaque-stream-event",
                "eventName": "MODIFY",
                "dynamodb": {
                    "SequenceNumber": "opaque-stream-sequence",
                    "NewImage": {
                        "PK": {"S": "DEBATE#opaque"},
                        "record_type": {"S": "debate_meta"},
                        "current_phase": {"S": "completed"},
                    },
                },
            }
        ]
    }


def test_projector_handler_returns_empty_partial_failure_list(monkeypatch: Any) -> None:
    service = FakeProjector()
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))

    result = lambda_handlers.projector_handler(stream_event(), object())

    assert result == {"batchItemFailures": []}
    assert service.partitions == ["DEBATE#opaque"]


def test_projector_handler_returns_only_failed_stream_sequence(monkeypatch: Any) -> None:
    service = FakeProjector(fail=True)
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))

    result = lambda_handlers.projector_handler(stream_event(), object())

    assert result == {"batchItemFailures": [{"itemIdentifier": "opaque-stream-sequence"}]}


def test_projector_handler_fails_closed_on_non_completed_event(monkeypatch: Any) -> None:
    service = FakeProjector()
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))
    event = cast(dict[str, Any], stream_event())
    event["Records"][0]["dynamodb"]["NewImage"]["current_phase"] = {"S": "failed"}

    result = lambda_handlers.projector_handler(event, object())

    assert result["batchItemFailures"] == [{"itemIdentifier": "opaque-stream-sequence"}]
    assert service.partitions == []


def test_projector_handler_rejects_missing_stream_sequence(monkeypatch: Any) -> None:
    service = FakeProjector(fail=True)
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))
    event = cast(dict[str, Any], stream_event())
    del event["Records"][0]["dynamodb"]["SequenceNumber"]

    with pytest.raises(ValueError, match="sequence number"):
        lambda_handlers.projector_handler(event, object())


def test_projection_failure_fields_keep_only_content_free_client_codes() -> None:
    error = ClientError(
        {
            "Error": {"Code": "AccessDeniedException", "Message": "private detail"},
            "CancellationReasons": [
                {"Code": "ConditionalCheckFailed", "Message": "private item"},
                {"Code": "None"},
            ],
        },
        "TransactWriteItems",
    )

    fields = lambda_handlers._projection_failure_fields(error)

    assert fields == {
        "error_type": "ClientError",
        "client_error_code": "AccessDeniedException",
        "cancellation_reason_codes": ["ConditionalCheckFailed", "None"],
    }
    assert "private" not in str(fields)


def test_backfill_handler_returns_only_content_free_counts(monkeypatch: Any) -> None:
    monkeypatch.setattr(lambda_handlers, "_BACKFILL", cast(Any, FakeBackfill()))

    result = lambda_handlers.backfill_handler(
        {"mode": "dry-run", "page_limit": 25},
        object(),
    )

    assert result == {
        "mode": "dry-run",
        "candidates": 2,
        "validated": 2,
        "projected": 0,
        "skipped": 0,
        "complete": False,
    }


def test_backfill_handler_rejects_boolean_page_limit() -> None:
    with pytest.raises(ValueError, match="integer"):
        lambda_handlers.backfill_handler(
            {"mode": "dry-run", "page_limit": True},
            object(),
        )


def test_ranking_handler_returns_only_content_free_counts(monkeypatch: Any) -> None:
    monkeypatch.setattr(lambda_handlers, "_RANKING", cast(Any, FakeRanking()))

    result = lambda_handlers.ranking_handler({}, object())

    assert result == {"archive_count": 54, "win_entries": 3, "request_entries": 2}


def test_cost_handler_returns_only_content_free_counts(monkeypatch: Any) -> None:
    service = FakeCosts()
    monkeypatch.setattr(lambda_handlers, "_COSTS", cast(Any, service))

    result = lambda_handlers.cost_handler({"mode": "openai"}, object())

    assert result == {
        "mode": "openai",
        "sources": 1,
        "windows": 2,
        "days": 60,
        "complete": True,
    }
    assert service.modes == ["openai"]


def test_cost_handler_preserves_independent_success_then_raises(monkeypatch: Any) -> None:
    service = FakeCosts(fail=True)
    monkeypatch.setattr(lambda_handlers, "_COSTS", cast(Any, service))

    with pytest.raises(CostCollectionFailed):
        lambda_handlers.cost_handler({"mode": "aws_fx"}, object())

    assert service.modes == ["aws_fx"]


def test_cost_handler_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        lambda_handlers.cost_handler({"mode": "private-identifier"}, object())


def test_inspector_translation_handler_returns_only_content_free_counts(
    monkeypatch: Any,
) -> None:
    service = SimpleNamespace(
        refresh=lambda *, now: SimpleNamespace(
            discovered=54,
            cached=4,
            translated=50,
            remaining=0,
        )
    )
    monkeypatch.setattr(lambda_handlers, "_INSPECTOR_TRANSLATIONS", cast(Any, service))

    result = lambda_handlers.inspector_translation_handler({}, object())

    assert result == {
        "discovered": 54,
        "cached": 4,
        "translated": 50,
        "remaining": 0,
    }


def test_auth_and_read_handlers_delegate_without_logging_request_content(monkeypatch: Any) -> None:
    controller = cast(Any, FakeHttpController())
    monkeypatch.setattr(lambda_handlers, "_AUTH_CONTROLLER", controller)
    monkeypatch.setattr(lambda_handlers, "_READ_CONTROLLER", controller)
    event = {"routeKey": "GET /api/v1/session"}

    assert lambda_handlers.auth_handler(event, object()) == {
        "statusCode": 200,
        "event": event,
    }
    assert lambda_handlers.read_handler(event, object()) == {
        "statusCode": 200,
        "event": event,
    }


@pytest.mark.parametrize(
    ("factory_name", "handler_name", "expected_code"),
    (
        ("_auth_controller", "auth_handler", "RECORDS_UNAVAILABLE"),
        (
            "_admin_status_controller",
            "admin_status_handler",
            "ADMIN_STATUS_UNAVAILABLE",
        ),
    ),
)
def test_http_handler_controller_validation_failure_is_content_free(
    monkeypatch: Any,
    caplog: pytest.LogCaptureFixture,
    factory_name: str,
    handler_name: str,
    expected_code: str,
) -> None:
    private_user_id = "123456789" + "01234567"

    class PrivateConfiguration(BaseModel):
        admin_id: Annotated[str, Field(max_length=1)]

    with pytest.raises(ValidationError) as validation:
        PrivateConfiguration.model_validate({"admin_id": private_user_id})

    def fail() -> None:
        raise validation.value

    monkeypatch.setattr(lambda_handlers, factory_name, fail)
    handler = getattr(lambda_handlers, handler_name)

    response = handler(
        {
            "routeKey": "GET /api/v1/admin/status",
            "requestContext": {"requestId": "opaque-request"},
        },
        object(),
    )

    assert response["statusCode"] == 503
    assert expected_code in response["body"]
    assert private_user_id not in response["body"]
    assert private_user_id not in caplog.text


def test_s3_presigning_client_uses_the_lambda_region_endpoint(monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    client = object()

    def fake_client(service: str, **kwargs: Any) -> object:
        calls.append((service, kwargs))
        return client

    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setattr(lambda_handlers.boto3, "client", fake_client)

    assert lambda_handlers._regional_s3_client() is client
    assert calls == [
        (
            "s3",
            {
                "region_name": "ap-northeast-1",
                "endpoint_url": "https://s3.ap-northeast-1.amazonaws.com",
                "config": lambda_handlers.S3_SDK_CONFIG,
            },
        )
    ]


def test_admin_status_s3_client_allows_bucket_region_redirects(monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    client = object()

    def fake_client(service: str, **kwargs: Any) -> object:
        calls.append((service, kwargs))
        return client

    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setattr(lambda_handlers.boto3, "client", fake_client)

    assert lambda_handlers._admin_status_s3_client() is client
    assert calls == [
        (
            "s3",
            {
                "region_name": "ap-northeast-1",
                "config": lambda_handlers.S3_SDK_CONFIG,
            },
        )
    ]
