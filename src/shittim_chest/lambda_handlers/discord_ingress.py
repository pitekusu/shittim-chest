"""Thin composition root for Discord's signed HTTP interaction endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

from shittim_chest.adapters.aws import (
    LambdaRuntimeReconciliationTrigger,
    LambdaStatusPublicationTrigger,
    SsmParameterReader,
    create_ingress_dynamodb_client,
    create_lambda_client,
    create_ssm_client,
)
from shittim_chest.adapters.discord_http import (
    DiscordHttpBoundary,
    DiscordRequestVerifier,
    ingress_response,
    ingress_unavailable_response,
)
from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateAuthorizationLookup,
    DynamoDbIngressRepository,
    DynamoDbRuntimeStateRepository,
)
from shittim_chest.application.discord_http import DiscordHttpOperation
from shittim_chest.application.ingress import DiscordIngressApplication
from shittim_chest.application.ports import (
    Clock,
    ParameterReadUnavailable,
    RepositoryConflict,
    RepositoryUnavailable,
)
from shittim_chest.config.ingress import (
    IngressBootstrapSettings,
    IngressRuntimeSettings,
    load_ingress_bootstrap_settings,
    load_ingress_runtime_settings,
)
from shittim_chest.config.models import StartupConfigurationError
from shittim_chest.runtime.primitives import SystemClock

LOGGER = logging.getLogger(__name__)


class DiscordIngressLambda:
    """Verify before lazily constructing any DynamoDB or Lambda dependency."""

    __slots__ = ("_application", "_boundary", "_clock")

    def __init__(
        self,
        *,
        boundary: DiscordHttpBoundary,
        application: Callable[[], DiscordIngressApplication],
        clock: Clock,
    ) -> None:
        self._boundary = boundary
        self._application = application
        self._clock = clock

    def handle(
        self,
        event: Mapping[str, object],
        *,
        received_at: datetime | None = None,
    ) -> dict[str, object]:
        """Return one API Gateway v2 response without retaining the request payload."""

        now = self._clock.now() if received_at is None else received_at
        reception = self._boundary.receive(event, now=now)
        if reception.response is not None:
            return reception.response.as_event()
        operation = reception.interaction
        if not isinstance(operation, DiscordHttpOperation):
            raise AssertionError("verified Discord reception has no result")
        result = asyncio.run(self._application().accept(operation))
        return ingress_response(result.outcome).as_event()


_handler: DiscordIngressLambda | None = None


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """AWS entrypoint with entry-time budgeting and content-free failure telemetry."""

    received_at = SystemClock().now()
    request_id = _request_id(context)
    if not isinstance(event, Mapping) or any(not isinstance(key, str) for key in event):
        LOGGER.error(
            "discord_ingress_failure category=invalid_event request_id=%s",
            request_id,
        )
        return ingress_unavailable_response().as_event()
    try:
        return _get_handler().handle(
            cast(Mapping[str, object], event),
            received_at=received_at,
        )
    except Exception as error:
        LOGGER.error(
            "discord_ingress_failure category=%s request_id=%s",
            _failure_category(error),
            request_id,
        )
        return ingress_unavailable_response().as_event()


def _get_handler() -> DiscordIngressLambda:
    global _handler
    if _handler is None:
        _handler = _build_handler(os.environ)
    return _handler


def _build_handler(environ: Mapping[str, str]) -> DiscordIngressLambda:
    settings = load_ingress_bootstrap_settings(environ)
    ssm = SsmParameterReader(
        client=create_ssm_client(region_name=settings.aws_region),
    )
    runtime = asyncio.run(load_ingress_runtime_settings(settings, ssm))
    application = _lazy_application(settings, runtime)
    return DiscordIngressLambda(
        boundary=DiscordHttpBoundary(DiscordRequestVerifier(runtime.public_key_hex)),
        application=application,
        clock=SystemClock(),
    )


def _lazy_application(
    settings: IngressBootstrapSettings,
    runtime: IngressRuntimeSettings,
) -> Callable[[], DiscordIngressApplication]:
    cached: DiscordIngressApplication | None = None

    def load() -> DiscordIngressApplication:
        nonlocal cached
        if cached is not None:
            return cached
        dynamodb = create_ingress_dynamodb_client(region_name=settings.aws_region)
        lambda_client = create_lambda_client(region_name=settings.aws_region)
        cached = DiscordIngressApplication(
            runtime_config=runtime.discord,
            clock=SystemClock(),
            ingress=DynamoDbIngressRepository(
                client=dynamodb,
                table_name=settings.table_name,
            ),
            runtime_state=DynamoDbRuntimeStateRepository(
                client=dynamodb,
                table_name=settings.table_name,
            ),
            debates=DynamoDbDebateAuthorizationLookup(
                client=dynamodb,
                table_name=settings.table_name,
            ),
            status_trigger=LambdaStatusPublicationTrigger(
                client=lambda_client,
                function_name=settings.status_publisher_function,
            ),
            reconciler_trigger=LambdaRuntimeReconciliationTrigger(
                client=lambda_client,
                function_name=settings.runtime_reconciler_function,
            ),
        )
        return cached

    return load


def _request_id(context: object) -> str:
    value = getattr(context, "aws_request_id", None)
    if not isinstance(value, str) or not value or len(value) > 128:
        return "unknown"
    return value


def _failure_category(error: Exception) -> str:
    if isinstance(error, (ParameterReadUnavailable, StartupConfigurationError)):
        return "configuration_unavailable"
    if isinstance(error, RepositoryUnavailable):
        return "repository_unavailable"
    if isinstance(error, RepositoryConflict):
        return "repository_state_conflict"
    return "internal_error"


__all__ = ("DiscordIngressLambda", "lambda_handler")
