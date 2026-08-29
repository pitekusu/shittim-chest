"""Content-free Lambda boundary tests for runtime reconciliation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

import shittim_chest.lambda_handlers.runtime_reconciler as handler_module
from shittim_chest.application.ports import StatusTriggerUnavailable
from shittim_chest.application.runtime_reconciler import RuntimeReconciliationReport
from shittim_chest.application.scale_to_zero import RuntimeStatus
from shittim_chest.lambda_handlers.runtime_reconciler import (
    RuntimeReconcilerInvocationError,
    RuntimeReconcilerLambda,
)
from shittim_chest.runtime.operational_metrics import CloudWatchEmfMetrics

NOW = datetime(2026, 7, 26, 1, 2, 3, tzinfo=UTC)


class FakeReconciler:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def reconcile(self) -> RuntimeReconciliationReport:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return RuntimeReconciliationReport(
            observed_at=NOW,
            terminal_failed=1,
            wake_candidates=2,
            startup_timed_out=3,
            startup_recovered=4,
            status_publications_triggered=5,
            conditional_conflicts=6,
            runtime_status=RuntimeStatus.BUSY,
            runtime_desired_count=1,
            ecs_running_count=1,
            ingress_pending=7,
            outbox_pending=8,
            ecs_observed=True,
            ecs_scaled_up=True,
            ecs_scaled_down=True,
            runtime_entered_idle=True,
            runtime_stopped=True,
            runtime_reconciled=True,
        )


class Context:
    aws_request_id = "request-id"


class _RaisingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record
        raise RuntimeError("private provider detail")


@pytest.mark.parametrize(
    "event",
    [
        {"schema_version": 1, "trigger": "scheduled"},
        {"schema_version": 1, "interaction_id": "123456789"},
    ],
)
def test_handler_accepts_only_content_free_scheduled_or_hint_events(
    event: dict[str, object],
) -> None:
    reconciler = FakeReconciler()
    handler = RuntimeReconcilerLambda(reconciler=reconciler)

    result = handler.handle(event)

    assert reconciler.calls == 1
    assert result == {
        "conditional_conflicts": 6,
        "ecs_pending_count": 0,
        "ecs_running_count": 1,
        "ecs_observed": True,
        "ecs_scaled_down": True,
        "ecs_scaled_up": True,
        "observed_at": "2026-07-26T01:02:03Z",
        "ingress_pending": 7,
        "outbox_pending": 8,
        "runtime_desired_count": 1,
        "runtime_reconciled": True,
        "runtime_entered_idle": True,
        "runtime_status": "busy",
        "runtime_stopped": True,
        "startup_recovered": 4,
        "startup_timed_out": 3,
        "status_publications_triggered": 5,
        "terminal_failed": 1,
        "wake_candidates": 2,
    }
    assert "123456789" not in str(result)


def test_handler_emits_bounded_reconciler_metrics_without_input_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = FakeReconciler()
    logger = logging.getLogger("test-runtime-reconciler-emf")
    metrics = CloudWatchEmfMetrics(logger=logger, environment="production")
    handler = RuntimeReconcilerLambda(reconciler=reconciler, metrics=metrics)

    with caplog.at_level(logging.INFO, logger=logger.name):
        handler.handle({"schema_version": 1, "interaction_id": "123456789"})

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["Service"] == "reconciler"
    assert payload["RuntimeStateCode"] == 4
    assert payload["RuntimeDesiredCount"] == 1
    assert payload["EcsRunningCount"] == 1
    assert payload["IngressPending"] == 7
    assert payload["OutboxPending"] == 8
    assert payload["ReconcilerFailed"] == 0
    assert payload["StatusPublishFailed"] == 0
    assert "123456789" not in caplog.records[-1].getMessage()


def test_handler_emits_status_failure_without_provider_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = FakeReconciler()
    reconciler.error = StatusTriggerUnavailable()
    logger = logging.getLogger("test-runtime-reconciler-emf-failure")
    metrics = CloudWatchEmfMetrics(logger=logger, environment="production")
    handler = RuntimeReconcilerLambda(reconciler=reconciler, metrics=metrics)

    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        pytest.raises(StatusTriggerUnavailable),
    ):
        handler.handle({"schema_version": 1, "trigger": "scheduled"})

    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["ReconcilerFailed"] == 1
    assert payload["StatusPublishFailed"] == 1
    assert "provider" not in caplog.records[-1].getMessage().lower()


def test_handler_does_not_retry_reconciliation_when_metric_sink_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = FakeReconciler()
    logger = logging.Logger("test-runtime-reconciler-emf-sink-failure", level=logging.INFO)
    logger.addHandler(_RaisingHandler())
    metrics = CloudWatchEmfMetrics(logger=logger, environment="production")
    handler = RuntimeReconcilerLambda(reconciler=reconciler, metrics=metrics)

    with caplog.at_level(logging.ERROR, logger=handler_module.LOGGER.name):
        result = handler.handle({"schema_version": 1, "trigger": "scheduled"})

    assert result["runtime_status"] == "busy"
    assert reconciler.calls == 1
    assert "runtime_reconciler_metric_emission_failed" in caplog.text
    assert "private provider detail" not in caplog.text


def test_lambda_emf_sink_writes_one_unwrapped_json_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler_module._write_emf('{"_aws":{"CloudWatchMetrics":[]}}')

    captured = capsys.readouterr()
    assert captured.out == '{"_aws":{"CloudWatchMetrics":[]}}\n'
    assert captured.err == ""


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"schema_version": True, "trigger": "scheduled"},
        {"schema_version": 1, "trigger": "manual"},
        {"schema_version": 1, "interaction_id": "001"},
        {"schema_version": 1, "interaction_id": "123", "extra": "content"},
        "raw-event",
    ],
)
def test_handler_rejects_invalid_events_before_reconciliation(event: object) -> None:
    reconciler = FakeReconciler()
    handler = RuntimeReconcilerLambda(reconciler=reconciler)

    with pytest.raises(ValueError):
        handler.handle(event)

    assert reconciler.calls == 0


def test_lambda_entrypoint_logs_only_category_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconciler = FakeReconciler()
    handler = RuntimeReconcilerLambda(reconciler=reconciler)
    monkeypatch.setattr(handler_module, "_handler", handler)
    content_marker = "must-not-be-logged"

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(
            RuntimeReconcilerInvocationError,
            match="runtime_reconciler_invocation_failed",
        ),
    ):
        handler_module.lambda_handler(
            {
                "schema_version": 1,
                "interaction_id": "123456789",
                "unexpected": content_marker,
            },
            Context(),
        )

    assert "category=invalid_invocation" in caplog.text
    assert "request_id=request-id" in caplog.text
    assert content_marker not in caplog.text
    assert "123456789" not in caplog.text
