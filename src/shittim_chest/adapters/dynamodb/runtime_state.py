"""Strongly consistent, generation-fenced singleton runtime persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef
else:
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import INGRESS_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.outbox import (
    OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
    GLOBAL_LEASE_SLOTS,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    IngressActivePointer,
    PersistenceFormatError,
    deserialize_ingress_active_pointer,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    deserialize_runtime_state,
    deserialize_runtime_wake_result,
    ingress_request_sort_key,
    serialize_runtime_state,
    serialize_runtime_wake_result,
)
from shittim_chest.adapters.dynamodb.transaction_errors import (
    is_condition_only_cancellation,
)
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable
from shittim_chest.application.scale_to_zero import (
    INGRESS_QUEUE_LIMIT,
    IngressOperationResult,
    IngressRequest,
    RuntimeState,
    RuntimeStatus,
    RuntimeWakeResult,
)

RUNTIME_RECORD_SCHEMA_VERSION = 1
RUNTIME_ACTIVITY_SCHEMA_VERSION = 1
RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE = "runtime_activity_schema"
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
        try:
            return await asyncio.to_thread(self._get)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    async def request_wake(
        self,
        *,
        interaction_id: str,
        at: datetime,
    ) -> RuntimeState:
        try:
            return await asyncio.to_thread(self._request_wake, interaction_id, at)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    async def ensure_wake(
        self,
        *,
        interaction_id: str,
        at: datetime,
    ) -> RuntimeState:
        try:
            return await asyncio.to_thread(self._ensure_wake, interaction_id, at)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    async def replace(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
    ) -> RuntimeState:
        try:
            return await asyncio.to_thread(self._replace, expected, updated)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    async def begin_idle_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState:
        try:
            return await asyncio.to_thread(self._begin_idle_stop, expected, at)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    async def begin_unneeded_start_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState:
        try:
            return await asyncio.to_thread(self._begin_unneeded_start_stop, expected, at)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("runtime state record is invalid") from None

    def _get(self) -> RuntimeState:
        item = self._get_item(_runtime_key())
        if item is None:
            raise RepositoryConflict("runtime state record is missing")
        return deserialize_runtime_state(item)

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
        pointer = self._load_active_pointer(request)

        for _attempt in range(RUNTIME_CAS_ATTEMPTS):
            previous = self._get()
            effective_at = max(at, previous.updated_at)
            updated = previous.request_wake(at=effective_at)
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
                    pointer=pointer,
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
                pointer = self._load_active_pointer(request)
        raise RepositoryConflict("runtime wake lost repeated conditional-write races")

    def _ensure_wake(self, interaction_id: str, at: datetime) -> RuntimeState:
        if not interaction_id.strip():
            raise ValueError("interaction ID must not be empty")
        _require_utc(at)

        for _attempt in range(RUNTIME_CAS_ATTEMPTS):
            operation = self._load_ingress_operation(interaction_id)
            _require_active_operation(operation)
            request = self._load_ingress_request(operation)
            pointer = self._load_active_pointer(request)
            result = self._get_wake_result(interaction_id)
            if result is None:
                return self._request_wake(interaction_id, at)

            current = self._get()
            if current is None:
                raise RepositoryConflict("runtime wake result points to a missing runtime state")
            _require_marker_not_ahead(result, current)
            effective_at = max(at, current.updated_at)
            _require_wakeable_request(operation, request, effective_at)

            if current.status in {
                RuntimeStatus.STARTING,
                RuntimeStatus.READY,
                RuntimeStatus.BUSY,
            }:
                if current.desired_count != 1:
                    raise RepositoryConflict("active runtime state has an invalid desired count")
                return current
            if current.status not in {
                RuntimeStatus.STOPPED,
                RuntimeStatus.STOPPING,
                RuntimeStatus.IDLE,
                RuntimeStatus.DEGRADED,
            }:
                raise RepositoryConflict("runtime state cannot be recovered by a wake request")

            updated = current.request_wake(at=effective_at)
            try:
                self._transact_rewake(
                    operation=operation,
                    request=request,
                    pointer=pointer,
                    result=result,
                    previous=current,
                    updated=updated,
                    at=effective_at,
                )
                return updated
            except RepositoryConflict:
                continue
        raise RepositoryConflict("runtime re-wake lost repeated conditional-write races")

    def _replace(self, expected: RuntimeState, updated: RuntimeState) -> RuntimeState:
        expected.validate_replacement(updated)
        if (
            expected.status is not RuntimeStatus.STOPPING
            and updated.status is RuntimeStatus.STOPPING
        ):
            raise ValueError("STOPPING requires the complete activity transaction fence")
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
        except self._client.exceptions.ConditionalCheckFailedException:
            current = self._get()
            if current == updated:
                return updated
            raise RepositoryConflict("runtime state changed before replacement") from None
        return updated

    def _begin_idle_stop(self, expected: RuntimeState, at: datetime) -> RuntimeState:
        updated = expected.begin_idle_stop(at=at)
        return self._transact_stop(
            expected=expected,
            updated=updated,
            transaction_kind="idle-stop",
        )

    def _begin_unneeded_start_stop(
        self,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState:
        updated = expected.begin_unneeded_start_stop(at=at)
        return self._transact_stop(
            expected=expected,
            updated=updated,
            transaction_kind="unneeded-start-stop",
        )

    def _transact_stop(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
        transaction_kind: str,
    ) -> RuntimeState:
        expected.validate_replacement(updated)
        condition, names, values = _runtime_cas(expected)
        actions: list[TransactWriteItemTypeDef] = [
            cast(
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
            ),
            _activity_schema_check(table_name=self._table_name),
            _zero_counter_check(
                table_name=self._table_name,
                key={"PK": "CONTROL#INGRESS", "SK": "COUNTER"},
                record_type="ingress_queue_counter",
                record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
                fields=("count",),
            ),
            _zero_counter_check(
                table_name=self._table_name,
                key={"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"},
                record_type="ingress_status_pending_counter",
                record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
                fields=("count",),
            ),
            _zero_counter_check(
                table_name=self._table_name,
                key={"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"},
                record_type="panel_refresh_pending_counter",
                record_schema_version=None,
                fields=("count",),
            ),
            _zero_counter_check(
                table_name=self._table_name,
                key={"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"},
                record_type="outbox_activity_counter",
                record_schema_version=OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
                fields=("pending_count", "claimed_count"),
            ),
            _zero_counter_check(
                table_name=self._table_name,
                key={"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"},
                record_type="active_attempt_counter",
                record_schema_version=ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
                fields=("count",),
            ),
            *(
                _free_slot_check(table_name=self._table_name, slot=slot)
                for slot in range(GLOBAL_LEASE_SLOTS)
            ),
        ]
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=_transaction_token(transaction_kind, actions),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            current = self._get()
            if current == updated:
                return updated
            if is_condition_only_cancellation(error):
                raise RepositoryConflict("runtime stop activity fence rejected") from None
            raise RepositoryUnavailable from None
        except self._client.exceptions.IdempotentParameterMismatchException:
            raise RepositoryConflict("runtime stop transaction token input changed") from None
        return updated

    def _transact_wake(
        self,
        *,
        operation: IngressOperationResult,
        request: IngressRequest,
        pointer: IngressActivePointer,
        previous: RuntimeState,
        updated: RuntimeState,
        result: RuntimeWakeResult,
    ) -> None:
        operation_check = self._active_operation_check(operation)
        request_check = self._active_request_check(request, at=result.recorded_at)
        pointer_check = self._active_pointer_check(pointer)
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
        actions = [
            operation_check,
            request_check,
            pointer_check,
            wake_put,
            runtime_put,
        ]
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=_transaction_token("wake", actions),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            if is_condition_only_cancellation(error):
                raise RepositoryConflict("runtime wake transaction condition failed") from None
            raise RepositoryUnavailable from None
        except self._client.exceptions.IdempotentParameterMismatchException:
            raise RepositoryConflict("runtime wake transaction token input changed") from None

    def _transact_rewake(
        self,
        *,
        operation: IngressOperationResult,
        request: IngressRequest,
        pointer: IngressActivePointer,
        result: RuntimeWakeResult,
        previous: RuntimeState,
        updated: RuntimeState,
        at: datetime,
    ) -> None:
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
        actions = [
            self._active_operation_check(operation),
            self._active_request_check(request, at=at),
            self._active_pointer_check(pointer),
            self._wake_result_check(result),
            runtime_put,
        ]
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=_transaction_token("rewake", actions),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            if is_condition_only_cancellation(error):
                raise RepositoryConflict("runtime re-wake transaction condition failed") from None
            raise RepositoryUnavailable from None
        except self._client.exceptions.IdempotentParameterMismatchException:
            raise RepositoryConflict("runtime re-wake transaction token input changed") from None

    def _active_operation_check(
        self,
        operation: IngressOperationResult,
    ) -> TransactWriteItemTypeDef:
        return cast(
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
                        "AND updated_at=:updated_at AND #operation_status=:operation_status"
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
                            ":updated_at": _timestamp(operation.updated_at),
                            ":operation_status": operation.status.value,
                        }
                    ),
                }
            },
        )

    def _active_request_check(
        self,
        request: IngressRequest,
        *,
        at: datetime,
    ) -> TransactWriteItemTypeDef:
        condition = (
            "record_type=:request_type AND schema_version=:schema "
            "AND record_schema_version=:record_schema "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND created_at=:created_at AND updated_at=:updated_at "
            "AND startup_deadline_at=:startup_deadline "
            "AND terminal_deadline_at=:terminal_deadline "
            "AND #request_status=:request_status"
        )
        values: DynamoItem = {
            ":request_type": "ingress_request",
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": request.schema_version,
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":created_at": _timestamp(request.created_at),
            ":updated_at": _timestamp(request.updated_at),
            ":startup_deadline": _timestamp(request.startup_deadline_at),
            ":terminal_deadline": _timestamp(request.terminal_deadline_at),
            ":request_status": request.status.value,
        }
        if request.processing_started_at is None:
            condition += (
                " AND attribute_not_exists(processing_started_at) AND terminal_deadline_at > :at"
            )
            values[":at"] = _timestamp(at)
        else:
            condition += (
                " AND processing_started_at=:processing_started "
                "AND processing_started_at < terminal_deadline_at"
            )
            values[":processing_started"] = _timestamp(request.processing_started_at)
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_request_key(ingress_request_sort_key(request))),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#request_status": "status"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _active_pointer_check(
        self,
        pointer: IngressActivePointer,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_active_pointer_key(pointer.request_sort_key)),
                    "ConditionExpression": (
                        "record_type=:pointer_type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND interaction_id=:interaction_id "
                        "AND request_sort_key=:request_sort_key AND created_at=:created_at"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":pointer_type": "ingress_active_pointer",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": pointer.schema_version,
                            ":interaction_id": pointer.interaction_id,
                            ":request_sort_key": pointer.request_sort_key,
                            ":created_at": _timestamp(pointer.created_at),
                        }
                    ),
                }
            },
        )

    def _wake_result_check(
        self,
        result: RuntimeWakeResult,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_wake_key(result.interaction_id)),
                    "ConditionExpression": (
                        "record_type=:wake_type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND interaction_id=:interaction_id AND generation=:generation "
                        "AND runtime_version=:runtime_version AND recorded_at=:recorded_at"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":wake_type": "runtime_wake_result",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": result.schema_version,
                            ":interaction_id": result.interaction_id,
                            ":generation": result.generation,
                            ":runtime_version": result.runtime_version,
                            ":recorded_at": _timestamp(result.recorded_at),
                        }
                    ),
                }
            },
        )

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

    def _load_active_pointer(self, request: IngressRequest) -> IngressActivePointer:
        request_sort_key = ingress_request_sort_key(request)
        item = self._get_item(_active_pointer_key(request_sort_key))
        if item is None:
            raise RepositoryConflict("runtime wake requires an active ingress pointer")
        pointer = deserialize_ingress_active_pointer(item)
        if (
            pointer.interaction_id != request.interaction_id
            or pointer.request_sort_key != request_sort_key
            or pointer.created_at != request.created_at
        ):
            raise RepositoryConflict("ingress active pointer targets another request")
        return pointer

    def _replay_wake(self, result: RuntimeWakeResult) -> RuntimeState:
        current = self._get()
        if current is None:
            raise RepositoryConflict("runtime wake result points to a missing runtime state")
        _require_marker_not_ahead(result, current)
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


def _active_pointer_key(request_sort_key: str) -> DynamoItem:
    return {"PK": "CONTROL#INGRESS#ACTIVE", "SK": request_sort_key}


def _activity_schema_key() -> DynamoItem:
    return {"PK": "CONTROL#RUNTIME", "SK": "ACTIVITY_SCHEMA"}


def _activity_schema_check(*, table_name: str) -> TransactWriteItemTypeDef:
    return cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": marshal_item(_activity_schema_key()),
                "ConditionExpression": (
                    "record_type=:type AND schema_version=:schema "
                    "AND record_schema_version=:record_schema"
                ),
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":type": RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
                        ":schema": CURRENT_SCHEMA_VERSION,
                        ":record_schema": RUNTIME_ACTIVITY_SCHEMA_VERSION,
                    }
                ),
            }
        },
    )


def _zero_counter_check(
    *,
    table_name: str,
    key: DynamoItem,
    record_type: str,
    record_schema_version: int | None,
    fields: tuple[str, ...],
) -> TransactWriteItemTypeDef:
    names = {f"#count{index}": field for index, field in enumerate(fields)}
    values: DynamoItem = {
        ":zero": 0,
        ":type": record_type,
        ":schema": CURRENT_SCHEMA_VERSION,
    }
    valid = [
        "record_type=:type",
        "schema_version=:schema",
        *(f"{name}=:zero" for name in names),
    ]
    if record_schema_version is not None:
        values[":record_schema"] = record_schema_version
        valid.append("record_schema_version=:record_schema")
    else:
        valid.append("attribute_not_exists(record_schema_version)")
    return cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": marshal_item(key),
                "ConditionExpression": " AND ".join(valid),
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": marshal_item(values),
            }
        },
    )


def _free_slot_check(*, table_name: str, slot: int) -> TransactWriteItemTypeDef:
    return cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": table_name,
                "Key": marshal_item({"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{slot}"}),
                "ConditionExpression": (
                    "record_type=:type AND schema_version=:schema AND slot=:slot "
                    "AND fencing_token >= :zero "
                    "AND attribute_not_exists(lease_owner) "
                    "AND attribute_not_exists(lease_expiry)"
                ),
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":type": "lease_slot",
                        ":schema": CURRENT_SCHEMA_VERSION,
                        ":slot": slot,
                        ":zero": 0,
                    }
                ),
            }
        },
    )


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
    if request.status is not operation.status:
        raise RepositoryConflict("ingress request and operation status do not match")
    if request.terminal_deadline_at <= at and request.processing_started_at is None:
        raise RepositoryConflict("ingress request terminal deadline prevents runtime wake")


def _require_marker_not_ahead(result: RuntimeWakeResult, current: RuntimeState) -> None:
    if current.generation < result.generation or current.version < result.runtime_version:
        raise RepositoryConflict("runtime wake result is ahead of runtime state")


def _client_token(value: str) -> str:
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}"


def _transaction_token(label: str, actions: list[TransactWriteItemTypeDef]) -> str:
    """Bind DynamoDB idempotency to the complete transaction request body."""

    canonical = json.dumps(actions, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return _client_token(f"{label}:{canonical}")


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
