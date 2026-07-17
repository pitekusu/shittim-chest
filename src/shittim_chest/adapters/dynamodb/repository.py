"""Fenced, transactional DynamoDB repository implementation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        QueryInputTypeDef,
        TransactWriteItemTypeDef,
    )
else:
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.models import PanelOperation, PanelOperationKind
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    deserialize_panel_operation,
    deserialize_snapshot,
    serialize_panel_operation,
    serialize_snapshot,
)
from shittim_chest.application.models import DebateSnapshot, LeaseGrant
from shittim_chest.application.ports import (
    RepositoryBusy,
    RepositoryConflict,
    RepositoryQuotaExceeded,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

LEASE_SECONDS = 60
DAILY_GUILD_QUOTA = 30
GLOBAL_LEASE_SLOTS = 3
RECOVERABLE_INDEX = "gsi2"
_JST = ZoneInfo("Asia/Tokyo")


def create_dynamodb_client(
    *,
    region_name: str = "ap-northeast-1",
    endpoint_url: str | None = None,
) -> DynamoDBClient:
    """Create one reusable client with bounded standard-mode SDK retries."""

    return boto3.client(
        "dynamodb",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=2,
            read_timeout=5,
            max_pool_connections=4,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )


@dataclass(frozen=True, slots=True)
class _SlotCandidate:
    grant: LeaseGrant
    action: TransactWriteItemTypeDef


class DynamoDbDebateRepository:
    """Store debate aggregates with durable idempotency and fenced leases."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def get_operation_result(self, operation_id: str) -> DebateSnapshot | None:
        return await asyncio.to_thread(self._get_operation_result, operation_id)

    async def create(
        self,
        snapshot: DebateSnapshot,
        *,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._create,
            snapshot,
            operation_id,
            lease_owner,
        )

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None:
        return await asyncio.to_thread(self._load_snapshot, debate_id, None)

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None = None,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(self._replace, expected, updated, operation_id)

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._create_retry,
            expected_failed,
            retry,
            operation_id,
            lease_owner,
        )

    async def claim_recoverable(
        self,
        *,
        lease_owner: str,
        at: datetime,
    ) -> tuple[DebateSnapshot, ...]:
        return await asyncio.to_thread(self._claim_recoverable, lease_owner, at)

    async def renew_lease(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
    ) -> LeaseGrant:
        return await asyncio.to_thread(self._renew_lease, expected, at)

    def _create(
        self,
        snapshot: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot:
        existing = self._get_operation_result(operation_id)
        if existing is not None:
            return existing
        now = snapshot.state.updated_at
        candidates = self._slot_candidates(lease_owner, now)
        if not candidates:
            raise RepositoryBusy("all global lease slots are occupied")

        for candidate in candidates:
            persisted = replace(snapshot, lease=candidate.grant)
            operation = _panel_operation(
                persisted,
                operation_id=operation_id,
                kind=PanelOperationKind.ACCEPT,
                source_attempt_id=persisted.state.attempt_id,
            )
            actions = [
                candidate.action,
                self._quota_action(persisted.guild_id, now),
                *(self._put_new(item) for item in serialize_snapshot(persisted)),
                self._put_new(serialize_panel_operation(operation)),
            ]
            try:
                token_source = f"{self._table_name}:{operation_id}"
                self._transact(actions, token=_client_token(token_source, candidate.grant.slot))
                return persisted
            except RepositoryConflict:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    return replay
                if self._quota_count(persisted.guild_id, now) >= DAILY_GUILD_QUOTA:
                    raise RepositoryQuotaExceeded(
                        "daily Guild acceptance quota exhausted"
                    ) from None
                if self._load_snapshot(persisted.state.debate_id, None) is not None:
                    raise RepositoryConflict("debate ID already exists") from None
        raise RepositoryBusy("all global lease slots were claimed concurrently")

    def _replace(
        self,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None,
    ) -> DebateSnapshot:
        if operation_id is not None:
            replay = self._get_operation_result(operation_id)
            if replay is not None:
                return replay
        _require_same_attempt(expected, updated)
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("active write requires a fenced lease")
        persisted = replace(updated, lease=None) if updated.state.phase.is_terminal else updated
        old_items = _items_by_key(serialize_snapshot(expected))
        new_items = _items_by_key(serialize_snapshot(persisted))
        attempt_key = _attempt_key(expected.state.debate_id, expected.state.attempt_id)
        attempt_tuple = (_text(attempt_key, "PK"), _text(attempt_key, "SK"))
        attempt_item = new_items.pop(attempt_tuple)
        actions = [
            self._update_expected_attempt(
                previous=old_items[attempt_tuple],
                updated=attempt_item,
                expected=expected,
                write_at=persisted.state.updated_at,
            )
        ]
        for key, item in new_items.items():
            if old_items.get(key) != item:
                actions.append(self._put(item))
        if persisted.state.phase.is_terminal:
            actions.append(self._release_slot_action(lease, persisted.state.updated_at))
        if operation_id is not None:
            operation = _panel_operation(
                persisted,
                operation_id=operation_id,
                kind=PanelOperationKind.CANCEL,
                source_attempt_id=persisted.state.attempt_id,
            )
            actions.append(self._put_new(serialize_panel_operation(operation)))
        try:
            token_source = ":".join(
                (
                    self._table_name,
                    operation_id or "replace",
                    str(updated.state.debate_id),
                    str(updated.state.attempt_id),
                    str(updated.state.updated_at),
                )
            )
            self._transact(actions, token=_client_token(token_source))
        except RepositoryConflict:
            if operation_id is not None:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    return replay
            raise
        return persisted

    def _create_retry(
        self,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot:
        replay = self._get_operation_result(operation_id)
        if replay is not None:
            return replay
        if expected_failed.state.phase is not DebatePhase.FAILED:
            raise RepositoryConflict("retry source is not failed")
        if retry.state.retry_of != expected_failed.state.attempt_id:
            raise RepositoryConflict("retry source attempt does not match")
        candidates = self._slot_candidates(lease_owner, retry.state.updated_at)
        if not candidates:
            raise RepositoryBusy("all global lease slots are occupied")

        for candidate in candidates:
            persisted = replace(retry, lease=candidate.grant)
            items = _items_by_key(serialize_snapshot(persisted))
            debate_key = _debate_key(persisted.state.debate_id)
            debate_item = items.pop((_text(debate_key, "PK"), _text(debate_key, "SK")))
            attempt_key = _attempt_key(persisted.state.debate_id, persisted.state.attempt_id)
            attempt_item = items.pop((_text(attempt_key, "PK"), _text(attempt_key, "SK")))
            operation = _panel_operation(
                persisted,
                operation_id=operation_id,
                kind=PanelOperationKind.RETRY,
                source_attempt_id=expected_failed.state.attempt_id,
            )
            actions: list[TransactWriteItemTypeDef] = [
                candidate.action,
                self._condition_failed_attempt(expected_failed),
                self._put_current_attempt(debate_item, expected_failed.state.attempt_id),
                self._put_new(attempt_item),
                *(self._put_new(item) for item in items.values()),
                self._put_new(serialize_panel_operation(operation)),
            ]
            try:
                token_source = f"{self._table_name}:{operation_id}"
                self._transact(actions, token=_client_token(token_source, candidate.grant.slot))
                return persisted
            except RepositoryConflict:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    return replay
                current = self._load_snapshot(expected_failed.state.debate_id, None)
                if current is None or current.state.attempt_id != expected_failed.state.attempt_id:
                    raise RepositoryConflict("retry source is no longer current") from None
        raise RepositoryBusy("all global lease slots were claimed concurrently")

    def _claim_recoverable(
        self,
        lease_owner: str,
        at: datetime,
    ) -> tuple[DebateSnapshot, ...]:
        _require_utc(at)
        claimed: list[DebateSnapshot] = []
        for candidate_item in self._query_recoverable():
            if len(claimed) == GLOBAL_LEASE_SLOTS:
                break
            debate_id = DebateId.parse(_text(candidate_item, "debate_id"))
            attempt_id = AttemptId.parse(_text(candidate_item, "attempt_id"))
            snapshot = self._load_snapshot(debate_id, attempt_id)
            if snapshot is None or snapshot.state.phase.is_terminal:
                continue
            if snapshot.lease is not None and snapshot.lease.expires_at >= at:
                continue
            acquired = self._claim_one(snapshot, lease_owner, at)
            if acquired is not None:
                claimed.append(acquired)
        return tuple(claimed)

    def _claim_one(
        self,
        snapshot: DebateSnapshot,
        lease_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        for candidate in self._slot_candidates(lease_owner, at):
            values = marshal_item(
                {
                    ":owner": lease_owner,
                    ":slot": candidate.grant.slot,
                    ":token": candidate.grant.fencing_token,
                    ":expiry": _timestamp(candidate.grant.expires_at),
                    ":now": _timestamp(at),
                }
            )
            attempt_update = cast(
                TransactWriteItemTypeDef,
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": marshal_item(
                            _attempt_key(snapshot.state.debate_id, snapshot.state.attempt_id)
                        ),
                        "UpdateExpression": (
                            "SET lease_owner=:owner, lease_slot=:slot, fencing_token=:token, "
                            "lease_expiry=:expiry"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(PK) AND (attribute_not_exists(lease_expiry) "
                            "OR lease_expiry < :now)"
                        ),
                        "ExpressionAttributeValues": values,
                    }
                },
            )
            current_check = cast(
                TransactWriteItemTypeDef,
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": marshal_item(_debate_key(snapshot.state.debate_id)),
                        "ConditionExpression": "current_attempt_id=:attempt",
                        "ExpressionAttributeValues": marshal_item(
                            {":attempt": str(snapshot.state.attempt_id)}
                        ),
                    }
                },
            )
            try:
                token = (
                    f"{self._table_name}:claim:{snapshot.state.attempt_id}:"
                    f"{candidate.grant.slot}:{candidate.grant.fencing_token}"
                )
                self._transact([candidate.action, current_check, attempt_update], token=token)
                return replace(snapshot, lease=candidate.grant)
            except RepositoryConflict:
                continue
        return None

    def _renew_lease(self, expected: DebateSnapshot, at: datetime) -> LeaseGrant:
        _require_utc(at)
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("cannot renew an unleased attempt")
        renewed = replace(lease, expires_at=at + timedelta(seconds=LEASE_SECONDS))
        values = marshal_item(
            {
                ":owner": lease.owner_id,
                ":token": lease.fencing_token,
                ":old_expiry": _timestamp(lease.expires_at),
                ":new_expiry": _timestamp(renewed.expires_at),
                ":now": _timestamp(at),
            }
        )
        condition = (
            "lease_owner=:owner AND fencing_token=:token AND lease_expiry=:old_expiry "
            "AND lease_expiry >= :now"
        )
        actions = [
            cast(
                TransactWriteItemTypeDef,
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": marshal_item(
                            _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                        ),
                        "UpdateExpression": "SET lease_expiry=:new_expiry",
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": values,
                    }
                },
            ),
            cast(
                TransactWriteItemTypeDef,
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": marshal_item(_slot_key(lease.slot)),
                        "UpdateExpression": "SET lease_expiry=:new_expiry",
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": values,
                    }
                },
            ),
        ]
        token_source = f"{self._table_name}:renew:{expected.state.attempt_id}:{at}"
        self._transact(actions, token=_client_token(token_source))
        return renewed

    def _get_operation_result(self, operation_id: str) -> DebateSnapshot | None:
        item = self._get_item(_operation_key(operation_id))
        if item is None:
            return None
        operation = deserialize_panel_operation(item)
        snapshot = self._load_snapshot(operation.debate_id, operation.result_attempt_id)
        if snapshot is None:
            raise RepositoryConflict("operation result points to a missing attempt")
        return snapshot

    def _load_snapshot(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId | None,
    ) -> DebateSnapshot | None:
        items = self._query_partition(f"DEBATE#{debate_id}", consistent=True)
        if not items:
            return None
        if attempt_id is not None:
            adjusted: list[DynamoItem] = []
            for item in items:
                if item.get("record_type") == "debate_meta":
                    adjusted.append({**item, "current_attempt_id": str(attempt_id)})
                else:
                    adjusted.append(item)
            items = adjusted
        return deserialize_snapshot(items)

    def _slot_candidates(self, lease_owner: str, at: datetime) -> tuple[_SlotCandidate, ...]:
        if not lease_owner.strip():
            raise ValueError("lease owner must not be empty")
        _require_utc(at)
        candidates: list[_SlotCandidate] = []
        for slot in range(GLOBAL_LEASE_SLOTS):
            item = self._get_item(_slot_key(slot))
            previous_token = 0 if item is None else _integer(item, "fencing_token")
            expiry = None if item is None else _optional_timestamp(item, "lease_expiry")
            if expiry is not None and expiry >= at:
                continue
            grant = LeaseGrant(
                owner_id=lease_owner,
                slot=slot,
                fencing_token=previous_token + 1,
                expires_at=at + timedelta(seconds=LEASE_SECONDS),
            )
            if item is None:
                control: DynamoItem = {
                    **_slot_key(slot),
                    "record_type": "lease_slot",
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "slot": slot,
                    "lease_owner": lease_owner,
                    "lease_expiry": _timestamp(grant.expires_at),
                    "fencing_token": grant.fencing_token,
                    "created_at": _timestamp(at),
                    "updated_at": _timestamp(at),
                }
                action = self._put_new(control)
            else:
                action = cast(
                    TransactWriteItemTypeDef,
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": marshal_item(_slot_key(slot)),
                            "UpdateExpression": (
                                "SET lease_owner=:owner, lease_expiry=:expiry, "
                                "fencing_token=:next, updated_at=:now"
                            ),
                            "ConditionExpression": (
                                "fencing_token=:previous AND "
                                "(attribute_not_exists(lease_expiry) OR lease_expiry < :now)"
                            ),
                            "ExpressionAttributeValues": marshal_item(
                                {
                                    ":owner": lease_owner,
                                    ":expiry": _timestamp(grant.expires_at),
                                    ":next": grant.fencing_token,
                                    ":previous": previous_token,
                                    ":now": _timestamp(at),
                                }
                            ),
                        }
                    },
                )
            candidates.append(_SlotCandidate(grant, action))
        return tuple(candidates)

    def _quota_action(self, guild_id: str, at: datetime) -> TransactWriteItemTypeDef:
        day = at.astimezone(_JST).date().isoformat()
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": f"QUOTA#GUILD#{guild_id}", "SK": f"DAY#{day}"}),
                    "UpdateExpression": (
                        "SET #count=if_not_exists(#count,:zero)+:one, "
                        "record_type=:type, schema_version=:schema, "
                        "created_at=if_not_exists(created_at,:at), updated_at=:at"
                    ),
                    "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":limit": DAILY_GUILD_QUOTA,
                            ":type": "guild_daily_quota",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _quota_count(self, guild_id: str, at: datetime) -> int:
        day = at.astimezone(_JST).date().isoformat()
        item = self._get_item({"PK": f"QUOTA#GUILD#{guild_id}", "SK": f"DAY#{day}"})
        return 0 if item is None else _integer(item, "count")

    def _update_expected_attempt(
        self,
        *,
        previous: DynamoItem,
        updated: DynamoItem,
        expected: DebateSnapshot,
        write_at: datetime,
    ) -> TransactWriteItemTypeDef:
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("expected attempt has no lease")
        key_fields = {"PK", "SK"}
        lease_fields = {"lease_owner", "lease_slot", "lease_expiry", "fencing_token"}
        set_fields = sorted(set(updated) - key_fields - lease_fields)
        remove_fields = sorted(set(previous) - set(updated) - key_fields)
        names = {"#phase": "phase"}
        values: DynamoItem = {
            ":phase": expected.state.phase.value,
            ":recovery": expected.state.recovery_state.value,
            ":expected_updated": _timestamp(expected.state.updated_at),
            ":owner": lease.owner_id,
            ":token": lease.fencing_token,
            ":at": _timestamp(write_at),
        }
        assignments: list[str] = []
        for index, field in enumerate(set_fields):
            name = f"#set{index}"
            value = f":set{index}"
            names[name] = field
            values[value] = updated[field]
            assignments.append(f"{name}={value}")
        removals: list[str] = []
        for index, field in enumerate(remove_fields):
            name = f"#remove{index}"
            names[name] = field
            removals.append(name)
        update_expression = f"SET {', '.join(assignments)}"
        if removals:
            update_expression += f" REMOVE {', '.join(removals)}"
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                    ),
                    "UpdateExpression": update_expression,
                    "ConditionExpression": (
                        "#phase=:phase AND recovery_state=:recovery "
                        "AND updated_at=:expected_updated "
                        "AND lease_owner=:owner AND fencing_token=:token AND lease_expiry >= :at"
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _condition_failed_attempt(self, expected: DebateSnapshot) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                    ),
                    "ConditionExpression": "#phase=:failed AND updated_at=:updated",
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":failed": DebatePhase.FAILED.value,
                            ":updated": _timestamp(expected.state.updated_at),
                        }
                    ),
                }
            },
        )

    def _put_current_attempt(
        self,
        item: DynamoItem,
        expected_attempt_id: AttemptId,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(item),
                    "ConditionExpression": "current_attempt_id=:expected",
                    "ExpressionAttributeValues": marshal_item(
                        {":expected": str(expected_attempt_id)}
                    ),
                }
            },
        )

    def _release_slot_action(
        self,
        lease: LeaseGrant,
        at: datetime,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_slot_key(lease.slot)),
                    "UpdateExpression": "SET updated_at=:at REMOVE lease_owner, lease_expiry",
                    "ConditionExpression": "lease_owner=:owner AND fencing_token=:token",
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":owner": lease.owner_id,
                            ":token": lease.fencing_token,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _put(self, item: DynamoItem) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {"Put": {"TableName": self._table_name, "Item": marshal_item(item)}},
        )

    def _put_new(self, item: DynamoItem) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(item),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            },
        )

    def _transact(self, actions: Iterable[TransactWriteItemTypeDef], *, token: str) -> None:
        action_list = list(actions)
        if not 1 <= len(action_list) <= 100:
            raise ValueError("DynamoDB transaction must contain between 1 and 100 actions")
        try:
            self._client.transact_write_items(
                TransactItems=action_list,
                ClientRequestToken=token[:36],
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            raise RepositoryConflict("DynamoDB transaction condition failed") from error
        except self._client.exceptions.IdempotentParameterMismatchException as error:
            raise RepositoryConflict("transaction token was reused with different input") from error

    def _get_item(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)

    def _query_partition(self, partition_key: str, *, consistent: bool) -> list[DynamoItem]:
        items: list[DynamoItem] = []
        exclusive_start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK=:pk",
                "ExpressionAttributeValues": marshal_item({":pk": partition_key}),
                "ConsistentRead": consistent,
            }
            if exclusive_start_key is not None:
                parameters["ExclusiveStartKey"] = exclusive_start_key
            response = self._client.query(**parameters)
            items.extend(unmarshal_item(item) for item in response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                return items

    def _query_recoverable(self) -> list[DynamoItem]:
        items: list[DynamoItem] = []
        exclusive_start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "IndexName": RECOVERABLE_INDEX,
                "KeyConditionExpression": "gsi2pk=:recoverable",
                "ExpressionAttributeValues": marshal_item({":recoverable": "RECOVERABLE"}),
                "ConsistentRead": False,
            }
            if exclusive_start_key is not None:
                parameters["ExclusiveStartKey"] = exclusive_start_key
            response = self._client.query(**parameters)
            items.extend(unmarshal_item(item) for item in response.get("Items", []))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                return items


