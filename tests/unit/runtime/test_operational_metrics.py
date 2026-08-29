from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from shittim_chest.runtime.operational_metrics import (
    EMF_NAMESPACE,
    CloudWatchEmfMetrics,
    OperationalMetric,
    OperationalMetricService,
    RuntimeMetricsReporter,
)

NOW = datetime(2026, 7, 29, 1, 2, 3, tzinfo=UTC)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _RaisingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record
        raise RuntimeError("private provider detail")


def _metrics() -> tuple[CloudWatchEmfMetrics, _ListHandler]:
    logger = logging.Logger("operational-metrics-test", level=logging.INFO)
    handler = _ListHandler()
    logger.addHandler(handler)
    return CloudWatchEmfMetrics(logger=logger, environment="production"), handler


def test_emf_has_one_fixed_dimension_set_and_no_runtime_identifiers() -> None:
    metrics, handler = _metrics()

    metrics.emit(
        service=OperationalMetricService.RECONCILER,
        values={
            OperationalMetric.INGRESS_PENDING: 2,
            OperationalMetric.OUTBOX_PENDING: 1,
        },
        at=NOW,
    )

    assert len(handler.messages) == 1
    payload = json.loads(handler.messages[0])
    directive = payload["_aws"]["CloudWatchMetrics"][0]
    assert payload["_aws"]["Timestamp"] == 1_785_286_923_000
    assert directive["Namespace"] == EMF_NAMESPACE
    assert directive["Dimensions"] == [["Service"]]
    assert payload["Service"] == "reconciler"
    assert payload["Environment"] == "production"
    assert payload["IngressPending"] == 2
    assert payload["OutboxPending"] == 1
    assert "debate" not in handler.messages[0].lower()
    assert "discord" not in handler.messages[0].lower()


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan")])
def test_emf_rejects_non_numeric_or_unbounded_values(value: object) -> None:
    metrics, _ = _metrics()

    with pytest.raises(ValueError, match="metric values"):
        metrics.emit(
            service=OperationalMetricService.RUNTIME,
            values={OperationalMetric.BOT_READY: cast(int | float, value)},
            at=NOW,
        )


def test_emf_rejects_non_production_environment_and_naive_timestamp() -> None:
    logger = logging.Logger("operational-metrics-test")
    with pytest.raises(ValueError, match="production"):
        CloudWatchEmfMetrics(logger=logger, environment="development")

    metrics, _ = _metrics()
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        metrics.emit(
            service=OperationalMetricService.RUNTIME,
            values={OperationalMetric.BOT_READY: 1},
            at=NOW.replace(tzinfo=None),
        )

    with pytest.raises(ValueError, match="must not be empty"):
        metrics.emit(
            service=OperationalMetricService.RUNTIME,
            values={},
            at=NOW,
        )


def test_emf_requires_exactly_one_output_sink() -> None:
    logger = logging.Logger("operational-metrics-test")

    with pytest.raises(ValueError, match="exactly one output sink"):
        CloudWatchEmfMetrics(environment="production")
    with pytest.raises(ValueError, match="exactly one output sink"):
        CloudWatchEmfMetrics(
            environment="production",
            logger=logger,
            writer=lambda _: None,
        )


def test_emf_writer_receives_one_root_json_document() -> None:
    messages: list[str] = []
    metrics = CloudWatchEmfMetrics(
        environment="production",
        writer=messages.append,
    )

    metrics.emit(
        service=OperationalMetricService.RECONCILER,
        values={OperationalMetric.OUTBOX_PENDING: 0},
        at=NOW,
    )

    assert len(messages) == 1
    assert json.loads(messages[0])["_aws"]["CloudWatchMetrics"][0]["Namespace"] == EMF_NAMESPACE


@pytest.mark.asyncio
async def test_runtime_reporter_owns_task_and_emits_readiness_and_heartbeat(
    tmp_path: Path,
) -> None:
    metrics, handler = _metrics()
    path = tmp_path / "heartbeat"
    path.write_text("42\n", encoding="ascii")
    modified_at = path.stat().st_mtime

    async def ready() -> bool:
        return True

    reporter = RuntimeMetricsReporter(
        metrics=metrics,
        readiness=ready,
        heartbeat_path=path,
        interval_seconds=0.01,
        now=lambda: modified_at + 3,
    )
    async with reporter:
        await asyncio.sleep(0.025)

    assert len(handler.messages) >= 2
    payload = json.loads(handler.messages[0])
    assert payload["BotReady"] == 1
    assert payload["HeartbeatAgeSeconds"] == 3
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == "runtime:operational-metrics"
        and not task.done()
    ]


@pytest.mark.asyncio
async def test_runtime_reporter_fails_closed_when_readiness_probe_fails(
    tmp_path: Path,
) -> None:
    metrics, handler = _metrics()

    async def unavailable() -> bool:
        raise RuntimeError("provider detail must not be logged")

    reporter = RuntimeMetricsReporter(
        metrics=metrics,
        readiness=unavailable,
        heartbeat_path=tmp_path / "missing",
        interval_seconds=1,
    )
    async with reporter:
        await asyncio.sleep(0)

    assert len(handler.messages) == 1
    assert json.loads(handler.messages[0])["BotReady"] == 0
    assert "provider detail" not in handler.messages[0]


@pytest.mark.asyncio
async def test_runtime_reporter_bounds_a_stalled_readiness_probe(tmp_path: Path) -> None:
    metrics, handler = _metrics()
    release = asyncio.Event()

    async def stalled() -> bool:
        await release.wait()
        return True

    reporter = RuntimeMetricsReporter(
        metrics=metrics,
        readiness=stalled,
        heartbeat_path=tmp_path / "missing",
        interval_seconds=1,
        readiness_timeout_seconds=0.01,
    )
    async with reporter:
        await asyncio.sleep(0.02)

    assert len(handler.messages) == 1
    assert json.loads(handler.messages[0])["BotReady"] == 0


@pytest.mark.asyncio
async def test_runtime_reporter_contains_metric_sink_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.Logger("operational-metrics-failure", level=logging.INFO)
    logger.addHandler(_RaisingHandler())
    metrics = CloudWatchEmfMetrics(logger=logger, environment="production")

    async def ready() -> bool:
        return True

    reporter = RuntimeMetricsReporter(
        metrics=metrics,
        readiness=ready,
        heartbeat_path=tmp_path / "missing",
        interval_seconds=0.01,
    )
    with caplog.at_level(
        logging.ERROR,
        logger="shittim_chest.runtime.operational_metrics",
    ):
        async with reporter:
            await asyncio.sleep(0.02)

    assert "runtime_metric_emission_failed" in caplog.text
    assert "private provider detail" not in caplog.text
