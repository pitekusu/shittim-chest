"""The only production composition root for the Discord debate process."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from openai import AsyncOpenAI

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_lambda.client import LambdaClient

from shittim_chest.adapters.aws import (
    LambdaStatusPublicationTrigger,
    create_lambda_client,
    ecs_task_instance_id,
)
from shittim_chest.adapters.discord import (
    DiscordClientSupervisor,
    DiscordInteractionController,
    DiscordOutboxRecovery,
    DiscordPyGateway,
    DiscordPyPublisher,
    build_discord_clients,
)
from shittim_chest.adapters.discord.ingress_runtime import DiscordIngressRuntime
from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateRepository,
    DynamoDbIngressRepository,
    DynamoDbOutboxRepository,
    DynamoDbRuntimeStateRepository,
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
from shittim_chest.application import DebateApplication, IngressCommandAdapter
from shittim_chest.application.ingress_drain import IngressDrainer, RuntimeIngressDrainGate
from shittim_chest.application.runtime_instance import RuntimeInstanceState
from shittim_chest.config import BootstrapConfig, load_bootstrap_config
from shittim_chest.runtime import (
    CloudWatchEmfMetrics,
    ContentFreeTelemetry,
    RuntimeAdmissionGateway,
    RuntimeLifecycle,
    RuntimeMetricsReporter,
    SecureCandidateOrderer,
    SystemClock,
    Uuid7IdGenerator,
)
from shittim_chest.runtime.health import EventLoopHeartbeat

_LOGGER = logging.getLogger("shittim_chest")
_EMF_LOGGER = logging.getLogger("shittim_chest.emf")
_EMF_LOGGER.setLevel(logging.INFO)
DEFAULT_CLIENT_CLOSE_TIMEOUT_SECONDS: Final = 20.0


class RuntimeClientCloseTimeout(RuntimeError):
    """Raised when process-scoped SDK clients exceed their exit budget."""


@dataclass(slots=True)
class ProductionRuntime:
    """Own the composed runtime and process-level SDK client cleanup."""

    lifecycle: RuntimeLifecycle
    supervisor: DiscordClientSupervisor
    openai_client: AsyncOpenAI
    dynamodb_client: DynamoDBClient
    lambda_client: LambdaClient
    telemetry: ContentFreeTelemetry
    operational_metrics: RuntimeMetricsReporter | None = None
    client_close_timeout_seconds: float = DEFAULT_CLIENT_CLOSE_TIMEOUT_SECONDS
    heartbeat: EventLoopHeartbeat = field(default_factory=EventLoopHeartbeat)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.client_close_timeout_seconds <= 0:
            raise ValueError("client close timeout must be positive")

    async def run(self) -> None:
        """Run the lifecycle and always close every process-scoped client."""

        async with self.heartbeat:
            self.telemetry.runtime_event("application_started")
            try:
                if self.operational_metrics is None:
                    await self.lifecycle.run()
                else:
                    async with self.operational_metrics:
                        await self.lifecycle.run()
            finally:
                await self.aclose()
                self.telemetry.runtime_event("application_stopped")

    async def aclose(self) -> None:
        """Idempotently release Discord, OpenAI, and DynamoDB client resources."""

        if self._closed:
            return
        self._closed = True
        try:
            async with asyncio.timeout(self.client_close_timeout_seconds):
                results = await asyncio.gather(
                    self.supervisor.close(),
                    self.openai_client.close(),
                    _close_sync_client(self.dynamodb_client.close),
                    _close_sync_client(self.lambda_client.close),
                    return_exceptions=True,
                )
        except TimeoutError as error:
            raise RuntimeClientCloseTimeout(
                "process client shutdown exceeded its safety deadline"
            ) from error
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("process client shutdown failed", errors)


async def _close_sync_client(close: Callable[[], None]) -> None:
    """Bound a synchronous local close without retaining a non-daemon executor thread."""

    loop = asyncio.get_running_loop()
    completed: asyncio.Future[None] = loop.create_future()

    def run() -> None:
        error: Exception | None = None
        try:
            close()
        except Exception as caught:
            error = caught
        try:
            loop.call_soon_threadsafe(_settle_sync_close, completed, error)
        except RuntimeError:
            # The process-level deadline may close the loop before a blocked
            # local SDK cleanup returns. The daemon thread must not extend exit.
            return

    threading.Thread(
        target=run,
        name="dynamodb-client-close",
        daemon=True,
    ).start()
    await completed


def _settle_sync_close(
    completed: asyncio.Future[None],
    error: Exception | None,
) -> None:
    if completed.done():
        return
    if error is None:
        completed.set_result(None)
    else:
        completed.set_exception(error)


def build_production_runtime(config: BootstrapConfig) -> ProductionRuntime:
    """Construct each concrete dependency exactly once after configuration validation."""

    clock = SystemClock()
    ids = Uuid7IdGenerator()
    telemetry = ContentFreeTelemetry(logger=_LOGGER, environment=config.environment)
    owner_id = ecs_task_instance_id()

    dynamodb_client = create_dynamodb_client(region_name=config.aws_region)
    ingress_repository = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )
    repository = DynamoDbDebateRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )
    runtime_state_repository = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )
    outbox = DynamoDbOutboxRepository(
        client=dynamodb_client,
        table_name=config.table_name,
    )
    lambda_client = create_lambda_client(region_name=config.aws_region)
    status_trigger = LambdaStatusPublicationTrigger(
        client=lambda_client,
        function_name=config.status_publisher_function,
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
    ingress_runtime = DiscordIngressRuntime(
        clients=clients,
        application=application,
        panel_refresh=repository,
        clock=clock,
        metrics=telemetry,
        status_trigger=status_trigger,
        claim_owner=owner_id,
    )
    drain_gate = RuntimeIngressDrainGate(admission)
    runtime_instance = RuntimeInstanceState(
        clock=clock,
        repository=runtime_state_repository,
        runtime_instance_id=owner_id,
    )
    drainer = IngressDrainer(
        clock=clock,
        ingress=ingress_repository,
        runtime_state=runtime_state_repository,
        commands=IngressCommandAdapter(application),
        context=ingress_runtime,
        gate=drain_gate,
        runtime_instance_id=owner_id,
        runtime_session=runtime_instance,
    )
    lifecycle = RuntimeLifecycle(
        admission=admission,
        supervisor=supervisor,
        interactions=interactions,
        ingress_runtime=ingress_runtime,
        drain_gate=drain_gate,
        drainer=drainer,
        runtime_instance=runtime_instance,
        tokens=config.discord_tokens,
        previous_command_schema_hash=config.previous_command_schema_hash,
    )
    return ProductionRuntime(
        lifecycle=lifecycle,
        supervisor=supervisor,
        openai_client=openai_client,
        dynamodb_client=dynamodb_client,
        lambda_client=lambda_client,
        telemetry=telemetry,
        operational_metrics=RuntimeMetricsReporter(
            metrics=CloudWatchEmfMetrics(
                logger=_EMF_LOGGER,
                environment=config.environment,
            ),
            readiness=admission.all_identities_ready,
        ),
    )


async def run_from_environment(environ: Mapping[str, str] | None = None) -> None:
    """Validate injected environment values before creating any external SDK client."""

    config = load_bootstrap_config(os.environ if environ is None else environ)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _LOGGER.setLevel(getattr(logging, config.log_level))
    runtime = build_production_runtime(config)
    await runtime.run()
