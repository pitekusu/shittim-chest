"""Process-reusable boto3 client factories for bounded AWS execution paths."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

import boto3
from botocore.config import Config

from shittim_chest.application.ports import IngressExecutionDeadlineExceeded

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_ecs.client import ECSClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_ssm.client import SSMClient

DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS = 3.0
INGRESS_CONNECT_TIMEOUT_SECONDS = 0.1
INGRESS_READ_TIMEOUT_SECONDS = 0.3
# A new command uses one durable enqueue. Conflict classification may use two
# more bounded reads before the gate rejects additional SDK work.
INGRESS_MAX_SERIAL_SDK_ROUNDS = 3
# Reserve restore plus API Gateway and Discord transit outside handler timing.
INGRESS_RESPONSE_MARGIN_SECONDS = 1.4
INGRESS_TOTAL_MAX_ATTEMPTS = 1
STATUS_CONNECT_TIMEOUT_SECONDS = 1.0
STATUS_READ_TIMEOUT_SECONDS = 2.0
STATUS_TOTAL_MAX_ATTEMPTS = 3
RECONCILER_CONNECT_TIMEOUT_SECONDS = 1.0
RECONCILER_READ_TIMEOUT_SECONDS = 3.0
RECONCILER_TOTAL_MAX_ATTEMPTS = 3
CONTROL_RECORDS_CONNECT_TIMEOUT_SECONDS = 0.5
CONTROL_RECORDS_READ_TIMEOUT_SECONDS = 2.0
CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS = 2


class IngressSdkCancellationGate:
    """One invocation's thread-safe stop gate for not-yet-started SDK calls."""

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        """Return whether this invocation has stopped admitting new SDK calls."""

        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Permanently stop new SDK calls for this invocation."""

        self._cancelled.set()

    def raise_if_cancelled(self) -> None:
        """Reject before botocore serializes or transmits another request."""

        if self.cancelled:
            raise IngressExecutionDeadlineExceeded


_CURRENT_INGRESS_SDK_GATE: Final[ContextVar[IngressSdkCancellationGate | None]] = ContextVar(
    "shittim_chest_ingress_sdk_gate",
    default=None,
)


@contextmanager
def activate_ingress_sdk_cancellation_gate(
    gate: IngressSdkCancellationGate,
) -> Iterator[None]:
    """Propagate one mutable gate through asyncio.to_thread context copies."""

    token = _CURRENT_INGRESS_SDK_GATE.set(gate)
    try:
        yield
    finally:
        _CURRENT_INGRESS_SDK_GATE.reset(token)


def current_ingress_sdk_cancellation_gate() -> IngressSdkCancellationGate | None:
    """Return the current invocation gate, including inside asyncio worker threads."""

    return _CURRENT_INGRESS_SDK_GATE.get()


def ingress_sdk_config() -> Config:
    """Return a single-attempt SDK policy bounded below the Discord deadline."""

    return Config(
        connect_timeout=INGRESS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=INGRESS_READ_TIMEOUT_SECONDS,
        max_pool_connections=4,
        retries={"mode": "standard", "total_max_attempts": INGRESS_TOTAL_MAX_ATTEMPTS},
        tcp_keepalive=True,
        user_agent_extra="shittim-chest-ingress",
    )


def status_sdk_config() -> Config:
    """Return bounded standard retries for the asynchronous status path."""

    return Config(
        connect_timeout=STATUS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=STATUS_READ_TIMEOUT_SECONDS,
        max_pool_connections=4,
        retries={"mode": "standard", "total_max_attempts": STATUS_TOTAL_MAX_ATTEMPTS},
        tcp_keepalive=True,
        user_agent_extra="shittim-chest-status-publisher",
    )


def runtime_reconciler_sdk_config() -> Config:
    """Return bounded standard retries for scheduled runtime reconciliation."""

    return Config(
        connect_timeout=RECONCILER_CONNECT_TIMEOUT_SECONDS,
        read_timeout=RECONCILER_READ_TIMEOUT_SECONDS,
        max_pool_connections=4,
        retries={"mode": "standard", "total_max_attempts": RECONCILER_TOTAL_MAX_ATTEMPTS},
        tcp_keepalive=True,
        user_agent_extra="shittim-chest-runtime-reconciler",
    )


def control_records_sdk_config() -> Config:
    """Return bounded retries for the deployment-time control-record initializer."""

    return Config(
        connect_timeout=CONTROL_RECORDS_CONNECT_TIMEOUT_SECONDS,
        read_timeout=CONTROL_RECORDS_READ_TIMEOUT_SECONDS,
        max_pool_connections=2,
        retries={"mode": "standard", "total_max_attempts": CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS},
        tcp_keepalive=True,
        user_agent_extra="shittim-chest-control-records",
    )


def create_ingress_dynamodb_client(
    *,
    region_name: str,
) -> DynamoDBClient:
    """Create one DynamoDB client shared by ingress repositories in one process."""

    _require_region(region_name)
    client = boto3.client("dynamodb", region_name=region_name, config=ingress_sdk_config())
    _register_ingress_sdk_gate(client, service_name="dynamodb")
    return client


def create_control_records_dynamodb_client(*, region_name: str) -> DynamoDBClient:
    """Create one DynamoDB client for audited deployment-time initialization."""

    _require_region(region_name)
    return boto3.client(
        "dynamodb",
        region_name=region_name,
        config=control_records_sdk_config(),
    )


def create_lambda_client(*, region_name: str) -> LambdaClient:
    """Create one Lambda client to reuse for an ingress Lambda execution environment."""

    _require_region(region_name)
    client = boto3.client("lambda", region_name=region_name, config=ingress_sdk_config())
    _register_ingress_sdk_gate(client, service_name="lambda")
    return client


def create_ssm_client(*, region_name: str) -> SSMClient:
    """Create one SSM client to reuse for an ingress Lambda execution environment."""

    _require_region(region_name)
    return boto3.client("ssm", region_name=region_name, config=ingress_sdk_config())


def create_status_dynamodb_client(*, region_name: str) -> DynamoDBClient:
    """Create one reusable DynamoDB client for status delivery transactions."""

    _require_region(region_name)
    return boto3.client("dynamodb", region_name=region_name, config=status_sdk_config())


def create_status_ssm_client(*, region_name: str) -> SSMClient:
    """Create one reusable SSM client for the moderator token read."""

    _require_region(region_name)
    return boto3.client("ssm", region_name=region_name, config=status_sdk_config())


def create_runtime_reconciler_ecs_client(*, region_name: str) -> ECSClient:
    """Create one ECS client shared for one reconciler execution environment."""

    _require_region(region_name)
    return boto3.client(
        "ecs",
        region_name=region_name,
        config=runtime_reconciler_sdk_config(),
    )


def create_runtime_reconciler_dynamodb_client(*, region_name: str) -> DynamoDBClient:
    """Create one DynamoDB client shared by reconciler repositories."""

    _require_region(region_name)
    return boto3.client(
        "dynamodb",
        region_name=region_name,
        config=runtime_reconciler_sdk_config(),
    )


def create_runtime_reconciler_lambda_client(*, region_name: str) -> LambdaClient:
    """Create one Lambda client used for idempotent public-status kicks."""

    _require_region(region_name)
    return boto3.client(
        "lambda",
        region_name=region_name,
        config=runtime_reconciler_sdk_config(),
    )


def _require_region(region_name: str) -> None:
    if not region_name or region_name != region_name.strip():
        raise ValueError("AWS Region must not be empty or padded")


def _register_ingress_sdk_gate(
    client: DynamoDBClient | LambdaClient,
    *,
    service_name: str,
) -> None:
    """Check the invocation gate at botocore's last pre-network boundary."""

    client.meta.events.register(
        f"before-call.{service_name}",
        _reject_cancelled_ingress_sdk_call,
        unique_id="shittim-chest-ingress-sdk-cancellation-gate",
    )


def _reject_cancelled_ingress_sdk_call(**_: object) -> None:
    gate = current_ingress_sdk_cancellation_gate()
    if gate is not None:
        gate.raise_if_cancelled()
