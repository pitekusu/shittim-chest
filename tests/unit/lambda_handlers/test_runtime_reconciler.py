"""Content-free Lambda boundary tests for runtime reconciliation."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

import shittim_chest.lambda_handlers.runtime_reconciler as handler_module
from shittim_chest.application.runtime_reconciler import RuntimeReconciliationReport
from shittim_chest.lambda_handlers.runtime_reconciler import (
    RuntimeReconcilerInvocationError,
    RuntimeReconcilerLambda,
)

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
            ecs_observed=True,
            ecs_scaled_up=True,
            runtime_reconciled=True,
        )


class Context:
    aws_request_id = "request-id"


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
        "ecs_observed": True,
        "ecs_scaled_up": True,
        "observed_at": "2026-07-26T01:02:03Z",
        "runtime_reconciled": True,
        "startup_recovered": 4,
        "startup_timed_out": 3,
        "status_publications_triggered": 5,
        "terminal_failed": 1,
        "wake_candidates": 2,
    }
    assert "123456789" not in str(result)


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
