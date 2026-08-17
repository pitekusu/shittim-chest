"""Content-free Lambda handler tests."""

from __future__ import annotations

import subprocess
import sys
from typing import Any, cast

import pytest

from shittim_records import lambda_handlers
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
                    "NewImage": {
                        "PK": {"S": "DEBATE#opaque"},
                        "record_type": {"S": "debate_meta"},
                        "current_phase": {"S": "completed"},
                    }
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


def test_projector_handler_returns_only_failed_event_identifier(monkeypatch: Any) -> None:
    service = FakeProjector(fail=True)
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))

    result = lambda_handlers.projector_handler(stream_event(), object())

    assert result == {"batchItemFailures": [{"itemIdentifier": "opaque-stream-event"}]}


def test_projector_handler_fails_closed_on_non_completed_event(monkeypatch: Any) -> None:
    service = FakeProjector()
    monkeypatch.setattr(lambda_handlers, "_PROJECTOR", cast(Any, service))
    event = cast(dict[str, Any], stream_event())
    event["Records"][0]["dynamodb"]["NewImage"]["current_phase"] = {"S": "failed"}

    result = lambda_handlers.projector_handler(event, object())

    assert result["batchItemFailures"] == [{"itemIdentifier": "opaque-stream-event"}]
    assert service.partitions == []


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