def _panel_operation(
    snapshot: DebateSnapshot,
    *,
    operation_id: str,
    kind: PanelOperationKind,
    source_attempt_id: AttemptId,
) -> PanelOperation:
    return PanelOperation(
        operation_id=operation_id,
        kind=kind,
        debate_id=snapshot.state.debate_id,
        source_attempt_id=source_attempt_id,
        result_attempt_id=snapshot.state.attempt_id,
        guild_id=snapshot.guild_id,
        channel_id=snapshot.channel_id,
        requester_id=snapshot.requester_id,
        created_at=snapshot.state.updated_at,
        thread_id=snapshot.thread_id,
        message_id=snapshot.starter_message_id,
    )


def _items_by_key(items: Iterable[DynamoItem]) -> dict[tuple[str, str], DynamoItem]:
    return {(_text(item, "PK"), _text(item, "SK")): item for item in items}


def _debate_key(debate_id: DebateId) -> DynamoItem:
    return {"PK": f"DEBATE#{debate_id}", "SK": "META"}


def _attempt_key(debate_id: DebateId, attempt_id: AttemptId) -> DynamoItem:
    return {"PK": f"DEBATE#{debate_id}", "SK": f"ATTEMPT#{attempt_id}#META"}


def _operation_key(operation_id: str) -> DynamoItem:
    return {"PK": f"OPERATION#{operation_id}", "SK": "RESULT"}


def _slot_key(slot: int) -> DynamoItem:
    return {"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{slot}"}


def _require_same_attempt(expected: DebateSnapshot, updated: DebateSnapshot) -> None:
    if (
        expected.state.debate_id != updated.state.debate_id
        or expected.state.attempt_id != updated.state.attempt_id
    ):
        raise RepositoryConflict("replace cannot change debate or attempt identity")


def _client_token(value: str, slot: int | None = None) -> str:
    suffix = "" if slot is None else f"-{slot}"
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}{suffix}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _optional_timestamp(item: Mapping[str, DynamoValue], field: str) -> datetime | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepositoryConflict(f"{field} is not a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_utc(parsed)
    return parsed


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")


def _text(item: Mapping[str, DynamoValue], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value


def _integer(item: Mapping[str, DynamoValue], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value
