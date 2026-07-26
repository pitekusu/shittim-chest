"""Bounded AWS SDK adapters used by the HTTP interaction ingress."""

from shittim_chest.adapters.aws.clients import (
    create_ingress_dynamodb_client,
    create_lambda_client,
    create_runtime_reconciler_dynamodb_client,
    create_runtime_reconciler_ecs_client,
    create_runtime_reconciler_lambda_client,
    create_ssm_client,
    create_status_dynamodb_client,
    create_status_ssm_client,
    ingress_sdk_config,
    runtime_reconciler_sdk_config,
    status_sdk_config,
)
from shittim_chest.adapters.aws.ecs import EcsServiceRuntimeControl
from shittim_chest.adapters.aws.ssm import SsmParameterReader
from shittim_chest.adapters.aws.status_trigger import (
    LambdaRuntimeReconciliationTrigger,
    LambdaStatusPublicationTrigger,
)
from shittim_chest.adapters.aws.task_metadata import (
    EcsTaskMetadataUnavailable,
    ecs_task_instance_id,
)

__all__ = (
    "EcsServiceRuntimeControl",
    "EcsTaskMetadataUnavailable",
    "LambdaRuntimeReconciliationTrigger",
    "LambdaStatusPublicationTrigger",
    "SsmParameterReader",
    "create_ingress_dynamodb_client",
    "create_lambda_client",
    "create_runtime_reconciler_dynamodb_client",
    "create_runtime_reconciler_ecs_client",
    "create_runtime_reconciler_lambda_client",
    "create_ssm_client",
    "create_status_dynamodb_client",
    "create_status_ssm_client",
    "ecs_task_instance_id",
    "ingress_sdk_config",
    "runtime_reconciler_sdk_config",
    "status_sdk_config",
)
