"""Strongly consistent, generation-fenced singleton runtime persistence."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef
else:
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    deserialize_runtime_state,
    deserialize_runtime_wake_result,
    serialize_runtime_state,
    serialize_runtime_wake_result,
)
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.scale_to_zero import (
    INGRESS_QUEUE_LIMIT,
    IngressOperationResult,
    IngressRequest,
    RuntimeState,
    RuntimeWakeResult,
)

RUNTIME_RECORD_SCHEMA_VERSION = 1
RUNTIME_CAS_ATTEMPTS = INGRESS_QUEUE_LIMIT + 1
_ACTIVE_INGRESS_STATUSES = frozenset({"pending", "claimed", "retrying"})
_RUNTIME_STATE_ATTRIBUTES = (
    "record_type",
    "schema_version",
    "record_schema_version",
    "state",
    "generation",
    "desired_count",
    "version",
    "updated_at",
    "runtime_instance_id",
    "wake_started_at",
    "last_request_at",
    "started_at",
    "ready_at",
    "busy_since",
    "idle_since",
    "stop_eligible_at",
    "stopping_at",
    "stopped_at",
    "last_error_code",
    "last_reconciled_at",
)


class DynamoDbRuntimeStateRepository:
    """Store one runtime aggregate and immutable per-interaction wake results."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def get(self) -> RuntimeState | None:
        return await asyncio.to_thread(self._get)

    async def request_wake(
        self,
        *,
        interaction_id: str,
        at: datetime,
    ) -> RuntimeState:
        return await asyncio.to_thread(self._request_wake, interaction_id, at)

    async def replace(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
    ) -> RuntimeState:
        return await asyncio.to_thread(self._replace, expected, updated)

    def _get(self) -> RuntimeState | None:
        item = self._get_item(_runtime_key())
        return None if item is None else deserialize_runtime_state(item)

    def _request_wake(self, interaction_id: str, at: datetime) -> RuntimeState:
        if not interaction_id.strip():
            raise ValueError("interaction ID must not be empty")
        _require_utc(at)
        operation = self._load_ingress_operation(interaction_id)
        request = self._load_ingress_request(operation)
        replay = self._get_wake_result(interaction_id)
        if replay is not None:
            return self._replay_wake(replay)
        _require_active_operation(operation)

        for _attempt in range(RUNTIME_CAS_ATTEMPTS):
            previous = self._get()
            effective_at = at if previous is None else max(at, previous.updated_at)
            baseline = previous or RuntimeState.stopped(at=effective_at)
            updated = baseline.request_wake(at=effective_at)
            _require_wakeable_request(operation, request, effective_at)
            result = RuntimeWakeResult(
                interaction_id=interaction_id,
                generation=updated.generation,
                runtime_version=updated.version,
                recorded_at=effective_at,
            )
            try:
                self._transact_wake(
                    operation=operation,
                    request=request,
                    previous=previous,
                    updated=updated,
                    result=result,
                )
                return updated
            except RepositoryConflict:
                replay = self._get_wake_result(interaction_id)
                if replay is not None:
                    return self._replay_wake(replay)
                operation = self._load_ingress_operation(interaction_id)
                _require_active_operation(operation)
                request = self._load_ingress_request(operation)
        raise RepositoryConflict("runtime wake lost repeated conditional-write races")

    def _replace(self, expected: RuntimeState, updated: RuntimeState) -> RuntimeState:
        expected.validate_replacement(updated)
        if expected == updated:
            current = self._get()
            if current == expected:
                return expected
            raise RepositoryConflict("runtime state changed before no-op replacement")
        condition, names, values = _runtime_cas(expected)
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(serialize_runtime_state(updated)),
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=marshal_item(values),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.ConditionalCheckFailedException as error:
            current = self._get()
            if current == updated:
                return updated
            raise RepositoryConflict("runtime state changed before replacement") from error
        return updated

    def _transact_wake(
        self,
        *,
        operation: IngressOperationResult,
        request: IngressRequest,
        previous: RuntimeState | None,
        updated: RuntimeState,
        result: RuntimeWakeResult,
    ) -> None:
        operation_check = cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_operation_key(operation.interaction_id)),
                    "ConditionExpression": (
                        "record_type=:operation_type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                        "AND request_sort_key=:request_sort_key AND created_at=:created_at "
                        "AND #operation_status IN (:pending,:claimed,:retrying)"
                    ),
                    "ExpressionAttributeNames": {"#operation_status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":operation_type": "ingress_operation_result",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": operation.schema_version,
                            ":interaction_id": operation.interaction_id,
                            ":operation_id": operation.operation_id,
                            ":request_sort_key": operation.request_sort_key,
                            ":created_at": _timestamp(operation.created_at),
                            ":pending": "pending",
                            ":claimed": "claimed",
                            ":retrying": "retrying",
                        }
                    ),
                }
            },
        )
        request_check = cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_request_key(operation.request_sort_key)),
                    "ConditionExpression": (
                        "record_type=:request_type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                        "AND created_at=:created_at "
                        "AND #request_status IN (:pending,:claimed,:retrying) "
                        "AND terminal_deadline_at > :at"
                    ),
                    "ExpressionAttributeNames": {"#request_status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":request_type": "ingress_request",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": request.schema_version,
                            ":interaction_id": request.interaction_id,
                            ":operation_id": request.operation_id,
                            ":created_at": _timestamp(request.created_at),
                            ":pending": "pending",
                            ":claimed": "claimed",
                            ":retrying": "retrying",
                            ":at": _timestamp(result.recorded_at),
                        }
                    ),
                }
            },
        )
        wake_put = cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_runtime_wake_result(result)),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            },
        )
        runtime_put: TransactWriteItemTypeDef
        if previous is None:
            runtime_put = cast(
                TransactWriteItemTypeDef,
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(serialize_runtime_state(updated)),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
                    }
                },
            )
        else:
            condition, names, values = _runtime_cas(previous)
            runtime_put = cast(
                TransactWriteItemTypeDef,
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(serialize_runtime_state(updated)),
                        "ConditionExpression": condition,
                        "ExpressionAttributeNames": names,
                        "ExpressionAttributeValues": marshal_item(values),
                    }
                },
            )
        token_source = ":".join(
            (
                self._table_name,
                result.interaction_id,
                "missing" if previous is None else str(previous.version),
                str(result.generation),
                str(result.runtime_version),
            )
        )
        try:
            self._client.transact_write_items(
                TransactItems=[operation_check, request_check, wake_put, runtime_put],
                ClientRequestToken=_client_token(token_source),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            raise RepositoryConflict("runtime wake transaction condition failed") from error
        except self._client.exceptions.IdempotentParameterMismatchException as error:
            raise RepositoryConflict("runtime wake transaction token input changed") from error

    def _load_ingress_operation(self, interaction_id: str) -> IngressOperationResult:
        item = self._get_item(_operation_key(interaction_id))
        if item is None:
            raise RepositoryConflict("runtime wake requires a persisted ingress operation")
        operation = deserialize_ingress_operation_result(item)
        if operation.interaction_id != interaction_id:
            raise RepositoryConflict("ingress operation belongs to another interaction")
        return operation

    def _get_wake_result(self, interaction_id: str) -> RuntimeWakeResult | None:
        item = self._get_item(_wake_key(interaction_id))
        return None if item is None else deserialize_runtime_wake_result(item)

    def _load_ingress_request(self, operation: IngressOperationResult) -> IngressRequest:
        item = self._get_item(_request_key(operation.request_sort_key))
        if item is None:
            raise RepositoryConflict("runtime wake requires a persisted ingress request")
        request = deserialize_ingress_request(item)
        if (
            request.interaction_id != operation.interaction_id
            or request.operation_id != operation.operation_id
        ):
            raise RepositoryConflict("ingress request and operation result do not match")
        return request

    def _replay_wake(self, result: RuntimeWakeResult) -> RuntimeState:
        current = self._get()
        if current is None:
            raise RepositoryConflict("runtime wake result points to a missing runtime state")
        if current.generation < result.generation or current.version < result.runtime_version:
            raise RepositoryConflict("runtime wake result is ahead of runtime state")
        return current

    def _get_item(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)


def _runtime_key() -> DynamoItem:
    return {"PK": "CONTROL#RUNTIME", "SK": "STATE"}


def _operation_key(interaction_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RESULT"}


def _wake_key(interaction_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RUNTIME_WAKE"}


def _request_key(request_sort_key: str) -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": request_sort_key}


def _runtime_cas(
    state: RuntimeState,
) -> tuple[str, dict[str, str], DynamoItem]:
    item = serialize_runtime_state(state)
    names: dict[str, str] = {}
    values: DynamoItem = {}
    conditions: list[str] = []
    for index, attribute in enumerate(_RUNTIME_STATE_ATTRIBUTES):
        name = f"#a{index}"
        names[name] = attribute
        if attribute in item:
            value = f":v{index}"
            values[value] = item[attribute]
            conditions.append(f"{name}={value}")
        else:
            conditions.append(f"attribute_not_exists({name})")
    return " AND ".join(conditions), names, values


def _require_active_operation(operation: IngressOperationResult) -> None:
    if operation.status.value not in _ACTIVE_INGRESS_STATUSES:
        raise RepositoryConflict("terminal ingress operation cannot wake the runtime")


def _require_wakeable_request(
    operation: IngressOperationResult,
    request: IngressRequest,
    at: datetime,
) -> None:
    if (
        request.interaction_id != operation.interaction_id
        or request.operation_id != operation.operation_id
    ):
        raise RepositoryConflict("ingress request and operation result do not match")
    if not request.status.counts_toward_queue_limit:
        raise RepositoryConflict("terminal ingress request cannot wake the runtime")
    if request.terminal_deadline_at <= at:
        raise RepositoryConflict("ingress request terminal deadline prevents runtime wake")


def _client_token(value: str) -> str:
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
