"""Bounded, idempotent DynamoDB ingress queue implementation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        QueryInputTypeDef,
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )
else:
    TransactGetItemTypeDef = object
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    PersistenceFormatError,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    deserialize_ingress_semantic_binding,
    deserialize_ingress_status_publication,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_ingress_semantic_binding,
    serialize_ingress_status_publication,
)
from shittim_chest.adapters.dynamodb.transaction_errors import (
    is_condition_only_cancellation,
)
from shittim_chest.application.ports import (
    RepositoryConflict,
    RepositoryIdentityConflict,
    RepositoryQueueFull,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_CLAIM_SECONDS,
    INGRESS_QUEUE_LIMIT,
    EnqueuedIngress,
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressSemanticOperationBinding,
    IngressStatus,
    IngressStatusPublication,
    StatusMessageState,
)
from shittim_chest.domain import AttemptId, DebateId

INGRESS_INDEX = "gsi2"
INGRESS_ACTIVE_INDEX_KEY = "INGRESS#ACTIVE"
INGRESS_RECORD_SCHEMA_VERSION = 1

P = ParamSpec("P")
T = TypeVar("T")


class DynamoDbIngressRepository:
    """Store a twenty-entry FIFO with transactional replay and counter records."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        return await self._run(self._enqueue, request)

    async def get_replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        return await self._run(self._replay, request)

    async def get_operation_result(
        self,
        interaction_id: str,
    ) -> IngressOperationResult | None:
        return await self._run(self._get_operation_result, interaction_id)

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_ready, at)

    async def claim(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        return await self._run(self._claim, request, claim_owner, at)

    async def reschedule(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        return await self._run(
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
        return await self._run(
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
        return await self._run(self._mark_startup_timeout, request, at)

    async def mark_terminal(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        return await self._run(
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
        return await self._run(
            self._update_status_message,
            request,
            state,
            message_id,
            at,
        )

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_startup_deadlines, at)

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_terminal_deadlines, at)

    async def active_count(self) -> int:
        return await self._run(self._active_count)

    async def get_status_publication(
        self,
        interaction_id: str,
    ) -> IngressStatusPublication | None:
        return await self._run(self._get_status_publication, interaction_id)

    async def pending_status_count(self) -> int:
        return await self._run(self._pending_status_count)

    async def _run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("ingress repository record is invalid") from None

    def _enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        if request.status is not IngressStatus.PENDING:
            raise ValueError("new ingress request must be pending")
        operation = _operation_for_request(request)
        publication = IngressStatusPublication.prepared(request)
        actions = [
            self._increment_counter_action(request.created_at),
            self._increment_status_counter_action(request.created_at),
            self._put_new(serialize_ingress_request(request)),
            self._put_new(serialize_ingress_operation_result(operation)),
            self._put_new(serialize_ingress_status_publication(publication)),
        ]
        if request.kind is not IngressKind.NEW_DEBATE:
            actions.append(
                self._put_new(serialize_ingress_semantic_binding(_binding_for_request(request)))
            )
        try:
            wrote_items = self._transact(
                actions,
                token=_client_token(
                    f"{self._table_name}:ingress:{request.interaction_id}:{request.operation_id}"
                ),
            )
        except RepositoryConflict:
            replay, active_count = self._resolve_enqueue_conflict(request)
            if replay is not None:
                return replay
            if active_count >= INGRESS_QUEUE_LIMIT:
                raise RepositoryQueueFull(
                    "ingress queue already contains twenty requests"
                ) from None
            raise RepositoryUnavailable from None
        if not wrote_items:
            replay = self._replay(request)
            if replay is None:
                raise RepositoryConflict("idempotent ingress transaction has no replay result")
            return replay
        return EnqueuedIngress(request=request, operation=operation, created=True)

    def _resolve_enqueue_conflict(
        self,
        request: IngressRequest,
    ) -> tuple[EnqueuedIngress | None, int]:
        replay_key = (
            _operation_key(request.interaction_id)
            if request.kind is IngressKind.NEW_DEBATE
            else _semantic_binding_key(request.operation_id)
        )
        replay_item, counter_item = self._transact_get_items((replay_key, _counter_key()))
        active_count = _active_count_from_item(counter_item)
        if replay_item is None:
            return None, active_count
        if request.kind is IngressKind.NEW_DEBATE:
            operation = deserialize_ingress_operation_result(replay_item)
            persisted = self._load_replay_request(operation)
            _assert_exact_identity(request, persisted)
        else:
            binding = deserialize_ingress_semantic_binding(replay_item)
            operation, persisted = self._load_bound_replay(binding)
            _validate_binding(binding, operation)
            _assert_semantic_identity(request, persisted)
        return (
            EnqueuedIngress(request=persisted, operation=operation, created=False),
            active_count,
        )

    def _replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        if request.kind is IngressKind.NEW_DEBATE:
            operation = self._get_operation_result(request.interaction_id)
            if operation is None:
                return None
            persisted = self._load_replay_request(operation)
            _assert_exact_identity(request, persisted)
            return EnqueuedIngress(request=persisted, operation=operation, created=False)
        binding = self._get_semantic_binding(request.operation_id)
        if binding is None:
            return None
        operation, persisted = self._load_bound_replay(binding)
        _validate_binding(binding, operation)
        _assert_semantic_identity(request, persisted)
        return EnqueuedIngress(request=persisted, operation=operation, created=False)

    def _load_replay_request(self, operation: IngressOperationResult) -> IngressRequest:
        request_item, publication_item = self._transact_get_items(
            (
                _request_key(operation.request_sort_key),
                _status_publication_key(operation.interaction_id),
            )
        )
        if request_item is None:
            raise RepositoryConflict("ingress operation result points to a missing request")
        if publication_item is None:
            raise RepositoryConflict("ingress operation result has no status publication")
        persisted = deserialize_ingress_request(request_item)
        publication = deserialize_ingress_status_publication(publication_item)
        self._validate_replay_bundle(operation, persisted, publication)
        return persisted

    def _load_bound_replay(
        self,
        binding: IngressSemanticOperationBinding,
    ) -> tuple[IngressOperationResult, IngressRequest]:
        operation_item, request_item, publication_item = self._transact_get_items(
            (
                _operation_key(binding.canonical_interaction_id),
                _request_key(binding.request_sort_key),
                _status_publication_key(binding.canonical_interaction_id),
            )
        )
        if operation_item is None:
            raise RepositoryConflict("semantic operation binding points to a missing result")
        if request_item is None:
            raise RepositoryConflict("ingress operation result points to a missing request")
        if publication_item is None:
            raise RepositoryConflict("ingress operation result has no status publication")
        operation = deserialize_ingress_operation_result(operation_item)
        persisted = deserialize_ingress_request(request_item)
        publication = deserialize_ingress_status_publication(publication_item)
        self._validate_replay_bundle(operation, persisted, publication)
        return operation, persisted

    @staticmethod
    def _validate_replay_bundle(
        operation: IngressOperationResult,
        persisted: IngressRequest,
        publication: IngressStatusPublication,
    ) -> None:
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
        if (
            publication.canonical_interaction_id != operation.interaction_id
            or publication.request_sort_key != operation.request_sort_key
            or publication.status_channel_id != persisted.status_channel_id
        ):
            raise RepositoryConflict("ingress status publication points to another request")

    def _get_operation_result(self, interaction_id: str) -> IngressOperationResult | None:
        if not interaction_id.strip():
            raise ValueError("interaction ID must not be empty")
        item = self._get_item(_operation_key(interaction_id))
        return None if item is None else deserialize_ingress_operation_result(item)

    def _get_semantic_binding(
        self,
        operation_id: str,
    ) -> IngressSemanticOperationBinding | None:
        if not operation_id.strip():
            raise ValueError("operation ID must not be empty")
        item = self._get_item(_semantic_binding_key(operation_id))
        return None if item is None else deserialize_ingress_semantic_binding(item)

    def _get_status_publication(
        self,
        interaction_id: str,
    ) -> IngressStatusPublication | None:
        if not interaction_id.strip():
            raise ValueError("canonical interaction ID must not be empty")
        item = self._get_item(_status_publication_key(interaction_id))
        return None if item is None else deserialize_ingress_status_publication(item)

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
            IngressStatus.FAILED: StatusMessageState.TERMINAL_FAILED,
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
        return _active_count_from_item(item)

    def _pending_status_count(self) -> int:
        item = self._get_item(_status_counter_key())
        if item is None:
            return 0
        if _text(item, "record_type") != "ingress_status_pending_counter":
            raise RepositoryConflict("status publication counter record type is invalid")
        if _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION:
            raise RepositoryConflict("status publication counter shared schema is invalid")
        if _integer(item, "record_schema_version") != INGRESS_RECORD_SCHEMA_VERSION:
            raise RepositoryConflict("status publication counter record schema is invalid")
        count = _integer(item, "count")
        if count < 0:
            raise RepositoryConflict("status publication counter cannot be negative")
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

    def _increment_status_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_status_counter_key()),
                    "UpdateExpression": (
                        "SET #count=if_not_exists(#count,:zero)+:one, "
                        "record_type=:type, schema_version=:schema, "
                        "record_schema_version=:record_schema, "
                        "created_at=if_not_exists(created_at,:at), updated_at=:at"
                    ),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) OR "
                        "(#count >= :zero AND record_type=:type "
                        "AND schema_version=:schema "
                        "AND record_schema_version=:record_schema)"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":type": "ingress_status_pending_counter",
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

    def _transact(self, actions: Iterable[TransactWriteItemTypeDef], *, token: str) -> bool:
        action_list = list(actions)
        if not 1 <= len(action_list) <= 100:
            raise ValueError("DynamoDB transaction must contain between 1 and 100 actions")
        try:
            response = self._client.transact_write_items(
                TransactItems=action_list,
                ClientRequestToken=token[:36],
                ReturnConsumedCapacity="TOTAL",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            if is_condition_only_cancellation(error):
                raise RepositoryConflict("DynamoDB ingress transaction condition failed") from None
            raise RepositoryUnavailable from None
        except self._client.exceptions.IdempotentParameterMismatchException:
            raise RepositoryConflict("ingress transaction token input changed") from None
        capacities = response.get("ConsumedCapacity", [])
        write_units = sum(capacity.get("WriteCapacityUnits", 0) for capacity in capacities)
        read_units = sum(capacity.get("ReadCapacityUnits", 0) for capacity in capacities)
        if write_units > 0:
            return True
        if read_units > 0:
            return False
        # DynamoDB Local reports only aggregate units. These bounded auxiliary
        # items consume at least one unit per write action; an idempotent replay
        # reports the cheaper reads described by the TransactWriteItems API.
        total_units = sum(capacity.get("CapacityUnits", 0) for capacity in capacities)
        return not capacities or total_units >= len(action_list)

    def _load_current(self, request: IngressRequest) -> IngressRequest | None:
        return self._load_request(ingress_request_sort_key(request))

    def _load_request(self, request_sort_key: str) -> IngressRequest | None:
        item = self._get_item(_request_key(request_sort_key))
        return None if item is None else deserialize_ingress_request(item)

    def _get_item(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)

    def _transact_get_items(
        self,
        keys: tuple[DynamoItem, ...],
    ) -> tuple[DynamoItem | None, ...]:
        actions = [
            cast(
                TransactGetItemTypeDef,
                {
                    "Get": {
                        "TableName": self._table_name,
                        "Key": marshal_item(key),
                    }
                },
            )
            for key in keys
        ]
        response = self._client.transact_get_items(
            TransactItems=actions,
            ReturnConsumedCapacity="NONE",
        )
        raw_responses = response.get("Responses")
        if raw_responses is None or len(raw_responses) != len(keys):
            raise RepositoryConflict("DynamoDB replay bundle response is incomplete")
        items: list[DynamoItem | None] = []
        for raw_response in raw_responses:
            raw_item = raw_response.get("Item")
            items.append(None if raw_item is None else unmarshal_item(raw_item))
        return tuple(items)


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


def _binding_for_request(request: IngressRequest) -> IngressSemanticOperationBinding:
    if request.kind is IngressKind.NEW_DEBATE:
        raise ValueError("new debate has no semantic component-operation binding")
    return IngressSemanticOperationBinding(
        operation_id=request.operation_id,
        canonical_interaction_id=request.interaction_id,
        request_sort_key=ingress_request_sort_key(request),
        created_at=request.created_at,
    )


def _validate_binding(
    binding: IngressSemanticOperationBinding,
    operation: IngressOperationResult,
) -> None:
    if operation.interaction_id != binding.canonical_interaction_id:
        raise RepositoryConflict("semantic operation binding points to another interaction")
    if operation.operation_id != binding.operation_id:
        raise RepositoryConflict("semantic operation binding points to another operation")
    if operation.request_sort_key != binding.request_sort_key:
        raise RepositoryConflict("semantic operation binding points to another request")
    if operation.created_at != binding.created_at:
        raise RepositoryConflict("semantic operation binding has another creation timestamp")


def _operation_key(interaction_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RESULT"}


def _semantic_binding_key(operation_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_SEMANTIC_OPERATION#{operation_id}", "SK": "BINDING"}


def _status_publication_key(canonical_interaction_id: str) -> DynamoItem:
    return {
        "PK": f"INGRESS_OPERATION#{canonical_interaction_id}",
        "SK": "STATUS_PUBLICATION",
    }


def _request_key(request_sort_key: str) -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": request_sort_key}


def _counter_key() -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": "COUNTER"}


def _status_counter_key() -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"}


def _active_count_from_item(item: DynamoItem | None) -> int:
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


def _assert_exact_identity(incoming: IngressRequest, persisted: IngressRequest) -> None:
    if incoming.interaction_id != persisted.interaction_id:
        raise RepositoryIdentityConflict("ingress replay belongs to another interaction")
    _assert_common_identity(incoming, persisted, include_display_names=True)


def _assert_semantic_identity(incoming: IngressRequest, persisted: IngressRequest) -> None:
    if incoming.kind is IngressKind.NEW_DEBATE or persisted.kind is IngressKind.NEW_DEBATE:
        raise RepositoryIdentityConflict("semantic replay is only valid for component operations")
    _assert_common_identity(incoming, persisted, include_display_names=False)


def _assert_common_identity(
    incoming: IngressRequest,
    persisted: IngressRequest,
    *,
    include_display_names: bool,
) -> None:
    incoming_identity = (
        incoming.operation_id,
        incoming.kind,
        incoming.application_id,
        incoming.requester_id,
        incoming.requester_can_manage_messages,
        incoming.guild_id,
        incoming.channel_id,
        incoming.parent_channel_id,
        incoming.status_channel_id,
        incoming.command_name,
        incoming.custom_id,
        incoming.question,
        incoming.source_message_id,
        incoming.source_thread_id,
        incoming.target_debate_id,
        incoming.expected_attempt_id,
    )
    persisted_identity = (
        persisted.operation_id,
        persisted.kind,
        persisted.application_id,
        persisted.requester_id,
        persisted.requester_can_manage_messages,
        persisted.guild_id,
        persisted.channel_id,
        persisted.parent_channel_id,
        persisted.status_channel_id,
        persisted.command_name,
        persisted.custom_id,
        persisted.question,
        persisted.source_message_id,
        persisted.source_thread_id,
        persisted.target_debate_id,
        persisted.expected_attempt_id,
    )
    if incoming_identity != persisted_identity:
        raise RepositoryIdentityConflict("ingress replay immutable identity changed")
    if include_display_names and (
        incoming.requester_username != persisted.requester_username
        or incoming.requester_display_name != persisted.requester_display_name
    ):
        raise RepositoryIdentityConflict("ingress replay requester name snapshot changed")


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
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


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
