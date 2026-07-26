"""Fail-closed resource identifiers for the scheduled runtime reconciler."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from shittim_chest.config.models import DEFAULT_AWS_REGION, StartupConfigurationError

_ECS_NAME = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
_LAMBDA_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}\Z")


@dataclass(frozen=True, slots=True)
class RuntimeReconcilerSettings:
    """Resource names only; no token, request content, or private configuration."""

    aws_region: str
    table_name: str
    ecs_cluster: str
    ecs_service: str
    status_publisher_function: str


def load_runtime_reconciler_settings(
    environ: Mapping[str, str],
) -> RuntimeReconcilerSettings:
    """Validate one Tokyo production reconciler environment without echoing values."""

    try:
        region = environ.get("AWS_REGION", DEFAULT_AWS_REGION)
        if region != DEFAULT_AWS_REGION:
            raise ValueError
        table_name = _required(environ, "SHITTIM_DYNAMODB_TABLE", pattern=_TABLE_NAME)
        cluster = _required(environ, "SHITTIM_ECS_CLUSTER", pattern=_ECS_NAME)
        service = _required(environ, "SHITTIM_ECS_SERVICE", pattern=_ECS_NAME)
        status_function = _required(
            environ,
            "SHITTIM_STATUS_PUBLISHER_FUNCTION",
            pattern=_LAMBDA_FUNCTION_NAME,
        )
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None
    return RuntimeReconcilerSettings(
        aws_region=region,
        table_name=table_name,
        ecs_cluster=cluster,
        ecs_service=service,
        status_publisher_function=status_function,
    )


def _required(
    environ: Mapping[str, str],
    name: str,
    *,
    pattern: re.Pattern[str],
) -> str:
    value = environ[name]
    if value != value.strip() or pattern.fullmatch(value) is None:
        raise ValueError
    return value


__all__ = ("RuntimeReconcilerSettings", "load_runtime_reconciler_settings")
