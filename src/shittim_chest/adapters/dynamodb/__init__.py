"""DynamoDB persistence contracts and native-value serialization."""

from shittim_chest.adapters.dynamodb.outbox import DynamoDbOutboxRepository
from shittim_chest.adapters.dynamodb.repository import (
    DynamoDbDebateRepository,
    create_dynamodb_client,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    ItemTooLarge,
    PersistenceFormatError,
    deserialize_outbox,
    deserialize_panel_operation,
    deserialize_snapshot,
    migrate_item,
    serialize_outbox,
    serialize_panel_operation,
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
    "DynamoDbOutboxRepository",
    "ItemTooLarge",
    "OutboxOperation",
    "OutboxStatus",
    "PanelOperation",
    "PanelOperationKind",
    "PersistenceFormatError",
    "create_dynamodb_client",
    "deserialize_outbox",
    "deserialize_panel_operation",
    "deserialize_snapshot",
    "migrate_item",
    "serialize_outbox",
    "serialize_panel_operation",
    "serialize_snapshot",
)
