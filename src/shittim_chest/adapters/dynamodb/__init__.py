"""DynamoDB persistence contracts and native-value serialization."""

from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.adapters.dynamodb.outbox import DynamoDbOutboxRepository
from shittim_chest.adapters.dynamodb.repository import (
    DynamoDbDebateRepository,
    create_dynamodb_client,
)
from shittim_chest.adapters.dynamodb.runtime_state import DynamoDbRuntimeStateRepository
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    ItemTooLarge,
    PersistenceFormatError,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    deserialize_outbox,
    deserialize_panel_operation,
    deserialize_runtime_state,
    deserialize_runtime_wake_result,
    deserialize_snapshot,
    ingress_request_sort_key,
    migrate_item,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_outbox,
    serialize_panel_operation,
    serialize_runtime_state,
    serialize_runtime_wake_result,
    serialize_snapshot,
)
from shittim_chest.application.discord import (
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
)

__all__ = (
    "CURRENT_SCHEMA_VERSION",
    "DynamoDbDebateRepository",
    "DynamoDbIngressRepository",
    "DynamoDbOutboxRepository",
    "DynamoDbRuntimeStateRepository",
    "ItemTooLarge",
    "OutboxOperation",
    "OutboxStatus",
    "PanelOperation",
    "PanelOperationKind",
    "PersistenceFormatError",
    "create_dynamodb_client",
    "deserialize_ingress_operation_result",
    "deserialize_ingress_request",
    "deserialize_outbox",
    "deserialize_panel_operation",
    "deserialize_runtime_state",
    "deserialize_runtime_wake_result",
    "deserialize_snapshot",
    "ingress_request_sort_key",
    "migrate_item",
    "serialize_ingress_operation_result",
    "serialize_ingress_request",
    "serialize_outbox",
    "serialize_panel_operation",
    "serialize_runtime_state",
    "serialize_runtime_wake_result",
    "serialize_snapshot",
)
