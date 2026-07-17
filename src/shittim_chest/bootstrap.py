"""The only production composition root for the Discord debate process."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.discord import (
    DiscordClientSupervisor,
    DiscordInteractionController,
    DiscordOutboxRecovery,
    DiscordPyGateway,
    DiscordPyPublisher,
    build_discord_clients,
)
from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateRepository,
    DynamoDbOutboxRepository,
    create_dynamodb_client,
)
from shittim_chest.adapters.openai import (
    OpenAIAdapterConfig,
    OpenAIRequestLimiter,
    OpenAIResponsesService,
    OpenAIWebEvidenceService,
    PersonaPrompts,
    create_openai_client,
)
from shittim_chest.application import DebateApplication
from shittim_chest.config import BootstrapConfig, load_bootstrap_config
from shittim_chest.runtime import (
    ContentFreeTelemetry,
    RuntimeAdmissionGateway,
    RuntimeLifecycle,
    SecureCandidateOrderer,
    SystemClock,
    Uuid7IdGenerator,
    lease_owner_id,
)

_LOGGER = logging.getLogger("shittim_chest")


@dataclass(slots=True)
class ProductionRuntime:
    """Own the composed runtime and process-level SDK client cleanup."""

    lifecycle: RuntimeLifecycle
    supervisor: DiscordClientSupervisor
    openai_client: AsyncOpenAI
    dynamodb_client: DynamoDBClient
    telemetry: ContentFreeTelemetry
    _closed: bool = field(default=False, init=False)

    async def run(self) -> None:
        """Run the lifecycle and always close every process-scoped client."""

        self.telemetry.runtime_event("application_started")
        try:
            await self.lifecycle.run()
        finally:
            await self.aclose()
            self.telemetry.runtime_event("application_stopped")

    async def aclose(self) -> None:
        """Idempotently release Discord, OpenAI, and DynamoDB client resources."""

        if self._closed:
            return
        self._closed = True
        await self.supervisor.close()
        await self.openai_client.close()
        await asyncio.to_thread(self.dynamodb_client.close)


def build_production_runtime(config: BootstrapConfig) -> ProductionRuntime:
    """Construct each concrete dependency exactly once after configuration validation."""

    clock = SystemClock()
    ids = Uuid7IdGenerator()
    telemetry = ContentFreeTelemetry(logger=_LOGGER, environment=config.environment)
    owner_id = lease_owner_id()

    dynamodb_client = create_dynamodb_client(region_name=config.aws_region)
    repository = DynamoDbDebateRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )
    outbox = DynamoDbOutboxRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )

    clients = build_discord_clients(config.runtime)
    physical_gateway = DiscordPyGateway(clients=clients, config=config.runtime)
    admission = RuntimeAdmissionGateway(physical_gateway)
    supervisor = DiscordClientSupervisor(clients)

    openai_config = OpenAIAdapterConfig()
    limiter = OpenAIRequestLimiter(max_concurrency=openai_config.max_concurrency)
    openai_client = create_openai_client(api_key=config.openai_api_key)
    prompts = PersonaPrompts(config.participant_prompts())
    openai_service = OpenAIResponsesService(
        client=openai_client,
        personas=prompts,
        limiter=limiter,
        config=openai_config,
        recorder=telemetry,
    )
    evidence_service = OpenAIWebEvidenceService(
        client=openai_client,
        limiter=limiter,
        config=openai_config,
        recorder=telemetry,
    )

    publisher = DiscordPyPublisher(
        clients=clients,
        outbox=outbox,
        clock=clock,
        claim_owner=owner_id,
    )
    recovery = DiscordOutboxRecovery(
        outbox=outbox,
        publisher=publisher,
        clock=clock,
        metrics=telemetry,
    )
    application = DebateApplication(
        clock=clock,
        ids=ids,
        metrics=telemetry,
        discord=admission,
        evidence=evidence_service,
        openai=openai_service,
        repository=repository,
        candidate_orderer=SecureCandidateOrderer(),
        outbox_recovery=recovery,
        lease_owner=owner_id,
    )
    interactions = DiscordInteractionController(
        clients=clients,
        config=config.runtime,
        application=application,
    )
    lifecycle = RuntimeLifecycle(
        admission=admission,
        supervisor=supervisor,
        interactions=interactions,
        application=application,
        tokens=config.discord_tokens,
        previous_command_schema_hash=config.previous_command_schema_hash,
    )
    return ProductionRuntime(
        lifecycle=lifecycle,
        supervisor=supervisor,
        openai_client=openai_client,
        dynamodb_client=dynamodb_client,
        telemetry=telemetry,
    )


async def run_from_environment(environ: Mapping[str, str] | None = None) -> None:
    """Validate injected environment values before creating any external SDK client."""

    config = load_bootstrap_config(os.environ if environ is None else environ)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _LOGGER.setLevel(getattr(logging, config.log_level))
    runtime = build_production_runtime(config)
    await runtime.run()
