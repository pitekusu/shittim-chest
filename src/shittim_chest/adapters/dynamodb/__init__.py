"""DynamoDB persistence contracts and native-value serialization."""

from shittim_chest.adapters.dynamodb.models import (
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
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

__all__ = (
    "CURRENT_SCHEMA_VERSION",
    "ItemTooLarge",
    "OutboxOperation",
    "OutboxStatus",
    "PanelOperation",
    "PanelOperationKind",
    "PersistenceFormatError",
    "deserialize_outbox",
    "deserialize_panel_operation",
    "deserialize_snapshot",
    "migrate_item",
    "serialize_outbox",
    "serialize_panel_operation",
    "serialize_snapshot",
)
