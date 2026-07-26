"""Bounded, idempotent DynamoDB ingress queue implementation."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

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
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
)
from shittim_chest.application.ports import (
    RepositoryConflict,
    RepositoryQueueFull,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_CLAIM_SECONDS,
    INGRESS_QUEUE_LIMIT,
    EnqueuedIngress,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    StatusMessageState,
)
from shittim_chest.domain import AttemptId, DebateId

INGRESS_INDEX = "gsi2"
INGRESS_ACTIVE_INDEX_KEY = "INGRESS#ACTIVE"
INGRESS_RECORD_SCHEMA_VERSION = 1


class DynamoDbIngressRepository:
    """Store a twenty-entry FIFO with transactional replay and counter records."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        return await asyncio.to_thread(self._enqueue, request)

    async def get_operation_result(
        self,
        interaction_id: str,
    ) -> IngressOperationResult | None:
        return await asyncio.to_thread(self._get_operation_result, interaction_id)

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await asyncio.to_thread(self._list_ready, at)

    async def claim(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        return await asyncio.to_thread(self._claim, request, claim_owner, at)

    async def reschedule(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        return await asyncio.to_thread(
            self._reschedule,
            request,
            claim_owner,
            at,
            next_attempt_at,
            error_code,
        )

    async def mark_accepted(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest:
        return await asyncio.to_thread(
            self._mark_accepted,
            request,
            claim_owner,
            at,
            debate_id,
            attempt_id,
        )

    async def mark_startup_timeout(
        self,
        *,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest:
        return await asyncio.to_thread(self._mark_startup_timeout, request, at)

    async def mark_terminal(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        return await asyncio.to_thread(
            self._mark_terminal,
            request,
            at,
            status,
            error_code,
        )

    async def update_status_message(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        message_id: str,
        at: datetime,
    ) -> IngressRequest:
        return await asyncio.to_thread(
            self._update_status_message,
            request,
            state,
            message_id,
            at,
        )

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await asyncio.to_thread(self._list_startup_deadlines, at)

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await asyncio.to_thread(self._list_terminal_deadlines, at)

    async def active_count(self) -> int:
        return await asyncio.to_thread(self._active_count)

    def _enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        if request.status is not IngressStatus.PENDING:
            raise ValueError("new ingress request must be pending")
        replay = self._replay(request)
        if replay is not None:
            return replay

        operation = _operation_for_request(request)
        actions = [
            self._increment_counter_action(request.created_at),
            self._put_new(serialize_ingress_request(request)),
            self._put_new(serialize_ingress_operation_result(operation)),
        ]
        try:
            self._transact(
                actions,
                token=_client_token(
                    f"{self._table_name}:ingress:{request.operation_id}:{uuid.uuid7()}"
                ),
            )
        except RepositoryConflict:
            replay = self._replay(request)
            if replay is not None:
                return replay
            if self._active_count() >= INGRESS_QUEUE_LIMIT:
                raise RepositoryQueueFull(
                    "ingress queue already contains twenty requests"
                ) from None
            raise
        return EnqueuedIngress(request=request, operation=operation, created=True)

    def _replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        operation = self._get_operation_result(request.interaction_id)
        if operation is None:
            return None
        if operation.interaction_id != request.interaction_id:
            raise RepositoryConflict("ingress operation ID belongs to another interaction")
        persisted = self._load_request(operation.request_sort_key)
        if persisted is None:
            raise RepositoryConflict("ingress operation result points to a missing request")
        if persisted.operation_id != operation.operation_id:
            raise RepositoryConflict("ingress operation result points to another operation")
        if persisted.interaction_id != operation.interaction_id:
            raise RepositoryConflict("ingress operation result points to another interaction")
        if (
            persisted.status != operation.status
            or persisted.created_at != operation.created_at
            or persisted.updated_at != operation.updated_at
            or persisted.accepted_debate_id != operation.accepted_debate_id
            or persisted.accepted_attempt_id != operation.accepted_attempt_id
            or persisted.error_code != operation.error_code
        ):
            raise RepositoryConflict("ingress operation result disagrees with its request")
        return EnqueuedIngress(request=persisted, operation=operation, created=False)

    def _get_operation_result(self, interaction_id: str) -> IngressOperationResult | None:
        if not interaction_id.strip():
            raise ValueError("interaction ID must not be empty")
        item = self._get_item(_operation_key(interaction_id))
        return None if item is None else deserialize_ingress_operation_result(item)

    def _list_ready(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        return tuple(request for request in self._query_active() if _is_ready(request, at))

    def _claim(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        _require_utc(at)
        if not claim_owner.strip():
            raise ValueError("claim owner must not be empty")
        if not _is_ready(request, at):
            return None
        claimed = replace(
            request,
            status=IngressStatus.CLAIMED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(seconds=INGRESS_CLAIM_SECONDS),
            delivery_attempt=request.delivery_attempt + 1,
            error_code=None,
            error_detail_code=None,
        )
        condition = self._request_condition(request)
        condition += " AND terminal_deadline_at > :at"
        values = self._expected_values(request)
        values[":at"] = _timestamp(at)
        if request.status is IngressStatus.RETRYING:
            condition += " AND (attribute_not_exists(next_attempt_at) OR next_attempt_at <= :at)"
        elif request.status is IngressStatus.CLAIMED:
            condition += " AND claim_expiry <= :at"
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=claimed,
                condition=condition,
                values=values,
                token_suffix=f"claim:{claim_owner}:{at}",
            )
        except RepositoryConflict:
            return None
        return claimed

    def _reschedule(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        _require_utc(at)
        _require_utc(next_attempt_at)
        if request.status is not IngressStatus.CLAIMED or request.claim_owner != claim_owner:
            raise RepositoryConflict("only the current ingress claimant may reschedule")
        if next_attempt_at <= at:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.RETRYING
                and current.next_attempt_at == next_attempt_at
                and current.error_code == error_code
            ):
                return current
            raise ValueError("next ingress attempt must be in the future")
        if not error_code.strip():
            raise ValueError("reschedule error code must not be empty")
        updated = replace(
            request,
            status=IngressStatus.RETRYING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
        )
        condition = (
            self._request_condition(request) + " AND claim_owner=:owner AND claim_expiry > :at"
        )
        values = self._expected_values(request)
        values[":owner"] = claim_owner
        values[":at"] = _timestamp(at)
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=condition,
                values=values,
                token_suffix=f"retry:{claim_owner}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.RETRYING
                and current.next_attempt_at == next_attempt_at
                and current.error_code == error_code
            ):
                return current
            raise
        return updated

    def _mark_accepted(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest:
        _require_utc(at)
        if request.status is not IngressStatus.CLAIMED or request.claim_owner != claim_owner:
            raise RepositoryConflict("only the current ingress claimant may accept")
        updated = replace(
            request,
            status=IngressStatus.ACCEPTED,
            status_message_state=StatusMessageState.ACCEPTED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=None,
            error_detail_code=None,
            accepted_debate_id=debate_id,
            accepted_attempt_id=attempt_id,
        )
        condition = self._request_condition(request)
        condition += " AND claim_owner=:owner AND claim_expiry > :at AND terminal_deadline_at > :at"
        values = self._expected_values(request)
        values[":owner"] = claim_owner
        values[":at"] = _timestamp(at)
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=condition,
                values=values,
                extra_actions=(self._decrement_counter_action(at),),
                token_suffix=f"accepted:{debate_id}:{attempt_id}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.ACCEPTED
                and current.accepted_debate_id == debate_id
                and current.accepted_attempt_id == attempt_id
            ):
                return current
            raise
        return updated

    def _mark_startup_timeout(
        self,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest:
        _require_utc(at)
        if request.status_message_state is StatusMessageState.STARTUP_TIMEOUT:
            return request
        if not request.status.counts_toward_queue_limit:
            raise RepositoryConflict("startup timeout applies only to queued ingress")
        if at < request.startup_deadline_at or at >= request.terminal_deadline_at:
            raise RepositoryConflict("startup timeout is outside its request deadline window")
        updated = replace(
            request,
            status_message_state=StatusMessageState.STARTUP_TIMEOUT,
            updated_at=at,
        )
        condition = self._request_condition(request)
        condition += " AND startup_deadline_at <= :at AND terminal_deadline_at > :at"
        values = self._expected_values(request)
        values[":at"] = _timestamp(at)
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=condition,
                values=values,
                token_suffix=f"startup-timeout:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status_message_state is StatusMessageState.STARTUP_TIMEOUT
            ):
                return current
            raise
        return updated

    def _mark_terminal(
        self,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        _require_utc(at)
        if status not in {
            IngressStatus.COMPLETED,
            IngressStatus.REJECTED,
            IngressStatus.FAILED,
        }:
            raise ValueError("terminal ingress status must be completed, rejected, or failed")
        if status is IngressStatus.COMPLETED:
            if request.status is not IngressStatus.ACCEPTED:
                raise RepositoryConflict("only an accepted ingress request may complete")
            if error_code is not None:
                raise ValueError("completed ingress request cannot have an error code")
        elif error_code is None or not error_code.strip():
            raise ValueError("rejected and failed ingress requests require an error code")
        if request.status.is_terminal:
            if request.status is status and request.error_code == error_code:
                return request
            raise RepositoryConflict("ingress request already reached another terminal state")
        state_by_status = {
            IngressStatus.COMPLETED: StatusMessageState.COMPLETED,
            IngressStatus.REJECTED: StatusMessageState.REJECTED,
            IngressStatus.FAILED: StatusMessageState.FAILED,
        }
        updated = replace(
            request,
            status=status,
            status_message_state=state_by_status[status],
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
            completed_at=at,
        )
        extra = (
            (self._decrement_counter_action(at),)
            if request.status.counts_toward_queue_limit
            else ()
        )
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=self._request_condition(request),
                values=self._expected_values(request),
                extra_actions=extra,
                token_suffix=f"terminal:{status.value}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is status
                and current.error_code == error_code
            ):
                return current
            raise
        return updated

    def _update_status_message(
        self,
        request: IngressRequest,
        state: StatusMessageState,
        message_id: str,
        at: datetime,
    ) -> IngressRequest:
        _require_utc(at)
        if not message_id.strip():
            raise ValueError("status message ID must not be empty")
        if at < request.updated_at:
            raise ValueError("status message timestamp cannot precede request update")
        if (
            request.status_message_state is state
            and request.status_message_id == message_id
            and request.status_message_updated_at is not None
            and request.status_message_updated_at >= request.updated_at
        ):
            return request
        updated = replace(
            request,
            status_message_state=state,
            status_message_id=message_id,
            status_message_updated_at=at,
            updated_at=at,
        )
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=self._request_condition(request),
                values=self._expected_values(request),
                token_suffix=f"status-message:{state.value}:{message_id}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status_message_state is state
                and current.status_message_id == message_id
                and current.status_message_updated_at is not None
                and current.status_message_updated_at >= request.updated_at
            ):
                return current
            raise
        return updated

    def _list_startup_deadlines(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        return tuple(
            request
            for request in self._query_active()
            if request.startup_deadline_at <= at < request.terminal_deadline_at
            and request.status_message_state is not StatusMessageState.STARTUP_TIMEOUT
        )

    def _list_terminal_deadlines(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        return tuple(
            request for request in self._query_active() if request.terminal_deadline_at <= at
        )

    def _active_count(self) -> int:
        item = self._get_item(_counter_key())
        if item is None:
            return 0
        if _text(item, "record_type") != "ingress_queue_counter":
            raise RepositoryConflict("ingress counter record type is invalid")
        if _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION:
            raise RepositoryConflict("ingress counter shared schema version is invalid")
        if _integer(item, "record_schema_version") != INGRESS_RECORD_SCHEMA_VERSION:
            raise RepositoryConflict("ingress counter record schema version is invalid")
        count = _integer(item, "count")
        if not 0 <= count <= INGRESS_QUEUE_LIMIT:
            raise RepositoryConflict("ingress counter is outside its valid range")
        return count

    def _query_active(self) -> tuple[IngressRequest, ...]:
        requests: list[IngressRequest] = []
        exclusive_start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "IndexName": INGRESS_INDEX,
                "KeyConditionExpression": "gsi2pk=:active",
                "ExpressionAttributeValues": marshal_item({":active": INGRESS_ACTIVE_INDEX_KEY}),
                "ScanIndexForward": True,
            }
            if exclusive_start_key is not None:
                parameters["ExclusiveStartKey"] = exclusive_start_key
            response = self._client.query(**parameters)
            requests.extend(
                deserialize_ingress_request(unmarshal_item(item))
                for item in response.get("Items", [])
            )
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                return tuple(requests)

    def _replace_request_and_operation(
        self,
        *,
        previous: IngressRequest,
        updated: IngressRequest,
        condition: str,
        values: Mapping[str, DynamoValue],
        token_suffix: str,
        extra_actions: Iterable[TransactWriteItemTypeDef] = (),
    ) -> None:
        operation = _operation_for_request(updated)
        request_action = cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_request(updated)),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )
        operation_action = cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_operation_result(operation)),
                    "ConditionExpression": (
                        "#status=:expected_status AND updated_at=:expected_updated "
                        "AND schema_version=:schema AND record_schema_version=:record_schema "
                        "AND record_type=:operation_type "
                        "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                        "AND request_sort_key=:request_sort_key AND created_at=:created_at"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        self._operation_expected_values(previous)
                    ),
                }
            },
        )
        token_source = f"{self._table_name}:{previous.operation_id}:{token_suffix}"
        self._transact(
            (request_action, operation_action, *extra_actions),
            token=_client_token(token_source),
        )

    def _request_condition(self, request: IngressRequest) -> str:
        return (
            "#status=:expected_status AND updated_at=:expected_updated "
            "AND schema_version=:schema AND record_schema_version=:record_schema "
            "AND record_type=:request_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND created_at=:created_at AND startup_deadline_at=:startup_deadline "
            "AND terminal_deadline_at=:terminal_deadline"
        )

    def _expected_values(self, request: IngressRequest) -> DynamoItem:
        return {
            ":expected_status": request.status.value,
            ":expected_updated": _timestamp(request.updated_at),
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
            ":request_type": "ingress_request",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":created_at": _timestamp(request.created_at),
            ":startup_deadline": _timestamp(request.startup_deadline_at),
            ":terminal_deadline": _timestamp(request.terminal_deadline_at),
        }

    def _operation_expected_values(self, request: IngressRequest) -> DynamoItem:
        return {
            ":expected_status": request.status.value,
            ":expected_updated": _timestamp(request.updated_at),
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
            ":operation_type": "ingress_operation_result",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":request_sort_key": ingress_request_sort_key(request),
            ":created_at": _timestamp(request.created_at),
        }

    def _increment_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_counter_key()),
                    "UpdateExpression": (
                        "SET #count=if_not_exists(#count,:zero)+:one, "
                        "record_type=:type, schema_version=:schema, "
                        "record_schema_version=:record_schema, "
                        "created_at=if_not_exists(created_at,:at), updated_at=:at"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR "
                        "(#count >= :zero AND #count < :limit AND record_type=:type "
                        "AND schema_version=:schema "
                        "AND record_schema_version=:record_schema)"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":limit": INGRESS_QUEUE_LIMIT,
                            ":type": "ingress_queue_counter",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _decrement_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_counter_key()),
                    "UpdateExpression": "SET #count=#count-:one, updated_at=:at",
                    "ConditionExpression": (
                        "#count > :zero AND #count <= :limit AND schema_version=:schema "
                        "AND record_schema_version=:record_schema AND record_type=:type"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":limit": INGRESS_QUEUE_LIMIT,
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":type": "ingress_queue_counter",
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
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
            raise RepositoryConflict("DynamoDB ingress transaction condition failed") from error
        except self._client.exceptions.IdempotentParameterMismatchException as error:
            raise RepositoryConflict("ingress transaction token input changed") from error

    def _load_current(self, request: IngressRequest) -> IngressRequest | None:
        return self._load_request(ingress_request_sort_key(request))

    def _load_request(self, request_sort_key: str) -> IngressRequest | None:
        item = self._get_item({"PK": "CONTROL#INGRESS", "SK": request_sort_key})
        return None if item is None else deserialize_ingress_request(item)

    def _get_item(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)


def _operation_for_request(request: IngressRequest) -> IngressOperationResult:
    return IngressOperationResult(
        operation_id=request.operation_id,
        interaction_id=request.interaction_id,
        request_sort_key=ingress_request_sort_key(request),
        status=request.status,
        created_at=request.created_at,
        updated_at=request.updated_at,
        accepted_debate_id=request.accepted_debate_id,
        accepted_attempt_id=request.accepted_attempt_id,
        error_code=request.error_code,
    )


def _operation_key(interaction_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RESULT"}


def _counter_key() -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": "COUNTER"}


def _is_ready(request: IngressRequest, at: datetime) -> bool:
    if at >= request.terminal_deadline_at:
        return False
    if request.status is IngressStatus.PENDING:
        return True
    if request.status is IngressStatus.RETRYING:
        return request.next_attempt_at is None or request.next_attempt_at <= at
    return (
        request.status is IngressStatus.CLAIMED
        and request.claim_expires_at is not None
        and request.claim_expires_at <= at
    )


def _client_token(value: str) -> str:
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")


def _text(item: Mapping[str, DynamoValue], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value


def _integer(item: Mapping[str, DynamoValue], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value
