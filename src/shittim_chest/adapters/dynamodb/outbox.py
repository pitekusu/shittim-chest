"""Transactional DynamoDB outbox with fenced claim and completion updates."""

from __future__ import annotations

import asyncio
import json
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
    deserialize_outbox,
    serialize_outbox,
)
from shittim_chest.adapters.dynamodb.transaction_errors import classified_transaction_conflict
from shittim_chest.application.discord import (
    MAX_OUTBOX_DELIVERY_ATTEMPTS,
    OUTBOX_CLAIM_SECONDS,
    OutboxOperation,
    OutboxStatus,
)
from shittim_chest.application.models import (
    DebateSnapshot,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
)
from shittim_chest.application.ports import (
    RepositoryCancellationCode,
    RepositoryConflict,
    RepositoryTransactionAction,
    RepositoryTransactionConflict,
    RepositoryTransactionStage,
)
from shittim_chest.application.scale_to_zero import OutboxActivity
from shittim_chest.domain import AttemptId, DebateId

OUTBOX_ACTIVITY_LIMIT = 100_000
OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION = 1
MAX_TRANSACTION_BYTES = 4 * 1024 * 1024
_OUTBOX_ACTIVITY_RECORD_TYPE = "outbox_activity_counter"


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
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        return await asyncio.to_thread(
            self._mark_sent,
            expected,
            operation,
            message_id,
            at,
        )

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        return await asyncio.to_thread(
            self._reschedule,
            expected,
            operation,
            at,
            next_retry_at,
        )

    async def mark_reconciled_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        return await asyncio.to_thread(
            self._mark_reconciled_sent,
            expected,
            operation,
            message_id,
            at,
        )

    async def list_pending(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        return await asyncio.to_thread(self._list_pending, debate_id, attempt_id)

    async def activity(self) -> OutboxActivity:
        """Return the strong global PREPARED/CLAIMED activity snapshot."""

        return await asyncio.to_thread(self._activity)

    def _prepare(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> OutboxOperation:
        _require_same_attempt(expected, operation)
        if operation.status is not OutboxStatus.PREPARED or operation.delivery_attempt != 0:
            raise RepositoryConflict("new outbox operation must be prepared and unattempted")
        if operation.record_schema_version in {2, 3}:
            raise RepositoryConflict(
                "versioned outbox must be staged atomically with its phase delivery plan"
            )
        existing = self._get(operation.debate_id, operation.attempt_id, operation.operation_id)
        if existing is not None:
            if existing != operation:
                raise RepositoryConflict("outbox operation ID is bound to different content")
            return existing
        actions = [
            self._current_attempt_check(expected),
            self._lease_check(expected, operation.created_at, operation=operation),
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
            self._outbox_activity_action(
                pending_delta=1,
                claimed_delta=0,
                at=operation.created_at,
            ),
        ]
        try:
            self._transact(
                actions,
                operation.operation_id,
                cancellation_stage=RepositoryTransactionStage.OUTBOX_PREPARE,
                cancellation_action_kinds=(
                    RepositoryTransactionAction.ATTEMPT_CAS,
                    RepositoryTransactionAction.LEASE_FENCE,
                    RepositoryTransactionAction.OUTBOX_OPERATION,
                    RepositoryTransactionAction.OUTBOX_ACTIVITY,
                ),
            )
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
        if operation.status in {OutboxStatus.SENT, OutboxStatus.ABANDONED}:
            return None
        if operation.record_schema_version in {2, 3}:
            if operation.delivery_attempt >= MAX_OUTBOX_DELIVERY_ATTEMPTS:
                return None
            if operation.deadline_at is None or operation.deadline_at <= at:
                return None
        if operation.next_retry_at is not None and operation.next_retry_at > at:
            return None
        if (
            operation.status is OutboxStatus.CLAIMED
            and operation.claim_expires_at is not None
            and operation.claim_expires_at >= at
        ):
            return None
        if not self._prior_operations_settled(expected, operation):
            return None

        expiry = at + timedelta(seconds=OUTBOX_CLAIM_SECONDS)
        raw_values: DynamoItem = {
            ":prepared": OutboxStatus.PREPARED.value,
            ":claimed": OutboxStatus.CLAIMED.value,
            ":owner": claim_owner,
            ":expiry": _timestamp(expiry),
            ":at": _timestamp(at),
            ":zero": 0,
            ":one": 1,
            ":expected_attempt": operation.delivery_attempt,
            ":content_hash": operation.content_hash,
            ":created": _timestamp(operation.created_at),
        }
        reclaim = operation.status is OutboxStatus.CLAIMED
        if reclaim:
            if operation.claim_owner is None or operation.claim_expires_at is None:
                raise RepositoryConflict("claimed outbox identity is incomplete")
            raw_values[":old_owner"] = operation.claim_owner
            raw_values[":old_expiry"] = _timestamp(operation.claim_expires_at)
            status_condition = (
                "#status=:claimed AND claim_owner=:old_owner "
                "AND claim_expiry=:old_expiry AND claim_expiry < :at"
            )
        else:
            status_condition = (
                "#status=:prepared AND attribute_not_exists(claim_owner) "
                "AND attribute_not_exists(claim_expiry)"
            )
        if operation.next_retry_at is None:
            retry_condition = "attribute_not_exists(next_retry_at)"
        else:
            raw_values[":expected_retry"] = _timestamp(operation.next_retry_at)
            retry_condition = "next_retry_at=:expected_retry AND next_retry_at <= :at"
        identity_condition = "content_hash=:content_hash AND created_at=:created"
        if operation.record_schema_version in {2, 3}:
            if (
                operation.plan_id is None
                or operation.phase is None
                or operation.delivery_sequence is None
                or operation.deadline_at is None
            ):
                raise RepositoryConflict("versioned outbox identity is incomplete")
            raw_values.update(
                {
                    ":record_schema": operation.record_schema_version,
                    ":plan": operation.plan_id,
                    ":phase": operation.phase.value,
                    ":delivery_sequence": operation.delivery_sequence,
                    ":deadline": _timestamp(operation.deadline_at),
                }
            )
            identity_condition += (
                " AND record_schema_version=:record_schema AND plan_id=:plan "
                "AND phase=:phase AND delivery_sequence=:delivery_sequence "
                "AND deadline_at=:deadline"
            )
            if operation.record_schema_version == 3:
                if operation.channel_id is None:
                    raise RepositoryConflict("parent-channel outbox identity is incomplete")
                raw_values.update(
                    {
                        ":delivery_target": operation.delivery_target.value,
                        ":channel": operation.channel_id,
                    }
                )
                identity_condition += (
                    " AND delivery_target=:delivery_target AND channel_id=:channel"
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
                        f"({status_condition}) "
                        "AND delivery_attempt=:expected_attempt AND "
                        + identity_condition
                        + " AND "
                        + retry_condition
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(raw_values),
                }
            },
        )
        actions = [
            self._current_attempt_check(expected),
            self._lease_check(expected, at, operation=operation),
        ]
        if operation.record_schema_version in {2, 3}:
            actions.append(
                self._phase_plan_check(
                    expected,
                    operation,
                    at=at,
                    allow_terminating=False,
                )
            )
        actions.append(update)
        if not reclaim:
            actions.append(
                self._outbox_activity_action(
                    pending_delta=-1,
                    claimed_delta=1,
                    at=at,
                )
            )
        action_kinds = [
            RepositoryTransactionAction.ATTEMPT_CAS,
            RepositoryTransactionAction.LEASE_FENCE,
        ]
        if operation.record_schema_version in {2, 3}:
            action_kinds.append(RepositoryTransactionAction.PHASE_DELIVERY_PLAN)
        action_kinds.append(RepositoryTransactionAction.OUTBOX_OPERATION)
        if not reclaim:
            action_kinds.append(RepositoryTransactionAction.OUTBOX_ACTIVITY)
        self._transact(
            actions,
            f"claim-{operation_id}-{operation.delivery_attempt + 1}",
            cancellation_stage=RepositoryTransactionStage.OUTBOX_CLAIM,
            cancellation_action_kinds=tuple(action_kinds),
        )
        claimed = self._get(operation.debate_id, operation.attempt_id, operation_id)
        if claimed is None:
            raise RepositoryConflict("claimed outbox operation disappeared")
        return claimed

    def _mark_sent(
        self,
        expected: DebateSnapshot,
        observed: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        _require_utc(at)
        if not message_id.strip():
            raise ValueError("message ID must not be empty")
        _require_same_attempt(expected, observed)
        current = self._require_operation(expected, observed.operation_id)
        if current.status is OutboxStatus.SENT:
            if current.message_id != message_id or not _same_outbox_identity(current, observed):
                raise RepositoryConflict("outbox operation is bound to another message")
            return current
        if current != observed:
            raise RepositoryConflict("outbox claim changed before completion")
        condition, condition_values = _claimed_operation_condition(observed, at=at)
        condition_values.update(
            {
                ":sent": OutboxStatus.SENT.value,
                ":message": message_id,
            }
        )
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_outbox_key(observed)),
                    "UpdateExpression": (
                        "SET #status=:sent, message_id=:message, sent_at=:at, updated_at=:at "
                        "REMOVE claim_owner, claim_expiry, next_retry_at"
                    ),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(condition_values),
                }
            },
        )
        actions = [
            self._current_attempt_check(expected),
            self._lease_check(
                expected,
                at,
                operation=observed,
                allow_terminating=True,
            ),
        ]
        action_kinds = [
            RepositoryTransactionAction.ATTEMPT_CAS,
            RepositoryTransactionAction.LEASE_FENCE,
        ]
        if observed.record_schema_version in {2, 3}:
            actions.append(
                self._phase_plan_check(
                    expected,
                    observed,
                    at=at,
                    allow_terminating=True,
                )
            )
            action_kinds.append(RepositoryTransactionAction.PHASE_DELIVERY_PLAN)
        actions.extend(
            (
                update,
                self._outbox_activity_action(
                    pending_delta=0,
                    claimed_delta=-1,
                    at=at,
                ),
            )
        )
        action_kinds.extend(
            (
                RepositoryTransactionAction.OUTBOX_OPERATION,
                RepositoryTransactionAction.OUTBOX_ACTIVITY,
            )
        )
        self._transact(
            actions,
            f"sent-{observed.operation_id}-{observed.delivery_attempt}",
            cancellation_stage=RepositoryTransactionStage.OUTBOX_MARK_SENT,
            cancellation_action_kinds=tuple(action_kinds),
        )
        sent = self._require_operation(expected, observed.operation_id)
        if sent.status is not OutboxStatus.SENT:
            raise RepositoryConflict("outbox completion was not persisted")
        return sent

    def _mark_reconciled_sent(
        self,
        expected: DebateSnapshot,
        observed: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        _require_utc(at)
        if not message_id.strip():
            raise ValueError("message ID must not be empty")
        _require_same_attempt(expected, observed)
        current = self._require_operation(expected, observed.operation_id)
        if current.status is OutboxStatus.SENT:
            if current.message_id != message_id or not _same_outbox_identity(current, observed):
                raise RepositoryConflict("outbox operation is bound to another message")
            return current
        if current != observed:
            raise RepositoryConflict("outbox claim changed before reconciliation")
        if observed.record_schema_version not in {2, 3} or observed.delivery_attempt < 1:
            raise RepositoryConflict("outbox operation has no delivery to reconcile")
        if observed.status is OutboxStatus.CLAIMED:
            if observed.claim_expires_at is None or observed.claim_expires_at >= at:
                raise RepositoryConflict("outbox claim has not expired for reconciliation")
            pending_delta, claimed_delta = 0, -1
            condition, expected_values = _claimed_operation_condition(
                observed,
                at=at,
                require_unexpired=False,
            )
        elif observed.status is OutboxStatus.PREPARED:
            pending_delta, claimed_delta = -1, 0
            condition, expected_values = _prepared_operation_condition(observed)
        else:
            raise RepositoryConflict("outbox operation is not reconcilable")
        expected_values.update(
            {
                ":sent": OutboxStatus.SENT.value,
                ":message": message_id,
                ":at": _timestamp(at),
            }
        )
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_outbox_key(observed)),
                    "UpdateExpression": (
                        "SET #status=:sent, message_id=:message, sent_at=:at, updated_at=:at "
                        "REMOVE claim_owner, claim_expiry, next_retry_at"
                    ),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(expected_values),
                }
            },
        )
        self._transact(
            [
                self._current_attempt_check(expected),
                self._lease_check(
                    expected,
                    at,
                    operation=observed,
                    allow_terminating=True,
                    terminating_only=True,
                ),
                self._phase_plan_check(
                    expected,
                    observed,
                    at=at,
                    allow_terminating=True,
                    terminating_only=True,
                ),
                update,
                self._outbox_activity_action(
                    pending_delta=pending_delta,
                    claimed_delta=claimed_delta,
                    at=at,
                ),
            ],
            f"reconciled-{observed.operation_id}-{observed.delivery_attempt}",
            cancellation_stage=RepositoryTransactionStage.OUTBOX_MARK_SENT,
            cancellation_action_kinds=(
                RepositoryTransactionAction.ATTEMPT_CAS,
                RepositoryTransactionAction.LEASE_FENCE,
                RepositoryTransactionAction.PHASE_DELIVERY_PLAN,
                RepositoryTransactionAction.OUTBOX_OPERATION,
                RepositoryTransactionAction.OUTBOX_ACTIVITY,
            ),
        )
        sent = self._require_operation(expected, observed.operation_id)
        if sent.status is not OutboxStatus.SENT:
            raise RepositoryConflict("reconciled outbox completion was not persisted")
        return sent

    def _reschedule(
        self,
        expected: DebateSnapshot,
        observed: OutboxOperation,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        _require_utc(at)
        _require_utc(next_retry_at)
        if next_retry_at <= at:
            raise ValueError("next retry timestamp must be in the future")
        _require_same_attempt(expected, observed)
        operation = self._require_operation(expected, observed.operation_id)
        if operation != observed:
            raise RepositoryConflict("outbox claim changed before rescheduling")
        condition, condition_values = _claimed_operation_condition(observed, at=at)
        condition_values.update(
            {
                ":prepared": OutboxStatus.PREPARED.value,
                ":retry": _timestamp(next_retry_at),
            }
        )
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
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(condition_values),
                }
            },
        )
        actions = [
            self._current_attempt_check(expected),
            self._lease_check(expected, at, operation=operation),
        ]
        action_kinds = [
            RepositoryTransactionAction.ATTEMPT_CAS,
            RepositoryTransactionAction.LEASE_FENCE,
        ]
        if operation.record_schema_version in {2, 3}:
            actions.append(
                self._phase_plan_check(
                    expected,
                    operation,
                    at=at,
                    allow_terminating=False,
                )
            )
            action_kinds.append(RepositoryTransactionAction.PHASE_DELIVERY_PLAN)
        actions.extend(
            (
                update,
                self._outbox_activity_action(
                    pending_delta=1,
                    claimed_delta=-1,
                    at=at,
                ),
            )
        )
        action_kinds.extend(
            (
                RepositoryTransactionAction.OUTBOX_OPERATION,
                RepositoryTransactionAction.OUTBOX_ACTIVITY,
            )
        )
        self._transact(
            actions,
            f"retry-{observed.operation_id}-{observed.delivery_attempt}",
            cancellation_stage=RepositoryTransactionStage.OUTBOX_RESCHEDULE,
            cancellation_action_kinds=tuple(action_kinds),
        )
        return self._require_operation(expected, observed.operation_id)

    def _list_pending(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        operations = self._query_attempt_outbox(debate_id, attempt_id)
        return tuple(
            operation
            for operation in operations
            if operation.status not in {OutboxStatus.SENT, OutboxStatus.ABANDONED}
        )

    def _prior_operations_settled(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> bool:
        operations = self._query_attempt_outbox(operation.debate_id, operation.attempt_id)
        if operation.record_schema_version in {2, 3}:
            plan = self._expected_phase_plan(expected, operation)
            by_operation_id = {candidate.operation_id: candidate for candidate in operations}
            if len(by_operation_id) != len(operations):
                raise RepositoryConflict("outbox operation identity is duplicated")
            if set(plan.operation_ids) - set(by_operation_id):
                raise RepositoryConflict("phase delivery outbox is incomplete")
            for operation_id, content_hash, delivery_sequence in zip(
                plan.operation_ids,
                plan.content_hashes,
                plan.delivery_sequences,
                strict=True,
            ):
                candidate = by_operation_id[operation_id]
                if (
                    candidate.record_schema_version not in {2, 3}
                    or candidate.plan_id != plan.plan_id
                    or candidate.phase is not plan.target_phase
                    or candidate.content_hash != content_hash
                    or candidate.delivery_sequence != delivery_sequence
                    or candidate.deadline_at != plan.deadline_at
                    or candidate.created_at != plan.staged_at
                ):
                    raise RepositoryConflict("phase delivery outbox identity is inconsistent")
            persisted = by_operation_id.get(operation.operation_id)
            if persisted != operation:
                raise RepositoryConflict("outbox operation changed during ordered claim")
        sequence = (
            operation.chunk_sequence
            if operation.delivery_sequence is None
            else operation.delivery_sequence
        )
        for candidate in operations:
            candidate_sequence = (
                candidate.chunk_sequence
                if candidate.delivery_sequence is None
                else candidate.delivery_sequence
            )
            if candidate_sequence >= sequence:
                continue
            if candidate.plan_id == operation.plan_id:
                if candidate.status is not OutboxStatus.SENT:
                    return False
            elif candidate.status not in {OutboxStatus.SENT, OutboxStatus.ABANDONED}:
                return False
        return True

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
        sequences = [
            item.chunk_sequence if item.delivery_sequence is None else item.delivery_sequence
            for item in operations
        ]
        if len(sequences) != len(set(sequences)):
            raise RepositoryConflict("outbox delivery sequence is duplicated")
        return tuple(
            sorted(
                operations,
                key=lambda item: (
                    item.chunk_sequence
                    if item.delivery_sequence is None
                    else item.delivery_sequence,
                    item.operation_id,
                ),
            )
        )

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

    def _activity(self) -> OutboxActivity:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(_outbox_activity_key()),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            raise RepositoryConflict("outbox activity counter is missing")
        item = unmarshal_item(raw)
        if (
            item.get("record_type") != _OUTBOX_ACTIVITY_RECORD_TYPE
            or item.get("schema_version") != CURRENT_SCHEMA_VERSION
            or item.get("record_schema_version") != OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION
        ):
            raise RepositoryConflict("outbox activity counter is invalid")
        pending = item.get("pending_count")
        claimed = item.get("claimed_count")
        if (
            isinstance(pending, bool)
            or not isinstance(pending, int)
            or isinstance(claimed, bool)
            or not isinstance(claimed, int)
            or not 0 <= pending <= OUTBOX_ACTIVITY_LIMIT
            or not 0 <= claimed <= OUTBOX_ACTIVITY_LIMIT
        ):
            raise RepositoryConflict("outbox activity counts are outside their bounds")
        return OutboxActivity(pending=pending, claimed=claimed)

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

    def _lease_check(
        self,
        expected: DebateSnapshot,
        at: datetime,
        *,
        operation: OutboxOperation,
        allow_terminating: bool = False,
        terminating_only: bool = False,
    ) -> TransactWriteItemTypeDef:
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("outbox write requires a fenced lease")
        values: DynamoItem = {
            ":owner": lease.owner_id,
            ":slot": lease.slot,
            ":token": lease.fencing_token,
            ":at": _timestamp(at),
        }
        condition = (
            "lease_owner=:owner AND lease_slot=:slot "
            "AND fencing_token=:token AND lease_expiry >= :at"
        )
        names: dict[str, str] = {}
        if operation.record_schema_version in {2, 3}:
            plan = self._expected_phase_plan(expected, operation)
            values.update(
                {
                    ":plan": plan.plan_id,
                    ":source": plan.source_phase.value,
                    ":target": plan.target_phase.value,
                    ":operation_ids": list(plan.operation_ids),
                    ":content_hashes": list(plan.content_hashes),
                    ":delivery_sequences": list(plan.delivery_sequences),
                    ":staged_at": _timestamp(plan.staged_at),
                    ":deadline": _timestamp(plan.deadline_at),
                    ":staged_status": PhaseDeliveryStatus.STAGED.value,
                }
            )
            names["#plan_status"] = "terminal_delivery_plan_status"
            if terminating_only:
                values[":terminating_status"] = PhaseDeliveryStatus.TERMINATING.value
                status_condition = "#plan_status=:terminating_status"
            elif allow_terminating:
                values[":terminating_status"] = PhaseDeliveryStatus.TERMINATING.value
                status_condition = (
                    "(#plan_status=:staged_status OR #plan_status=:terminating_status)"
                )
            else:
                status_condition = "#plan_status=:staged_status"
            condition += (
                " AND terminal_delivery_plan_id=:plan "
                "AND terminal_delivery_source=:source "
                "AND terminal_delivery_target=:target "
                "AND terminal_delivery_operation_ids=:operation_ids "
                "AND terminal_delivery_content_hashes=:content_hashes "
                "AND terminal_delivery_sequences=:delivery_sequences "
                "AND terminal_delivery_staged_at=:staged_at "
                "AND terminal_delivery_deadline_at=:deadline AND " + status_condition
            )
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
                    "ConditionExpression": condition,
                    **({"ExpressionAttributeNames": names} if names else {}),
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _phase_plan_check(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        *,
        at: datetime,
        allow_terminating: bool,
        terminating_only: bool = False,
    ) -> TransactWriteItemTypeDef:
        plan = self._expected_phase_plan(expected, operation)
        values: DynamoItem = {
            ":type": "phase_delivery_plan",
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": 2,
            ":debate": str(operation.debate_id),
            ":attempt": str(operation.attempt_id),
            ":plan": plan.plan_id,
            ":source": plan.source_phase.value,
            ":target": plan.target_phase.value,
            ":operation_ids": list(plan.operation_ids),
            ":content_hashes": list(plan.content_hashes),
            ":delivery_sequences": list(plan.delivery_sequences),
            ":staged_at": _timestamp(plan.staged_at),
            ":deadline": _timestamp(plan.deadline_at),
            ":updated": _timestamp(expected.state.updated_at),
            ":staged": PhaseDeliveryStatus.STAGED.value,
        }
        if terminating_only:
            values[":terminating"] = PhaseDeliveryStatus.TERMINATING.value
            status_condition = "#status=:terminating"
            deadline_condition = ""
        elif allow_terminating:
            values[":terminating"] = PhaseDeliveryStatus.TERMINATING.value
            status_condition = "(#status=:staged OR #status=:terminating)"
            deadline_condition = ""
        else:
            values[":at"] = _timestamp(at)
            status_condition = "#status=:staged"
            deadline_condition = " AND deadline_at > :at"
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        {
                            "PK": f"DEBATE#{operation.debate_id}",
                            "SK": (f"ATTEMPT#{operation.attempt_id}#DELIVERY#{plan.plan_id}"),
                        }
                    ),
                    "ConditionExpression": (
                        "record_type=:type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND debate_id=:debate AND attempt_id=:attempt AND plan_id=:plan "
                        "AND source_phase=:source AND target_phase=:target "
                        "AND operation_ids=:operation_ids AND content_hashes=:content_hashes "
                        "AND delivery_sequences=:delivery_sequences "
                        "AND staged_at=:staged_at AND deadline_at=:deadline "
                        "AND updated_at=:updated "
                        f"AND {status_condition}" + deadline_condition
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    @staticmethod
    def _expected_phase_plan(
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> PhaseDeliveryPlan:
        plan = expected.terminal_delivery
        if (
            not isinstance(plan, PhaseDeliveryPlan)
            or operation.record_schema_version not in {2, 3}
            or operation.plan_id != plan.plan_id
            or operation.phase is not plan.target_phase
            or operation.operation_id not in plan.operation_ids
        ):
            raise RepositoryConflict("versioned outbox is not bound to the current phase plan")
        index = plan.operation_ids.index(operation.operation_id)
        if (
            operation.content_hash != plan.content_hashes[index]
            or operation.delivery_sequence != plan.delivery_sequences[index]
            or operation.deadline_at != plan.deadline_at
            or operation.created_at != plan.staged_at
        ):
            raise RepositoryConflict("versioned outbox identity conflicts with its phase plan")
        return plan

    def _outbox_activity_action(
        self,
        *,
        pending_delta: int,
        claimed_delta: int,
        at: datetime,
    ) -> TransactWriteItemTypeDef:
        return outbox_activity_action(
            table_name=self._table_name,
            pending_delta=pending_delta,
            claimed_delta=claimed_delta,
            at=at,
        )

    def _transact(
        self,
        actions: list[TransactWriteItemTypeDef],
        token: str,
        *,
        cancellation_stage: RepositoryTransactionStage | None = None,
        cancellation_action_kinds: tuple[RepositoryTransactionAction, ...] = (),
    ) -> None:
        _require_transaction_size(actions)
        if cancellation_stage is None and cancellation_action_kinds:
            raise ValueError("transaction action kinds require a cancellation stage")
        if cancellation_stage is not None and len(cancellation_action_kinds) != len(actions):
            raise ValueError("transaction action kinds must match the transaction size")
        try:
            self._client.transact_write_items(
                TransactItems=actions,
                ClientRequestToken=_transaction_token(
                    f"{self._table_name}:{token}",
                    actions,
                ),
                ReturnConsumedCapacity="NONE",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            if cancellation_stage is not None:
                raise classified_transaction_conflict(
                    error,
                    stage=cancellation_stage,
                    action_kinds=cancellation_action_kinds,
                ) from error
            raise RepositoryConflict("outbox transaction condition failed") from error
        except self._client.exceptions.IdempotentParameterMismatchException as error:
            if cancellation_stage is not None:
                raise RepositoryTransactionConflict(
                    stage=cancellation_stage,
                    failures=(
                        (
                            RepositoryTransactionAction.UNKNOWN,
                            RepositoryCancellationCode.IDEMPOTENT_PARAMETER_MISMATCH,
                        ),
                    ),
                    reasons_complete=True,
                ) from error
            raise RepositoryConflict("outbox transaction token mismatch") from error


def outbox_activity_action(
    *,
    table_name: str,
    pending_delta: int,
    claimed_delta: int,
    at: datetime,
) -> TransactWriteItemTypeDef:
    """Build one bounded counter update shared by Outbox and terminal staging."""

    if not table_name.strip():
        raise ValueError("table name must not be empty")
    _require_utc(at)
    for label, delta in (("pending", pending_delta), ("claimed", claimed_delta)):
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise TypeError(f"{label} outbox activity delta must be an integer")
        if abs(delta) > OUTBOX_ACTIVITY_LIMIT:
            raise ValueError(f"{label} outbox activity delta exceeds its limit")
    if pending_delta == 0 and claimed_delta == 0:
        raise ValueError("outbox activity update must change a counter")
    values: DynamoItem = {
        ":type": _OUTBOX_ACTIVITY_RECORD_TYPE,
        ":schema": CURRENT_SCHEMA_VERSION,
        ":record_schema": OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
        ":at": _timestamp(at),
        ":pending_delta": pending_delta,
        ":claimed_delta": claimed_delta,
        ":pending_minimum": max(0, -pending_delta),
        ":claimed_minimum": max(0, -claimed_delta),
        ":pending_maximum": OUTBOX_ACTIVITY_LIMIT - max(0, pending_delta),
        ":claimed_maximum": OUTBOX_ACTIVITY_LIMIT - max(0, claimed_delta),
    }
    update = (
        "SET pending_count=pending_count+:pending_delta, "
        "claimed_count=claimed_count+:claimed_delta, updated_at=:at"
    )
    condition = (
        "record_type=:type AND schema_version=:schema "
        "AND record_schema_version=:record_schema "
        "AND pending_count >= :pending_minimum "
        "AND claimed_count >= :claimed_minimum "
        "AND pending_count <= :pending_maximum "
        "AND claimed_count <= :claimed_maximum"
    )
    return cast(
        TransactWriteItemTypeDef,
        {
            "Update": {
                "TableName": table_name,
                "Key": marshal_item(_outbox_activity_key()),
                "UpdateExpression": update,
                "ConditionExpression": condition,
                "ExpressionAttributeValues": marshal_item(values),
            }
        },
    )


def _require_same_attempt(expected: DebateSnapshot, operation: OutboxOperation) -> None:
    if (
        expected.state.debate_id != operation.debate_id
        or expected.state.attempt_id != operation.attempt_id
    ):
        raise RepositoryConflict("outbox operation is bound to another attempt")


def _operation_identity_condition(
    operation: OutboxOperation,
) -> tuple[str, DynamoItem]:
    values: DynamoItem = {
        ":type": "outbox",
        ":schema": CURRENT_SCHEMA_VERSION,
        ":record_schema": operation.record_schema_version,
        ":debate": str(operation.debate_id),
        ":attempt": str(operation.attempt_id),
        ":operation": operation.operation_id,
        ":bot": operation.bot_slot.value,
        ":thread": operation.thread_id,
        ":content": operation.content,
        ":content_hash": operation.content_hash,
        ":nonce": operation.nonce,
        ":chunk": operation.chunk_sequence,
        ":created": _timestamp(operation.created_at),
        ":delivery_attempt": operation.delivery_attempt,
    }
    condition = (
        "record_type=:type AND schema_version=:schema "
        "AND debate_id=:debate AND attempt_id=:attempt AND operation_id=:operation "
        "AND bot_slot=:bot AND thread_id=:thread AND content=:content "
        "AND content_hash=:content_hash AND nonce=:nonce AND chunk_sequence=:chunk "
        "AND created_at=:created AND delivery_attempt=:delivery_attempt "
    )
    if operation.record_schema_version == 1:
        condition += (
            "AND (attribute_not_exists(record_schema_version) "
            "OR record_schema_version=:record_schema) "
        )
    else:
        if (
            operation.phase is None
            or operation.plan_id is None
            or operation.delivery_sequence is None
            or operation.deadline_at is None
        ):
            raise RepositoryConflict("versioned outbox identity is incomplete")
        values.update(
            {
                ":phase": operation.phase.value,
                ":plan": operation.plan_id,
                ":delivery_sequence": operation.delivery_sequence,
                ":deadline": _timestamp(operation.deadline_at),
            }
        )
        condition += (
            "AND record_schema_version=:record_schema AND phase=:phase AND plan_id=:plan "
            "AND delivery_sequence=:delivery_sequence AND deadline_at=:deadline "
        )
        if operation.record_schema_version == 3:
            if operation.channel_id is None:
                raise RepositoryConflict("parent-channel outbox identity is incomplete")
            values.update(
                {
                    ":delivery_target": operation.delivery_target.value,
                    ":channel": operation.channel_id,
                }
            )
            condition += "AND delivery_target=:delivery_target AND channel_id=:channel "
    return condition, values


def _claimed_operation_condition(
    operation: OutboxOperation,
    *,
    at: datetime,
    require_unexpired: bool = True,
) -> tuple[str, DynamoItem]:
    if (
        operation.status is not OutboxStatus.CLAIMED
        or operation.claim_owner is None
        or operation.claim_expires_at is None
        or operation.delivery_attempt < 1
    ):
        raise RepositoryConflict("outbox settlement requires one exact claim")
    condition, values = _operation_identity_condition(operation)
    values.update(
        {
            ":claimed": OutboxStatus.CLAIMED.value,
            ":claim_owner": operation.claim_owner,
            ":claim_expiry": _timestamp(operation.claim_expires_at),
            ":at": _timestamp(at),
        }
    )
    expiry_comparison = ">=" if require_unexpired else "<"
    condition += (
        "AND #status=:claimed AND claim_owner=:claim_owner "
        "AND claim_expiry=:claim_expiry "
        f"AND claim_expiry {expiry_comparison} :at "
        "AND attribute_not_exists(next_retry_at) "
        "AND attribute_not_exists(message_id) AND attribute_not_exists(sent_at) "
        "AND attribute_not_exists(abandoned_at) AND attribute_not_exists(abandon_reason)"
    )
    return condition, values


def _prepared_operation_condition(
    operation: OutboxOperation,
) -> tuple[str, DynamoItem]:
    if operation.status is not OutboxStatus.PREPARED or operation.delivery_attempt < 1:
        raise RepositoryConflict("outbox reconciliation requires an attempted prepared operation")
    condition, values = _operation_identity_condition(operation)
    values[":prepared"] = OutboxStatus.PREPARED.value
    if operation.next_retry_at is None:
        retry_condition = "attribute_not_exists(next_retry_at)"
    else:
        values[":next_retry"] = _timestamp(operation.next_retry_at)
        retry_condition = "next_retry_at=:next_retry"
    condition += (
        "AND #status=:prepared AND attribute_not_exists(claim_owner) "
        "AND attribute_not_exists(claim_expiry) AND "
        + retry_condition
        + " AND attribute_not_exists(message_id) AND attribute_not_exists(sent_at) "
        "AND attribute_not_exists(abandoned_at) AND attribute_not_exists(abandon_reason)"
    )
    return condition, values


def _same_outbox_identity(first: OutboxOperation, second: OutboxOperation) -> bool:
    return (
        first.operation_id == second.operation_id
        and first.debate_id == second.debate_id
        and first.attempt_id == second.attempt_id
        and first.bot_slot is second.bot_slot
        and first.thread_id == second.thread_id
        and first.delivery_target is second.delivery_target
        and first.channel_id == second.channel_id
        and first.content == second.content
        and first.content_hash == second.content_hash
        and first.nonce == second.nonce
        and first.chunk_sequence == second.chunk_sequence
        and first.created_at == second.created_at
        and first.delivery_attempt == second.delivery_attempt
        and first.record_schema_version == second.record_schema_version
        and first.phase is second.phase
        and first.plan_id == second.plan_id
        and first.delivery_sequence == second.delivery_sequence
        and first.deadline_at == second.deadline_at
    )


def _outbox_key(operation: OutboxOperation) -> DynamoItem:
    return {
        "PK": f"DEBATE#{operation.debate_id}",
        "SK": f"ATTEMPT#{operation.attempt_id}#OUTBOX#{operation.operation_id}",
    }


def _outbox_activity_key() -> DynamoItem:
    return {"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"}


def _client_token(value: str) -> str:
    import hashlib

    return f"ob-{hashlib.sha256(value.encode()).hexdigest()[:33]}"


def _transaction_token(label: str, actions: list[TransactWriteItemTypeDef]) -> str:
    """Bind DynamoDB idempotency to the complete transaction request body."""

    canonical = json.dumps(actions, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return _client_token(f"{label}:{canonical}")


def _require_transaction_size(actions: list[TransactWriteItemTypeDef]) -> None:
    if not 1 <= len(actions) <= 100:
        raise RepositoryConflict("DynamoDB transaction action count is outside its bounds")
    encoded = json.dumps(
        actions,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_TRANSACTION_BYTES:
        raise RepositoryConflict("DynamoDB transaction exceeds the 4 MB aggregate limit")


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
