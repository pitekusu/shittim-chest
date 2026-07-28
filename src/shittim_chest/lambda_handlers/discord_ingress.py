"""Thin composition root for Discord's signed HTTP interaction endpoint."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Final, cast

from shittim_chest.adapters.aws import (
    IngressSdkCancellationGate,
    LambdaRuntimeReconciliationTrigger,
    LambdaStatusPublicationTrigger,
    SsmParameterReader,
    activate_ingress_sdk_cancellation_gate,
    create_ingress_dynamodb_client,
    create_lambda_client,
    create_ssm_client,
)
from shittim_chest.adapters.aws.clients import (
    DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS,
    INGRESS_CONNECT_TIMEOUT_SECONDS,
    INGRESS_READ_TIMEOUT_SECONDS,
    INGRESS_RESPONSE_MARGIN_SECONDS,
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
from shittim_chest.application.ingress import (
    DiscordIngressApplication,
    IngressAcceptance,
)
from shittim_chest.application.ports import (
    Clock,
    IngressExecutionDeadlineExceeded,
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

# Stop durable acceptance at 2.2s from Lambda entry. A currently active AWS SDK
# call may take up to 0.4s to unwind, leaving 0.4s for API Gateway/Discord before
# Discord's 3.0s initial-response deadline. Stop admitting new SDK calls 0.1s
# before cancellation to absorb event-loop scheduling jitter. The Lambda hard
# timeout remains 5s.
DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS: Final = (
    INGRESS_CONNECT_TIMEOUT_SECONDS + INGRESS_READ_TIMEOUT_SECONDS
)
DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS: Final = INGRESS_RESPONSE_MARGIN_SECONDS
DISCORD_INGRESS_SDK_GATE_LEAD_SECONDS: Final = 0.1
DISCORD_INGRESS_SOFT_DEADLINE_SECONDS: Final = (
    DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS
    - DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS
    - DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS
)


class DiscordIngressDeadlineExceeded(TimeoutError):
    """Content-free signal that the initial-response safety budget expired."""

    def __init__(self) -> None:
        super().__init__("discord_ingress_deadline_exceeded")


class DiscordIngressLambda:
    """Verify before lazily constructing any DynamoDB or Lambda dependency."""

    __slots__ = ("_application", "_boundary", "_clock", "_soft_deadline_seconds")

    def __init__(
        self,
        *,
        boundary: DiscordHttpBoundary,
        application: Callable[[], DiscordIngressApplication],
        clock: Clock,
        soft_deadline_seconds: float = DISCORD_INGRESS_SOFT_DEADLINE_SECONDS,
    ) -> None:
        if (
            isinstance(soft_deadline_seconds, bool)
            or not isinstance(soft_deadline_seconds, int | float)
            or not 0 < soft_deadline_seconds <= DISCORD_INGRESS_SOFT_DEADLINE_SECONDS
        ):
            raise ValueError("Discord ingress soft deadline is invalid")
        self._boundary = boundary
        self._application = application
        self._clock = clock
        self._soft_deadline_seconds = float(soft_deadline_seconds)

    def handle(
        self,
        event: Mapping[str, object],
        *,
        received_at: datetime | None = None,
    ) -> dict[str, object]:
        """Return one API Gateway v2 response without retaining the request payload."""

        entry_at = self._clock.now() if received_at is None else received_at
        reception = self._boundary.receive(event, now=entry_at)
        if reception.response is not None:
            return reception.response.as_event()
        operation = reception.interaction
        if not isinstance(operation, DiscordHttpOperation):
            raise AssertionError("verified Discord reception has no result")
        remaining_seconds = self._remaining_seconds(entry_at)
        if remaining_seconds <= 0:
            raise DiscordIngressDeadlineExceeded
        # Client construction is intentionally synchronous and local. Keep it
        # outside the event loop so it cannot postpone asyncio's timeout callback,
        # then remeasure the entry budget before any SDK request can start.
        application = self._application()
        remaining_seconds = self._remaining_seconds(entry_at)
        if remaining_seconds <= 0:
            raise DiscordIngressDeadlineExceeded
        gate = IngressSdkCancellationGate()
        with activate_ingress_sdk_cancellation_gate(gate):
            result = asyncio.run(
                self._accept(
                    application,
                    operation,
                    gate=gate,
                    timeout_seconds=remaining_seconds,
                )
            )
        return ingress_response(result.outcome).as_event()

    def _remaining_seconds(self, entry_at: datetime) -> float:
        elapsed_seconds = max(0.0, (self._clock.now() - entry_at).total_seconds())
        return self._soft_deadline_seconds - elapsed_seconds

    async def _accept(
        self,
        application: DiscordIngressApplication,
        operation: DiscordHttpOperation,
        *,
        gate: IngressSdkCancellationGate,
        timeout_seconds: float,
    ) -> IngressAcceptance:
        loop = asyncio.get_running_loop()
        gate_lead_seconds = min(
            DISCORD_INGRESS_SDK_GATE_LEAD_SECONDS,
            timeout_seconds / 2,
        )
        gate_timer = loop.call_later(
            timeout_seconds - gate_lead_seconds,
            gate.cancel,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                gate.raise_if_cancelled()
                return await application.accept(operation)
        except IngressExecutionDeadlineExceeded, TimeoutError:
            raise DiscordIngressDeadlineExceeded from None
        finally:
            gate_timer.cancel()
            gate.cancel()


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
    if isinstance(error, DiscordIngressDeadlineExceeded):
        return "deadline_exceeded"
    if isinstance(error, (ParameterReadUnavailable, StartupConfigurationError)):
        return "configuration_unavailable"
    if isinstance(error, RepositoryUnavailable):
        return "repository_unavailable"
    if isinstance(error, RepositoryConflict):
        return "repository_state_conflict"
    return "internal_error"


__all__ = (
    "DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS",
    "DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS",
    "DISCORD_INGRESS_SDK_GATE_LEAD_SECONDS",
    "DISCORD_INGRESS_SOFT_DEADLINE_SECONDS",
    "DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS",
    "DiscordIngressDeadlineExceeded",
    "DiscordIngressLambda",
    "lambda_handler",
)
