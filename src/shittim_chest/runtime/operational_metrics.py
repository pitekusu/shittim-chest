"""Low-cardinality CloudWatch EMF records for runtime operations."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Final

from shittim_chest.healthcheck import HEARTBEAT_PATH, heartbeat_age_seconds

LOGGER = logging.getLogger(__name__)
EMF_NAMESPACE: Final = "ShittimChest/Prod"
DEFAULT_METRIC_INTERVAL_SECONDS: Final = 60.0
DEFAULT_READINESS_TIMEOUT_SECONDS: Final = 10.0


@unique
class OperationalMetricService(StrEnum):
    """Bounded service dimension values; never substitute identifiers here."""

    RUNTIME = "runtime"
    RECONCILER = "reconciler"


@unique
class OperationalMetric(StrEnum):
    """The complete paid custom-metric allowlist for STEP-09C-A."""

    BOT_READY = "BotReady"
    HEARTBEAT_AGE_SECONDS = "HeartbeatAgeSeconds"
    RUNTIME_STATE_CODE = "RuntimeStateCode"
    RUNTIME_DESIRED_COUNT = "RuntimeDesiredCount"
    ECS_RUNNING_COUNT = "EcsRunningCount"
    ECS_PENDING_COUNT = "EcsPendingCount"
    INGRESS_PENDING = "IngressPending"
    OUTBOX_PENDING = "OutboxPending"
    RECONCILER_FAILED = "ReconcilerFailed"
    STATUS_PUBLISH_FAILED = "StatusPublishFailed"


_METRIC_UNITS: Final[dict[OperationalMetric, str]] = {
    OperationalMetric.BOT_READY: "Count",
    OperationalMetric.HEARTBEAT_AGE_SECONDS: "Seconds",
    OperationalMetric.RUNTIME_STATE_CODE: "Count",
    OperationalMetric.RUNTIME_DESIRED_COUNT: "Count",
    OperationalMetric.ECS_RUNNING_COUNT: "Count",
    OperationalMetric.ECS_PENDING_COUNT: "Count",
    OperationalMetric.INGRESS_PENDING: "Count",
    OperationalMetric.OUTBOX_PENDING: "Count",
    OperationalMetric.RECONCILER_FAILED: "Count",
    OperationalMetric.STATUS_PUBLISH_FAILED: "Count",
}


class CloudWatchEmfMetrics:
    """Write one validated EMF document to the existing CloudWatch Logs stream."""

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        writer: Callable[[str], None] | None = None,
        environment: str,
        namespace: str = EMF_NAMESPACE,
    ) -> None:
        if environment != "production":
            raise ValueError("operational metrics require the production environment")
        if namespace != EMF_NAMESPACE:
            raise ValueError("operational metric namespace is fixed")
        if (logger is None) == (writer is None):
            raise ValueError("operational metrics require exactly one output sink")
        sink = logger.info if logger is not None else writer
        if sink is None:
            raise ValueError("operational metrics output sink is unavailable")
        self._write: Callable[[str], None] = sink
        self._environment = environment
        self._namespace = namespace

    def emit(
        self,
        *,
        service: OperationalMetricService,
        values: Mapping[OperationalMetric, int | float],
        at: datetime | None = None,
    ) -> None:
        """Emit only allowlisted finite non-negative values and one dimension set."""

        if not values:
            raise ValueError("operational metric emission must not be empty")
        observed_at = datetime.now(UTC) if at is None else at
        if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
            raise ValueError("operational metric timestamp must be timezone-aware UTC")

        payload: dict[str, object] = {
            "_aws": {
                "Timestamp": int(observed_at.timestamp() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [["Service"]],
                        "Metrics": [
                            {
                                "Name": metric.value,
                                "Unit": _METRIC_UNITS[metric],
                                "StorageResolution": 60,
                            }
                            for metric in sorted(values, key=lambda item: item.value)
                        ],
                    }
                ],
            },
            "Service": service.value,
            "Environment": self._environment,
        }
        for metric, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("operational metric values must be numeric")
            if not math.isfinite(value) or value < 0:
                raise ValueError("operational metric values must be finite and non-negative")
            payload[metric.value] = value
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self._write(serialized)


ReadinessProbe = Callable[[], Awaitable[bool]]


class RuntimeMetricsReporter:
    """Own periodic task-local health metric emission without leaking runtime input."""

    def __init__(
        self,
        *,
        metrics: CloudWatchEmfMetrics,
        readiness: ReadinessProbe,
        heartbeat_path: Path = HEARTBEAT_PATH,
        interval_seconds: float = DEFAULT_METRIC_INTERVAL_SECONDS,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0 or readiness_timeout_seconds <= 0:
            raise ValueError("metric interval and readiness timeout must be positive")
        self._metrics = metrics
        self._readiness = readiness
        self._heartbeat_path = heartbeat_path
        self._interval_seconds = interval_seconds
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._now = now
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> RuntimeMetricsReporter:
        if self._task is not None:
            raise RuntimeError("runtime metrics reporter is already running")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="runtime:operational-metrics")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self._sample()
            try:
                async with asyncio.timeout(self._interval_seconds):
                    await self._stop.wait()
            except TimeoutError:
                continue

    async def _sample(self) -> None:
        try:
            async with asyncio.timeout(self._readiness_timeout_seconds):
                ready = await self._readiness()
        except Exception:
            ready = False
        values: dict[OperationalMetric, int | float] = {
            OperationalMetric.BOT_READY: int(ready),
        }
        heartbeat_age = heartbeat_age_seconds(
            path=self._heartbeat_path,
            now=self._now,
        )
        if heartbeat_age is not None:
            values[OperationalMetric.HEARTBEAT_AGE_SECONDS] = heartbeat_age
        try:
            self._metrics.emit(
                service=OperationalMetricService.RUNTIME,
                values=values,
            )
        except Exception:
            LOGGER.error("runtime_metric_emission_failed")


__all__ = (
    "DEFAULT_METRIC_INTERVAL_SECONDS",
    "DEFAULT_READINESS_TIMEOUT_SECONDS",
    "EMF_NAMESPACE",
    "CloudWatchEmfMetrics",
    "OperationalMetric",
    "OperationalMetricService",
    "RuntimeMetricsReporter",
)
