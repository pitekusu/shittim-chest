"""Transactional DynamoDB outbox with fenced claim and completion updates."""

from __future__ import annotations

import asyncio
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
    DynamoItem,
    deserialize_outbox,
    serialize_outbox,
)
from shittim_chest.application.discord import (
    OUTBOX_CLAIM_SECONDS,
    OutboxOperation,
    OutboxStatus,
)
from shittim_chest.application.models import DebateSnapshot
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import AttemptId, DebateId


class DynamoDbOutboxRepository:
    """Persist Discord messages before delivery and fence each publisher claim."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def prepare(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> OutboxOperation:
        return await asyncio.to_thread(self._prepare, expected, operation)

    async def get(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        operation_id: str,
    ) -> OutboxOperation | None:
        return await asyncio.to_thread(self._get, debate_id, attempt_id, operation_id)

    async def claim(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
    ) -> OutboxOperation | None:
        return await asyncio.to_thread(
            self._claim,
            expected,
            operation_id,
            claim_owner,
            at,
        )

    async def mark_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        return await asyncio.to_thread(
            self._mark_sent,
            expected,
            operation_id,
            claim_owner,
            message_id,
            at,
        )

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        return await asyncio.to_thread(
            self._reschedule,
            expected,
            operation_id,
            claim_owner,
            at,
            next_retry_at,
        )

    async def list_pending(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        return await asyncio.to_thread(self._list_pending, debate_id, attempt_id)

    def _prepare(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> OutboxOperation:
        _require_same_attempt(expected, operation)
        existing = self._get(operation.debate_id, operation.attempt_id, operation.operation_id)
        if existing is not None:
            if existing != operation:
                raise RepositoryConflict("outbox operation ID is bound to different content")
            return existing
        actions = [
            self._current_attempt_check(expected),
            self._lease_check(expected, operation.created_at),
            cast(
                TransactWriteItemTypeDef,
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(serialize_outbox(operation)),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
                    }
                },
            ),
        ]
        try:
            self._transact(actions, operation.operation_id)
        except RepositoryConflict:
            existing = self._get(operation.debate_id, operation.attempt_id, operation.operation_id)
            if existing is None or existing != operation:
                raise
            return existing
        return operation

    def _claim(
        self,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
    ) -> OutboxOperation | None:
        _require_utc(at)
        if not claim_owner.strip():
            raise ValueError("claim owner must not be empty")
        operation = self._get(
            expected.state.debate_id,
            expected.state.attempt_id,
            operation_id,
        )
        if operation is None:
            raise RepositoryConflict("outbox operation does not exist")
        if operation.status is OutboxStatus.SENT:
            return None
        if operation.next_retry_at is not None and operation.next_retry_at > at:
            return None
        if (
            operation.status is OutboxStatus.CLAIMED
            and operation.claim_expires_at is not None
            and operation.claim_expires_at >= at
        ):
            return None
        if not self._prior_chunks_sent(operation):
            return None

        expiry = at + timedelta(seconds=OUTBOX_CLAIM_SECONDS)
        values = marshal_item(
            {
                ":prepared": OutboxStatus.PREPARED.value,
                ":claimed": OutboxStatus.CLAIMED.value,
                ":owner": claim_owner,
                ":expiry": _timestamp(expiry),
                ":at": _timestamp(at),
                ":zero": 0,
                ":one": 1,
            }
        )
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_outbox_key(operation)),
                    "UpdateExpression": (
                        "SET #status=:claimed, claim_owner=:owner, claim_expiry=:expiry, "
                        "updated_at=:at, "
                        "delivery_attempt=if_not_exists(delivery_attempt,:zero)+:one "
                        "REMOVE next_retry_at"
                    ),
                    "ConditionExpression": (
                        "(#status=:prepared OR (#status=:claimed AND claim_expiry < :at)) "
                        "AND (attribute_not_exists(next_retry_at) OR next_retry_at <= :at)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
        )
        self._transact(
            [self._current_attempt_check(expected), self._lease_check(expected, at), update],
            f"claim-{operation_id}-{operation.delivery_attempt + 1}",
        )
        claimed = self._get(operation.debate_id, operation.attempt_id, operation_id)
        if claimed is None:
            raise RepositoryConflict("claimed outbox operation disappeared")
        return claimed

    def _mark_sent(
        self,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        _require_utc(at)
        if not message_id.strip():
            raise ValueError("message ID must not be empty")
        operation = self._require_operation(expected, operation_id)
        if operation.status is OutboxStatus.SENT:
            if operation.message_id != message_id:
                raise RepositoryConflict("outbox operation is bound to another message")
            return operation
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_outbox_key(operation)),
                    "UpdateExpression": (
                        "SET #status=:sent, message_id=:message, sent_at=:at, updated_at=:at "
                        "REMOVE claim_owner, claim_expiry, next_retry_at"
                    ),
                    "ConditionExpression": (
                        "#status=:claimed AND claim_owner=:owner AND claim_expiry >= :at"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":sent": OutboxStatus.SENT.value,
                            ":claimed": OutboxStatus.CLAIMED.value,
                            ":owner": claim_owner,
                            ":message": message_id,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )
        self._transact(
            [self._current_attempt_check(expected), self._lease_check(expected, at), update],
            f"sent-{operation_id}-{operation.delivery_attempt}",
        )
        sent = self._require_operation(expected, operation_id)
        if sent.status is not OutboxStatus.SENT:
            raise RepositoryConflict("outbox completion was not persisted")
        return sent

    def _reschedule(
        self,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        _require_utc(at)
        _require_utc(next_retry_at)
        if next_retry_at <= at:
            raise ValueError("next retry timestamp must be in the future")
        operation = self._require_operation(expected, operation_id)
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_outbox_key(operation)),
                    "UpdateExpression": (
                        "SET #status=:prepared, next_retry_at=:retry, updated_at=:at "
                        "REMOVE claim_owner, claim_expiry"
                    ),
                    "ConditionExpression": (
                        "#status=:claimed AND claim_owner=:owner AND claim_expiry >= :at"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":prepared": OutboxStatus.PREPARED.value,
                            ":claimed": OutboxStatus.CLAIMED.value,
                            ":owner": claim_owner,
                            ":retry": _timestamp(next_retry_at),
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )
        self._transact(
            [self._current_attempt_check(expected), self._lease_check(expected, at), update],
            f"retry-{operation_id}-{operation.delivery_attempt}",
        )
        return self._require_operation(expected, operation_id)

    def _list_pending(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        operations = self._query_attempt_outbox(debate_id, attempt_id)
        return tuple(
            operation for operation in operations if operation.status is not OutboxStatus.SENT
        )

    def _prior_chunks_sent(self, operation: OutboxOperation) -> bool:
        operations = self._query_attempt_outbox(operation.debate_id, operation.attempt_id)
        return all(
            candidate.status is OutboxStatus.SENT
            for candidate in operations
            if candidate.chunk_sequence < operation.chunk_sequence
        )

    def _query_attempt_outbox(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        prefix = f"ATTEMPT#{attempt_id}#OUTBOX#"
        items: list[DynamoItem] = []
        start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK=:pk AND begins_with(SK,:prefix)",
                "ExpressionAttributeValues": marshal_item(
                    {":pk": f"DEBATE#{debate_id}", ":prefix": prefix}
                ),
                "ConsistentRead": True,
            }
            if start_key is not None:
                parameters["ExclusiveStartKey"] = start_key
            response = self._client.query(**parameters)
            items.extend(unmarshal_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        operations = [deserialize_outbox(item) for item in items]
        return tuple(sorted(operations, key=lambda item: (item.chunk_sequence, item.operation_id)))

    def _get(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
        operation_id: str,
    ) -> OutboxOperation | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(
                {
                    "PK": f"DEBATE#{debate_id}",
                    "SK": f"ATTEMPT#{attempt_id}#OUTBOX#{operation_id}",
                }
            ),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else deserialize_outbox(unmarshal_item(raw))

    def _require_operation(
        self,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation:
        operation = self._get(
            expected.state.debate_id,
            expected.state.attempt_id,
            operation_id,
        )
        if operation is None:
            raise RepositoryConflict("outbox operation does not exist")
        return operation

    def _current_attempt_check(self, expected: DebateSnapshot) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": f"DEBATE#{expected.state.debate_id}", "SK": "META"}),
                    "ConditionExpression": "current_attempt_id=:attempt",
                    "ExpressionAttributeValues": marshal_item(
                        {":attempt": str(expected.state.attempt_id)}
                    ),
                }
            },
        )

    def _lease_check(self, expected: DebateSnapshot, at: datetime) -> TransactWriteItemTypeDef:
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("outbox write requires a fenced lease")
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        {
                            "PK": f"DEBATE#{expected.state.debate_id}",
                            "SK": f"ATTEMPT#{expected.state.attempt_id}#META",
                        }
                    ),
                    "ConditionExpression": (
                        "lease_owner=:owner AND fencing_token=:token AND lease_expiry >= :at"
                    ),
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

    def _transact(self, actions: list[TransactWriteItemTypeDef], token: str) -> None:
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=_client_token(f"{self._table_name}:{token}"),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            raise RepositoryConflict("outbox transaction condition failed") from error
        except self._client.exceptions.IdempotentParameterMismatchException as error:
            raise RepositoryConflict("outbox transaction token mismatch") from error


def _require_same_attempt(expected: DebateSnapshot, operation: OutboxOperation) -> None:
    if (
        expected.state.debate_id != operation.debate_id
        or expected.state.attempt_id != operation.attempt_id
    ):
        raise RepositoryConflict("outbox operation is bound to another attempt")
    if operation.status is not OutboxStatus.PREPARED or operation.delivery_attempt != 0:
        raise RepositoryConflict("new outbox operation must be prepared and unattempted")


def _outbox_key(operation: OutboxOperation) -> DynamoItem:
    return {
        "PK": f"DEBATE#{operation.debate_id}",
        "SK": f"ATTEMPT#{operation.attempt_id}#OUTBOX#{operation.operation_id}",
    }


def _client_token(value: str) -> str:
    import hashlib

    return f"ob-{hashlib.sha256(value.encode()).hexdigest()[:33]}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
