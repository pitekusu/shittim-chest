"""Shared DynamoDB condition that closes producers while deployment is locked."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef
else:
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.serializer import CURRENT_SCHEMA_VERSION
from shittim_chest.application.deployment_guard import (
    DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION,
    DeploymentLockState,
)


def deployment_lock_open_check(*, table_name: str) -> TransactWriteItemTypeDef:
    """Return the shared fail-closed ConditionCheck used by ingress producers."""

    return cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": marshal_item({"PK": "CONTROL#DEPLOYMENT", "SK": "LOCK"}),
                "ConditionExpression": (
                    "record_type=:type AND schema_version=:schema "
                    "AND record_schema_version=:record_schema "
                    "AND lock_state=:open AND fencing_token >= :zero AND version >= :zero "
                    "AND attribute_type(updated_at, :string_type) "
                    "AND attribute_not_exists(guard_id) "
                    "AND attribute_not_exists(lock_owner) "
                    "AND attribute_not_exists(locked_at) "
                    "AND attribute_not_exists(lock_expires_at) "
                    "AND attribute_not_exists(deployment_mode) "
                    "AND attribute_not_exists(break_glass_reason)"
                ),
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":type": "deployment_lock",
                        ":schema": CURRENT_SCHEMA_VERSION,
                        ":record_schema": DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION,
                        ":open": DeploymentLockState.OPEN.value,
                        ":zero": 0,
                        ":string_type": "S",
                    }
                ),
            }
        },
    )


__all__ = ("deployment_lock_open_check",)
