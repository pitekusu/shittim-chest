"""Composition root for moderator-only durable public status delivery."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping

import httpx

from shittim_chest.adapters.aws import (
    SsmParameterReader,
    create_status_dynamodb_client,
    create_status_ssm_client,
)
from shittim_chest.adapters.discord import (
    DiscordRestStatusGateway,
    create_discord_status_http_client,
)
from shittim_chest.adapters.dynamodb import DynamoDbIngressRepository
from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.ports import (
    ParameterReadUnavailable,
    RepositoryConflict,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import IngressKind, IngressRequest
from shittim_chest.application.status_publication import (
    DiscordStatusGateway,
    PublicStatusPublisher,
    StatusDeliveryError,
    StatusDeliveryErrorCode,
)
from shittim_chest.config.models import StartupConfigurationError
from shittim_chest.config.status_publisher import (
    StatusPublisherSettings,
    load_status_publisher_settings,
    load_status_runtime_config,
)
from shittim_chest.runtime.primitives import SystemClock

LOGGER = logging.getLogger(__name__)

_SNOWFLAKE = re.compile(r"[0-9]{1,20}\Z")


class StatusPublisherInvocationError(RuntimeError):
    """Content-free failure raised so Lambda asynchronous retry remains active."""

    def __init__(self) -> None:
        super().__init__("status_publisher_invocation_failed")


class DiscordStatusPublisherLambda:
    """Load the moderator token only after a publication claim succeeds."""

    __slots__ = ("_http", "_publisher", "_reader", "_settings")

    def __init__(
        self,
        *,
        publisher: PublicStatusPublisher,
        reader: SsmParameterReader,
        settings: StatusPublisherSettings,
        http_client: httpx.Client,
    ) -> None:
        self._publisher = publisher
        self._reader = reader
        self._settings = settings
        self._http = http_client

    def handle(self, event: object, *, claim_owner: str) -> dict[str, str]:
        """Process one exact, content-free invocation event."""

        interaction_id = _parse_event(event)

        async def gateway_factory(request: IngressRequest) -> DiscordStatusGateway:
            try:
                runtime = await load_status_runtime_config(self._settings, self._reader)
                if not _runtime_allows_status(runtime, request):
                    raise StartupConfigurationError
                token = await self._reader.get_parameter(
                    self._settings.moderator_token_parameter,
                    with_decryption=True,
                )
                return DiscordRestStatusGateway(
                    client=self._http,
                    bot_token=token,
                    expected_application_id=runtime.application_id_for(DiscordBotSlot.MODERATOR),
                    expected_guild_id=runtime.guild_id,
                )
            except ParameterReadUnavailable:
                raise StatusDeliveryError(
                    StatusDeliveryErrorCode.UNAVAILABLE,
                    retryable=True,
                ) from None
            except StartupConfigurationError, ValueError:
                raise StatusDeliveryError(
                    StatusDeliveryErrorCode.REJECTED,
                    retryable=False,
                ) from None

        outcome = asyncio.run(
            self._publisher.publish(
                interaction_id=interaction_id,
                claim_owner=claim_owner,
                gateway_factory=gateway_factory,
            )
        )
        return {"outcome": outcome.value}


_handler: DiscordStatusPublisherLambda | None = None


def lambda_handler(event: object, context: object) -> dict[str, str]:
    """AWS entrypoint that logs only a fixed failure category and request ID."""

    request_id = _request_id(context)
    try:
        return _get_handler().handle(event, claim_owner=request_id)
    except Exception as error:
        LOGGER.error(
            "discord_status_failure category=%s request_id=%s",
            _failure_category(error),
            request_id,
        )
        raise StatusPublisherInvocationError from None


def _get_handler() -> DiscordStatusPublisherLambda:
    global _handler
    if _handler is None:
        settings = load_status_publisher_settings(os.environ)
        repository = DynamoDbIngressRepository(
            client=create_status_dynamodb_client(region_name=settings.aws_region),
            table_name=settings.table_name,
        )
        _handler = DiscordStatusPublisherLambda(
            publisher=PublicStatusPublisher(repository=repository, clock=SystemClock()),
            reader=SsmParameterReader(
                client=create_status_ssm_client(region_name=settings.aws_region)
            ),
            settings=settings,
            http_client=create_discord_status_http_client(),
        )
    return _handler


def _parse_event(event: object) -> str:
    if not isinstance(event, Mapping) or set(event) != {"schema_version", "interaction_id"}:
        raise ValueError("status publisher event shape is invalid")
    schema_version = event.get("schema_version")
    interaction_id = event.get("interaction_id")
    if schema_version != 1 or isinstance(schema_version, bool):
        raise ValueError("status publisher event schema is invalid")
    if (
        not isinstance(interaction_id, str)
        or _SNOWFLAKE.fullmatch(interaction_id) is None
        or not 0 < int(interaction_id) < 2**64
        or str(int(interaction_id)) != interaction_id
    ):
        raise ValueError("status publisher interaction ID is invalid")
    return interaction_id


def _runtime_allows_status(
    runtime: DiscordRuntimeConfig,
    request: IngressRequest,
) -> bool:
    if (
        request.application_id != runtime.application_id_for(DiscordBotSlot.MODERATOR)
        or request.guild_id != runtime.guild_id
        or request.status_channel_id != request.channel_id
    ):
        return False
    if request.kind is IngressKind.NEW_DEBATE:
        return request.channel_id in runtime.allowed_channel_ids
    return (
        request.parent_channel_id in runtime.allowed_channel_ids
        and request.source_thread_id == request.channel_id
    )


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
    if isinstance(error, ValueError):
        return "invalid_invocation"
    return "internal_error"


__all__ = (
    "DiscordStatusPublisherLambda",
    "StatusPublisherInvocationError",
    "lambda_handler",
)
