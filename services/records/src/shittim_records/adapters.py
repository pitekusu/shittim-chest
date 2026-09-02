"""AWS adapter boundary for Records projection and backfill."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        TransactWriteItemTypeDef,
    )
    from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    deserialize_snapshot,
)
from shittim_chest.application import DebateSnapshot

from shittim_records.archive import ArchiveProjection, RecordsPresentationConfig

MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
MAX_TRANSACTION_ACTIONS = 100
BackfillMode = Literal["dry-run", "apply"]


class ProjectionConflict(RuntimeError):
    """Raised when an existing archive marker has a different fingerprint."""


class ConfigurationUnavailable(RuntimeError):
    """Raised when required SSM configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class ProjectionConfiguration:
    identity_hmac_key: bytes
    presentation: RecordsPresentationConfig


@dataclass(frozen=True, slots=True)
class LoadedSourceDebate:
    """One validated snapshot plus its persisted, pre-migration schema version."""

    snapshot: DebateSnapshot
    schema_version: int


@dataclass(frozen=True, slots=True)
class BackfillCheckpoint:
    """One validated resumable cursor, including its terminal state."""

    exclusive_start_key: DynamoItem | None
    complete: bool
    candidate_count: int = 0
    validated_count: int = 0
    projected_count: int = 0
    skipped_count: int = 0


