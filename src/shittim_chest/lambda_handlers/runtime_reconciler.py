"""Composition root for scheduled and hinted runtime reconciliation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping
from typing import Protocol

from shittim_chest.adapters.aws import (
    EcsServiceRuntimeControl,
    LambdaStatusPublicationTrigger,
    create_runtime_reconciler_dynamodb_client,
    create_runtime_reconciler_ecs_client,
    create_runtime_reconciler_lambda_client,
)
from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    DynamoDbRuntimeActivityInspector,
    DynamoDbRuntimeStateRepository,
)
from shittim_chest.application.ports import (
    EcsRuntimeUnavailable,
    RepositoryConflict,
    RepositoryUnavailable,
    StatusTriggerUnavailable,
)
from shittim_chest.application.runtime_reconciler import (
    RuntimeReconciler,
    RuntimeReconciliationReport,
)
from shittim_chest.config.models import StartupConfigurationError
from shittim_chest.config.runtime_reconciler import (
    load_runtime_reconciler_settings,
)
from shittim_chest.runtime.primitives import SystemClock

LOGGER = logging.getLogger(__name__)

_SNOWFLAKE = re.compile(r"[0-9]{1,20}\Z")


class _Reconciler(Protocol):
    async def reconcile(self) -> RuntimeReconciliationReport: ...


class RuntimeReconcilerInvocationError(RuntimeError):
    """Stable failure that preserves Lambda retry without leaking provider detail."""

    def __init__(self) -> None:
        super().__init__("runtime_reconciler_invocation_failed")


class RuntimeReconcilerLambda:
    """Validate content-free invocations before running one full convergence pass."""

    __slots__ = ("_reconciler",)

    def __init__(self, *, reconciler: _Reconciler) -> None:
        self._reconciler = reconciler

    def handle(self, event: object) -> dict[str, object]:
        """Return only content-free counters and timestamps."""

        _parse_event(event)
        return _report_event(asyncio.run(self._reconciler.reconcile()))


_handler: RuntimeReconcilerLambda | None = None


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """AWS entrypoint with fixed error categories and no raw event logging."""

    request_id = _request_id(context)
    try:
        return _get_handler().handle(event)
    except Exception as error:
        LOGGER.error(
            "runtime_reconciler_failure category=%s request_id=%s",
            _failure_category(error),
            request_id,
        )
        raise RuntimeReconcilerInvocationError from None


def _get_handler() -> RuntimeReconcilerLambda:
    global _handler
    if _handler is None:
        settings = load_runtime_reconciler_settings(os.environ)
        dynamodb = create_runtime_reconciler_dynamodb_client(region_name=settings.aws_region)
        ingress = DynamoDbIngressRepository(
            client=dynamodb,
            table_name=settings.table_name,
        )
        _handler = RuntimeReconcilerLambda(
            reconciler=RuntimeReconciler(
                clock=SystemClock(),
                ingress=ingress,
                activity=DynamoDbRuntimeActivityInspector(
                    client=dynamodb,
                    table_name=settings.table_name,
                    ingress=ingress,
                ),
                runtime_state=DynamoDbRuntimeStateRepository(
                    client=dynamodb,
                    table_name=settings.table_name,
                ),
                ecs=EcsServiceRuntimeControl(
                    client=create_runtime_reconciler_ecs_client(region_name=settings.aws_region),
                    cluster=settings.ecs_cluster,
                    service=settings.ecs_service,
                ),
                status_publications=ingress,
                status_trigger=LambdaStatusPublicationTrigger(
                    client=create_runtime_reconciler_lambda_client(region_name=settings.aws_region),
                    function_name=settings.status_publisher_function,
                ),
            )
        )
    return _handler


def _parse_event(event: object) -> None:
    if not isinstance(event, Mapping) or any(not isinstance(key, str) for key in event):
        raise ValueError("runtime reconciler event shape is invalid")
    schema_version = event.get("schema_version")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise ValueError("runtime reconciler event schema is invalid")
    if set(event) == {"schema_version", "trigger"}:
        if event.get("trigger") != "scheduled":
            raise ValueError("runtime reconciler trigger is invalid")
        return
    if set(event) == {"schema_version", "interaction_id"}:
        interaction_id = event.get("interaction_id")
        if (
            not isinstance(interaction_id, str)
            or _SNOWFLAKE.fullmatch(interaction_id) is None
            or not 0 < int(interaction_id) < 2**64
            or str(int(interaction_id)) != interaction_id
        ):
            raise ValueError("runtime reconciler interaction ID is invalid")
        return
    raise ValueError("runtime reconciler event shape is invalid")


def _report_event(report: RuntimeReconciliationReport) -> dict[str, object]:
    return {
        "conditional_conflicts": report.conditional_conflicts,
        "ecs_observed": report.ecs_observed,
        "ecs_scaled_down": report.ecs_scaled_down,
        "ecs_scaled_up": report.ecs_scaled_up,
        "observed_at": report.observed_at.isoformat().replace("+00:00", "Z"),
        "runtime_reconciled": report.runtime_reconciled,
        "runtime_entered_idle": report.runtime_entered_idle,
        "runtime_stopped": report.runtime_stopped,
        "startup_recovered": report.startup_recovered,
        "startup_timed_out": report.startup_timed_out,
        "status_publications_triggered": report.status_publications_triggered,
        "terminal_failed": report.terminal_failed,
        "wake_candidates": report.wake_candidates,
    }


def _request_id(context: object) -> str:
    value = getattr(context, "aws_request_id", None)
    if not isinstance(value, str) or not value or len(value) > 128:
        return "unknown"
    return value


def _failure_category(error: Exception) -> str:
    if isinstance(error, StartupConfigurationError):
        return "configuration_unavailable"
    if isinstance(error, RepositoryUnavailable):
        return "repository_unavailable"
    if isinstance(error, RepositoryConflict):
        return "repository_state_conflict"
    if isinstance(error, EcsRuntimeUnavailable):
        return "ecs_runtime_unavailable"
    if isinstance(error, StatusTriggerUnavailable):
        return "status_trigger_unavailable"
    if isinstance(error, ValueError):
        return "invalid_invocation"
    return "internal_error"


__all__ = (
    "RuntimeReconcilerInvocationError",
    "RuntimeReconcilerLambda",
    "lambda_handler",
)
