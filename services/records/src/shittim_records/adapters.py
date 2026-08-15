"""AWS adapter boundary for Records projection and backfill."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from botocore.exceptions import ClientError
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import (
    AttributeValueTypeDef,
    TransactWriteItemTypeDef,
)
from mypy_boto3_ssm.client import SSMClient
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    DynamoItem,
    DynamoValue,
    deserialize_snapshot,
)
from shittim_chest.application import DebateSnapshot

from shittim_records.archive import ArchiveProjection, RecordsPresentationConfig

MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
MAX_TRANSACTION_ACTIONS = 100


class ProjectionConflict(RuntimeError):
    """Raised when an existing archive marker has a different fingerprint."""


class ConfigurationUnavailable(RuntimeError):
    """Raised when required SSM configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ProjectionConfiguration:
    identity_hmac_key: bytes
    presentation: RecordsPresentationConfig


@dataclass(frozen=True, slots=True)
class BackfillCheckpoint:
    """One validated resumable cursor, including its terminal state."""

    exclusive_start_key: DynamoItem | None
    complete: bool
    projected_count: int = 0
    skipped_count: int = 0


class SourceDebateRepository:
    """Read source partitions with strong consistency and complete pagination."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_partition(self, partition_key: str) -> DebateSnapshot:
        if not partition_key.startswith("DEBATE#"):
            raise ValueError("source partition key is not a debate partition")
        items: list[DynamoItem] = []
        start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: dict[str, Any] = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": marshal_item({":pk": partition_key}),
                "ConsistentRead": True,
            }
            if start_key is not None:
                parameters["ExclusiveStartKey"] = start_key
            response = self._client.query(**parameters)
            items.extend(unmarshal_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        if not items:
            raise ValueError("source debate partition does not exist")
        return deserialize_snapshot(items)

    def scan_completed_meta(
        self,
        *,
        exclusive_start_key: Mapping[str, DynamoValue] | None,
        limit: int,
    ) -> tuple[tuple[str, ...], DynamoItem | None]:
        if not 1 <= limit <= 100:
            raise ValueError("backfill page limit must be between 1 and 100")
        parameters: dict[str, Any] = {
            "TableName": self._table_name,
            "ConsistentRead": True,
            "FilterExpression": "record_type = :meta AND current_phase = :completed",
            "ExpressionAttributeValues": marshal_item(
                {":meta": "debate_meta", ":completed": "completed"}
            ),
            "ProjectionExpression": "PK",
            "Limit": limit,
        }
        if exclusive_start_key is not None:
            parameters["ExclusiveStartKey"] = marshal_item(exclusive_start_key)
        response = self._client.scan(**parameters)
        partition_keys = tuple(
            str(unmarshal_item(item)["PK"]) for item in response.get("Items", [])
        )
        raw_last_key = response.get("LastEvaluatedKey")
        last_key = None if not raw_last_key else unmarshal_item(raw_last_key)
        return partition_keys, last_key


class ArchiveRepository:
    """Write immutable projections using one bounded DynamoDB transaction."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def put_projection(self, projection: ArchiveProjection) -> bool:
        marker_key = {
            "PK": f"RECORD#{projection.record_id}",
            "SK": "PROJECTION#V1",
        }
        marker = self._get(marker_key)
        if marker is not None:
            return self._classify_existing(marker, projection.source_fingerprint)

        marshaled_items = tuple(marshal_item(item) for item in projection.items)
        _validate_transaction(marshaled_items)
        actions: list[TransactWriteItemTypeDef] = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": item,
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            }
            for item in marshaled_items
        ]
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=f"records-{projection.source_fingerprint[:28]}",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            marker = self._get(marker_key)
            if marker is None:
                raise
            return self._classify_existing(marker, projection.source_fingerprint)
        return True

    def _get(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        item = response.get("Item")
        return None if item is None else unmarshal_item(item)

    @staticmethod
    def _classify_existing(marker: DynamoItem, expected_fingerprint: str) -> bool:
        actual = marker.get("source_fingerprint")
        if actual == expected_fingerprint:
            return False
        raise ProjectionConflict("existing projection fingerprint does not match source")


class StatisticsRepository:
    """Persist only the internal resumable backfill checkpoint."""

    _KEY: ClassVar[dict[str, DynamoValue]] = {
        "PK": "CHECKPOINT#BACKFILL",
        "SK": "ARCHIVE#V1",
    }

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_backfill_checkpoint(self) -> BackfillCheckpoint | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(self._KEY),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        decoded = unmarshal_item(item)
        if (
            decoded.get("schema_version") != 1
            or decoded.get("record_type") != "backfill_checkpoint"
            or not isinstance(decoded.get("complete"), bool)
        ):
            raise ValueError("backfill checkpoint is invalid")
        for field in ("projected_count", "skipped_count"):
            value = decoded.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("backfill checkpoint count is invalid")
        complete = cast(bool, decoded["complete"])
        raw_key = decoded.get("exclusive_start_key")
        if complete and raw_key is not None:
            raise ValueError("completed backfill checkpoint still has a cursor")
        if not complete and not isinstance(raw_key, Mapping):
            raise ValueError("incomplete backfill checkpoint has no cursor")
        return BackfillCheckpoint(
            exclusive_start_key=cast(DynamoItem | None, raw_key),
            complete=complete,
            projected_count=cast(int, decoded["projected_count"]),
            skipped_count=cast(int, decoded["skipped_count"]),
        )

    def save_backfill_checkpoint(
        self,
        *,
        exclusive_start_key: DynamoItem | None,
        projected_count: int,
        skipped_count: int,
        updated_at: str,
    ) -> None:
        if projected_count < 0 or skipped_count < 0:
            raise ValueError("backfill checkpoint counts must be non-negative")
        item: DynamoItem = {
            **self._KEY,
            "schema_version": 1,
            "record_type": "backfill_checkpoint",
            "projected_count": projected_count,
            "skipped_count": skipped_count,
            "updated_at": updated_at,
            "complete": exclusive_start_key is None,
        }
        if exclusive_start_key is not None:
            item["exclusive_start_key"] = exclusive_start_key
        self._client.put_item(TableName=self._table_name, Item=marshal_item(item))


class ConfigurationRepository:
    """Load the two exact SSM values required by the Projector."""

    def __init__(
        self,
        client: SSMClient,
        *,
        identity_hmac_parameter_name: str,
        presentation_parameter_name: str,
    ) -> None:
        self._client = client
        self._identity_name = identity_hmac_parameter_name
        self._presentation_name = presentation_parameter_name
        self._cached: ProjectionConfiguration | None = None

    def load(self) -> ProjectionConfiguration:
        if self._cached is not None:
            return self._cached
        names = [self._identity_name, self._presentation_name]
        response = self._client.get_parameters(Names=names, WithDecryption=True)
        if response.get("InvalidParameters"):
            raise ConfigurationUnavailable("required Records configuration is missing")
        values = {
            parameter["Name"]: parameter.get("Value", "")
            for parameter in response.get("Parameters", [])
        }
        if set(values) != set(names):
            raise ConfigurationUnavailable("required Records configuration is incomplete")
        try:
            hmac_key = values[self._identity_name].encode()
            presentation = RecordsPresentationConfig.model_validate_json(
                values[self._presentation_name]
            )
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            raise ConfigurationUnavailable("Records configuration is invalid") from error
        if len(hmac_key) < 32:
            raise ConfigurationUnavailable("Records identity key is too short")
        self._cached = ProjectionConfiguration(
            identity_hmac_key=hmac_key,
            presentation=presentation,
        )
        return self._cached


def _validate_transaction(
    items: tuple[dict[str, AttributeValueTypeDef], ...],
) -> None:
    if not items or len(items) >= MAX_TRANSACTION_ACTIONS:
        raise ValueError("archive transaction action count is outside the allowed range")
    aggregate_bytes = 0
    for item in items:
        item_bytes = len(json.dumps(item, separators=(",", ":")).encode())
        if item_bytes > MAX_DYNAMODB_ITEM_BYTES:
            raise ValueError("archive item exceeds the DynamoDB item size limit")
        aggregate_bytes += item_bytes
    if aggregate_bytes > MAX_TRANSACTION_BYTES:
        raise ValueError("archive transaction exceeds the DynamoDB transaction size limit")