class SourceDebateRepository:
    """Read source partitions with strong consistency and complete pagination."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_partition(self, partition_key: str) -> DebateSnapshot:
        return self.load_partition_for_projection(partition_key).snapshot

    def load_partition_for_projection(self, partition_key: str) -> LoadedSourceDebate:
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
        if any(
            isinstance(item.get("schema_version"), bool)
            or not isinstance(item.get("schema_version"), int)
            or item.get("schema_version") not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
            for item in items
        ):
            raise ValueError("source debate partition schema is inconsistent")
        debate_metas = [
            item
            for item in items
            if item.get("SK") == "META" and item.get("record_type") == "debate_meta"
        ]
        if len(debate_metas) != 1 or debate_metas[0].get("current_phase") != "completed":
            raise ValueError("source completed debate metadata is invalid")
        debate_meta = debate_metas[0]
        attempt_id = debate_meta.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("source completed debate metadata is invalid")
        attempt_metas = [
            item
            for item in items
            if item.get("SK") == f"ATTEMPT#{attempt_id}#META"
            and item.get("record_type") == "attempt_meta"
        ]
        if len(attempt_metas) != 1 or attempt_metas[0].get("phase") != "completed":
            raise ValueError("source completed attempt metadata is invalid")
        persisted_schema = debate_meta.get("schema_version")
        if attempt_metas[0].get("schema_version") != persisted_schema:
            raise ValueError("source completion metadata schema is inconsistent")
        return LoadedSourceDebate(
            snapshot=deserialize_snapshot(items),
            schema_version=cast(int, persisted_schema),
        )

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

    def load_affection_profile(self, partition_key: str) -> DynamoItem:
        profile = self.find_affection_profile(partition_key)
        if profile is None:
            raise ValueError("source affection profile does not exist")
        return profile

    def find_affection_profile(self, partition_key: str) -> DynamoItem | None:
        """Strongly read one profile without conflating absence with invalid input."""

        if not partition_key.startswith("AFFECTION#REQUESTER#"):
            raise ValueError("source partition key is not an affection profile")
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": partition_key, "SK": "PROFILE"}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)


class ArchiveRepository:
    """Write immutable projections using one bounded DynamoDB transaction."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def put_projection(self, projection: ArchiveProjection) -> bool:
        marker_key = {
            "PK": f"RECORD#{projection.record_id}",
            "SK": f"PROJECTION#V{projection.schema_version}",
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


class AffectionProjectionRepository:
    """Converge one opaque affection profile without retaining its private source ID."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def put_profile(self, item: DynamoItem) -> bool:
        version = item.get("source_version")
        updated_at = item.get("updated_at")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("affection projection version is invalid")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("affection projection timestamp is invalid")
        condition = (
            "attribute_not_exists(PK) OR attribute_not_exists(source_version) "
            "OR source_version = :seed_version "
            "OR (source_version < :version AND updated_at <= :updated_at)"
        )
        condition_values: DynamoItem = {
            ":seed_version": 0,
            ":version": version,
            ":updated_at": updated_at,
        }
        if _is_defaults_only_profile_upgrade(item):
            condition += (
                " OR (source_version = :version AND schema_version = :legacy_schema "
                "AND record_type = :record_type AND display_name = :display_name "
                "AND scores = :scores AND updated_at = :updated_at)"
            )
            condition_values.update(
                {
                    ":legacy_schema": 1,
                    ":record_type": "affection_profile",
                    ":display_name": item["display_name"],
                    ":scores": item["scores"],
                }
            )
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(item),
                ConditionExpression=condition,
                ExpressionAttributeValues=marshal_item(condition_values),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            existing = self._get({"PK": item["PK"], "SK": item["SK"]})
            if existing is None:
                raise
            existing_version = existing.get("source_version")
            existing_updated_at = existing.get("updated_at")
            if (
                isinstance(existing_version, bool)
                or not isinstance(existing_version, int)
                or not isinstance(existing_updated_at, str)
                or not existing_updated_at
            ):
                raise ProjectionConflict("affection projection version is inconsistent") from error
            if existing == item:
                return False
            if existing_version == version:
                raise ProjectionConflict(
                    "affection projection content conflicts at one version"
                ) from error
            if existing_version > version and existing_updated_at >= updated_at:
                return False
            raise ProjectionConflict(
                "affection projection version and timestamp ordering conflict"
            ) from error
        return True

    def _get(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)


def _is_defaults_only_profile_upgrade(item: DynamoItem) -> bool:
    return (
        item.get("schema_version") == 2
        and item.get("record_type") == "affection_profile"
        and item.get("reset_count") == 0
        and item.get("memorial_cycle") == 1
        and not {
            "unlocked_participant",
            "unlocked_at",
            "unlock_record_id",
            "unlock_display_name",
            "unlock_memorial_cycle",
            "unlock_retroactive",
        }.intersection(item)
        and isinstance(item.get("display_name"), str)
        and isinstance(item.get("scores"), dict)
    )


class StatisticsRepository:
    """Persist only the internal resumable backfill checkpoint."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load_backfill_checkpoint(self, *, mode: BackfillMode) -> BackfillCheckpoint | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(self._key(mode)),
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        decoded = unmarshal_item(item)
        if (
            decoded.get("schema_version") != 1
            or decoded.get("record_type") != "backfill_checkpoint"
            or decoded.get("mode") != mode
            or not isinstance(decoded.get("complete"), bool)
        ):
            raise ValueError("backfill checkpoint is invalid")
        for field in (
            "candidate_count",
            "validated_count",
            "projected_count",
            "skipped_count",
        ):
            value = decoded.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("backfill checkpoint count is invalid")
        candidate_count = cast(int, decoded["candidate_count"])
        validated_count = cast(int, decoded["validated_count"])
        projected_count = cast(int, decoded["projected_count"])
        skipped_count = cast(int, decoded["skipped_count"])
        if validated_count != candidate_count:
            raise ValueError("backfill checkpoint candidates were not all validated")
        if projected_count + skipped_count > validated_count:
            raise ValueError("backfill checkpoint projection counts are invalid")
        if mode == "dry-run" and (projected_count != 0 or skipped_count != 0):
            raise ValueError("dry-run checkpoint contains Archive writes")
        complete = cast(bool, decoded["complete"])
        raw_key = decoded.get("exclusive_start_key")
        if complete and raw_key is not None:
            raise ValueError("completed backfill checkpoint still has a cursor")
        if not complete and not isinstance(raw_key, Mapping):
            raise ValueError("incomplete backfill checkpoint has no cursor")
        return BackfillCheckpoint(
            exclusive_start_key=cast(DynamoItem | None, raw_key),
            complete=complete,
            candidate_count=candidate_count,
            validated_count=validated_count,
            projected_count=projected_count,
            skipped_count=skipped_count,
        )

    def save_backfill_checkpoint(
        self,
        *,
        mode: BackfillMode,
        exclusive_start_key: DynamoItem | None,
        candidate_count: int,
        validated_count: int,
        projected_count: int,
        skipped_count: int,
        updated_at: str,
    ) -> None:
        counts = (candidate_count, validated_count, projected_count, skipped_count)
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts
        ):
            raise ValueError("backfill checkpoint counts must be non-negative")
        if validated_count != candidate_count:
            raise ValueError("backfill checkpoint candidates must all be validated")
        if projected_count + skipped_count > validated_count:
            raise ValueError("backfill checkpoint projection counts are invalid")
        if mode == "dry-run" and (projected_count != 0 or skipped_count != 0):
            raise ValueError("dry-run checkpoint cannot contain Archive writes")
        item: DynamoItem = {
            **self._key(mode),
            "schema_version": 1,
            "record_type": "backfill_checkpoint",
            "mode": mode,
            "candidate_count": candidate_count,
            "validated_count": validated_count,
            "projected_count": projected_count,
            "skipped_count": skipped_count,
            "updated_at": updated_at,
            "complete": exclusive_start_key is None,
        }
        if exclusive_start_key is not None:
            item["exclusive_start_key"] = exclusive_start_key
        self._client.put_item(TableName=self._table_name, Item=marshal_item(item))

    @staticmethod
    def _key(mode: BackfillMode) -> dict[str, DynamoValue]:
        if mode not in {"dry-run", "apply"}:
            raise ValueError("backfill checkpoint mode is invalid")
        return {
            "PK": "CHECKPOINT#BACKFILL",
            "SK": f"ARCHIVE#V1#{mode.upper()}",
        }


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
