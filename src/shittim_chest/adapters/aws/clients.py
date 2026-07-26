"""Process-reusable boto3 client factories for bounded AWS execution paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_ecs.client import ECSClient
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_ssm.client import SSMClient

DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS = 3.0
INGRESS_CONNECT_TIMEOUT_SECONDS = 0.1
INGRESS_READ_TIMEOUT_SECONDS = 0.3
# Cold SSM, semantic probe, authorization, enqueue, race classification,
# and canonical replay bundle are the longest serial pre-response path.
INGRESS_MAX_SERIAL_SDK_ROUNDS = 6
INGRESS_RESPONSE_MARGIN_SECONDS = 0.4
INGRESS_TOTAL_MAX_ATTEMPTS = 1
STATUS_CONNECT_TIMEOUT_SECONDS = 1.0
STATUS_READ_TIMEOUT_SECONDS = 2.0
STATUS_TOTAL_MAX_ATTEMPTS = 3
RECONCILER_CONNECT_TIMEOUT_SECONDS = 1.0
RECONCILER_READ_TIMEOUT_SECONDS = 3.0
RECONCILER_TOTAL_MAX_ATTEMPTS = 3


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


def create_ingress_dynamodb_client(
    *,
    region_name: str,
) -> DynamoDBClient:
    """Create one DynamoDB client shared by ingress repositories in one process."""

    _require_region(region_name)
    return boto3.client("dynamodb", region_name=region_name, config=ingress_sdk_config())


def create_lambda_client(*, region_name: str) -> LambdaClient:
    """Create one Lambda client to reuse for an ingress Lambda execution environment."""

    _require_region(region_name)
    return boto3.client("lambda", region_name=region_name, config=ingress_sdk_config())


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
