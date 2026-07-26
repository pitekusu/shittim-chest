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
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    deserialize_ingress_operation_result,
    deserialize_panel_operation,
    deserialize_snapshot,
    ingress_request_sort_key_from_identity,
    serialize_panel_operation,
    serialize_snapshot,
)
from shittim_chest.application.discord import PanelOperation, PanelOperationKind
from shittim_chest.application.models import DebateSnapshot, LeaseGrant
from shittim_chest.application.ports import (
    RepositoryBusy,
    RepositoryClaimLost,
    RepositoryConflict,
    RepositoryQuotaExceeded,
)
from shittim_chest.application.scale_to_zero import IngressClaimFence, IngressStatus
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

LEASE_SECONDS = 60
INGRESS_PREPARE_LEASE_MIN_SECONDS = 50
PANEL_REFRESH_CLAIM_SECONDS = 60
PANEL_REFRESH_COUNT_LIMIT = 100_000
PANEL_REFRESH_QUERY_LIMIT = 20
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

    async def get_operation_result(
        self,
        operation_id: str,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot | None:
        return await asyncio.to_thread(
            self._get_claim_fenced_operation_result,
            operation_id,
            ingress_claim,
        )

    async def create(
        self,
        snapshot: DebateSnapshot,
        *,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._create,
            snapshot,
            operation_id,
            lease_owner,
            ingress_claim,
        )

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None:
        return await asyncio.to_thread(self._load_snapshot, debate_id, None)

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._replace,
            expected,
            updated,
            operation_id,
            ingress_claim,
        )

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._create_retry,
            expected_failed,
            retry,
            operation_id,
            lease_owner,
            ingress_claim,
        )

    async def reclaim_for_ingress(
        self,
        *,
        expected: DebateSnapshot,
        lease_owner: str,
        at: datetime,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._reclaim_for_ingress,
            expected,
            lease_owner,
            at,
            ingress_claim,
        )

    async def fail_pre_activation(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._fail_pre_activation,
            expected,
            updated,
            ingress_claim,
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

    async def claim_panel_refresh(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        return await asyncio.to_thread(
            self._claim_panel_refresh,
            debate_id,
            attempt_id,
            claim_owner,
            at,
        )

    async def claim_next_due_panel_refresh(
        self,
        *,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        return await asyncio.to_thread(self._claim_next_due_panel_refresh, claim_owner, at)

    async def complete_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._complete_panel_refresh,
            expected,
            claim_owner,
            at,
        )

    async def reschedule_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._reschedule_panel_refresh,
            expected,
            claim_owner,
            at,
            next_attempt_at,
        )

    async def abandon_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> DebateSnapshot:
        return await asyncio.to_thread(
            self._abandon_panel_refresh,
            expected,
            claim_owner,
            at,
            error_code,
        )

    async def pending_panel_refresh_count(self) -> int:
        return await asyncio.to_thread(self._pending_panel_refresh_count)

    async def abandoned_panel_refresh_count(self) -> int:
        return await asyncio.to_thread(self._abandoned_panel_refresh_count)

    def _create(
        self,
        snapshot: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None,
    ) -> DebateSnapshot:
        snapshot = _with_ingress_origin(snapshot, ingress_claim)
        self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
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
                *self._ingress_claim_actions(ingress_claim),
                candidate.action,
                self._quota_action(persisted.guild_id, now),
                *(self._put_new(item) for item in serialize_snapshot(persisted)),
                self._put_new(serialize_panel_operation(operation)),
            ]
            try:
                token_source = (
                    f"{self._table_name}:{operation_id}:"
                    f"{_ingress_claim_token_component(ingress_claim)}"
                )
                self._transact(actions, token=_client_token(token_source, candidate.grant.slot))
                return persisted
            except RepositoryConflict:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    self._require_current_ingress_claim(
                        ingress_claim,
                        operation_id=operation_id,
                    )
                    return replay
                self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
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
        ingress_claim: IngressClaimFence | None,
    ) -> DebateSnapshot:
        self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
        if operation_id is not None:
            replay = self._get_operation_result(operation_id)
            if replay is not None:
                return replay
        _require_same_attempt(expected, updated)
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("active write requires a fenced lease")
        persisted = replace(updated, lease=None) if updated.state.phase.is_terminal else updated
        if persisted.state.phase.is_terminal:
            persisted = _require_panel_refresh(persisted)
        old_items = _items_by_key(serialize_snapshot(expected))
        new_items = _items_by_key(serialize_snapshot(persisted))
        attempt_key = _attempt_key(expected.state.debate_id, expected.state.attempt_id)
        attempt_tuple = (_text(attempt_key, "PK"), _text(attempt_key, "SK"))
        attempt_item = new_items.pop(attempt_tuple)
        actions = [
            *self._ingress_claim_actions(ingress_claim),
            self._update_expected_attempt(
                previous=old_items[attempt_tuple],
                updated=attempt_item,
                expected=expected,
                write_at=persisted.state.updated_at,
            ),
        ]
        for key, item in new_items.items():
            if old_items.get(key) != item:
                actions.append(self._put(item))
        if persisted.state.phase.is_terminal:
            actions.append(self._release_slot_action(lease, persisted.state.updated_at))
        if not expected.panel_refresh_pending and persisted.panel_refresh_pending:
            actions.append(self._panel_refresh_count_action(1, persisted.state.updated_at))
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
                    _ingress_claim_token_component(ingress_claim),
                )
            )
            self._transact(actions, token=_client_token(token_source))
        except RepositoryConflict:
            if operation_id is not None:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    self._require_current_ingress_claim(
                        ingress_claim,
                        operation_id=operation_id,
                    )
                    return replay
            self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
            raise
        return persisted

    def _create_retry(
        self,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None,
    ) -> DebateSnapshot:
        retry = _with_ingress_origin(retry, ingress_claim)
        self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
        replay = self._get_operation_result(operation_id)
        if replay is not None:
            return replay
        if expected_failed.state.phase is not DebatePhase.FAILED:
            raise RepositoryConflict("retry source is not failed")
        if retry.state.retry_of != expected_failed.state.attempt_id:
            raise RepositoryConflict("retry source attempt does not match")
        if _panel_context_complete(expected_failed) and (
            expected_failed.panel_refresh_required_at is None
            or expected_failed.panel_refreshed_at is None
            or expected_failed.panel_refreshed_at < expected_failed.panel_refresh_required_at
        ):
            raise RepositoryConflict("retry source panel has not converged")
        candidates = self._slot_candidates(lease_owner, retry.state.updated_at)
        if not candidates:
            raise RepositoryBusy("all global lease slots are occupied")

        for candidate in candidates:
            persisted = _require_panel_refresh(replace(retry, lease=candidate.grant))
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
                *self._ingress_claim_actions(ingress_claim),
                candidate.action,
                self._condition_failed_attempt(expected_failed),
                self._put_current_attempt(debate_item, expected_failed.state.attempt_id),
                self._put_new(attempt_item),
                *(self._put_new(item) for item in items.values()),
                self._put_new(serialize_panel_operation(operation)),
            ]
            if persisted.panel_refresh_pending:
                actions.append(self._panel_refresh_count_action(1, persisted.state.updated_at))
            try:
                token_source = (
                    f"{self._table_name}:{operation_id}:"
                    f"{_ingress_claim_token_component(ingress_claim)}"
                )
                self._transact(actions, token=_client_token(token_source, candidate.grant.slot))
                return persisted
            except RepositoryConflict:
                replay = self._get_operation_result(operation_id)
                if replay is not None:
                    self._require_current_ingress_claim(
                        ingress_claim,
                        operation_id=operation_id,
                    )
                    return replay
                self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
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
            if not self._origin_ingress_is_accepted(snapshot):
                continue
            if snapshot.lease is not None and snapshot.lease.expires_at >= at:
                continue
            acquired = self._claim_one(snapshot, lease_owner, at)
            if acquired is not None:
                claimed.append(acquired)
        return tuple(claimed)

    def _claim_next_due_panel_refresh(
        self,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        _require_utc(at)
        for candidate in self._query_due_panel_refreshes(at):
            debate_id = DebateId.parse(_text(candidate, "debate_id"))
            attempt_id = AttemptId.parse(_text(candidate, "attempt_id"))
            work = self._claim_panel_refresh(
                debate_id,
                attempt_id,
                claim_owner,
                at,
            )
            if work is not None:
                return work
        return None

    def _claim_panel_refresh(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        _require_utc(at)
        if not claim_owner.strip():
            raise ValueError("panel refresh claim owner must not be empty")
        snapshot = self._load_snapshot(debate_id, attempt_id)
        if (
            snapshot is None
            or snapshot.state.attempt_id != attempt_id
            or not snapshot.panel_refresh_pending
        ):
            return None
        if (
            snapshot.panel_refresh_next_attempt_at is not None
            and snapshot.panel_refresh_next_attempt_at > at
        ):
            return None
        if (
            snapshot.panel_refresh_claim_expires_at is not None
            and snapshot.panel_refresh_claim_expires_at >= at
        ):
            return None
        if not snapshot.state.phase.is_terminal and not self._origin_ingress_is_accepted(snapshot):
            return None
        required_at = snapshot.panel_refresh_required_at
        if required_at is None:  # pragma: no cover - model invariant narrows this
            raise RepositoryConflict("pending panel refresh has no desired version")
        expiry = at + timedelta(seconds=PANEL_REFRESH_CLAIM_SECONDS)
        values: DynamoItem = {
            ":phase": snapshot.state.phase.value,
            ":updated": _timestamp(snapshot.state.updated_at),
            ":required": _timestamp(required_at),
            ":owner": claim_owner,
            ":expiry": _timestamp(expiry),
            ":one": 1,
            ":zero": 0,
            ":panel_index": "PANEL_REFRESH",
            ":panel_index_sort": (f"{_timestamp(expiry)}#{debate_id}#{attempt_id}"),
        }
        refresh_condition = "panel_refresh_required_at=:required"
        if snapshot.panel_refreshed_at is None:
            refresh_condition += " AND attribute_not_exists(panel_refreshed_at)"
        else:
            values[":refreshed"] = _timestamp(snapshot.panel_refreshed_at)
            refresh_condition += " AND panel_refreshed_at=:refreshed"
        claim_condition: str
        if snapshot.panel_refresh_claim_owner is None:
            claim_condition = "attribute_not_exists(panel_refresh_claim_owner)"
        else:
            old_expiry = snapshot.panel_refresh_claim_expires_at
            if old_expiry is None:  # pragma: no cover - model invariant narrows this
                raise RepositoryConflict("panel refresh claim has no expiry")
            values[":old_owner"] = snapshot.panel_refresh_claim_owner
            values[":old_expiry"] = _timestamp(old_expiry)
            values[":at"] = _timestamp(at)
            claim_condition = (
                "panel_refresh_claim_owner=:old_owner "
                "AND panel_refresh_claim_expiry=:old_expiry "
                "AND panel_refresh_claim_expiry < :at"
            )
        retry_condition: str
        if snapshot.panel_refresh_next_attempt_at is None:
            retry_condition = "attribute_not_exists(panel_refresh_next_attempt_at)"
        else:
            values[":retry"] = _timestamp(snapshot.panel_refresh_next_attempt_at)
            values[":at"] = _timestamp(at)
            retry_condition = (
                "panel_refresh_next_attempt_at=:retry AND panel_refresh_next_attempt_at <= :at"
            )
        update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_attempt_key(debate_id, attempt_id)),
                    "UpdateExpression": (
                        "SET panel_refresh_claim_owner=:owner, "
                        "panel_refresh_claim_expiry=:expiry, "
                        "panel_refresh_delivery_attempt="
                        "if_not_exists(panel_refresh_delivery_attempt,:zero)+:one, "
                        "gsi2pk=:panel_index, gsi2sk=:panel_index_sort "
                        "REMOVE panel_refresh_next_attempt_at"
                    ),
                    "ConditionExpression": (
                        "#phase=:phase AND updated_at=:updated AND "
                        + refresh_condition
                        + " AND attribute_not_exists(panel_refresh_failed_at) "
                        "AND attribute_not_exists(panel_refresh_error_code)"
                        + " AND "
                        + claim_condition
                        + " AND "
                        + retry_condition
                    ),
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )
        try:
            self._transact(
                [self._current_attempt_check(debate_id, attempt_id), update],
                token=_client_token(
                    f"{self._table_name}:panel-claim:{attempt_id}:"
                    f"{snapshot.panel_refresh_delivery_attempt + 1}:{required_at}"
                ),
            )
        except RepositoryConflict:
            return None
        return replace(
            snapshot,
            panel_refresh_claim_owner=claim_owner,
            panel_refresh_claim_expires_at=expiry,
            panel_refresh_next_attempt_at=None,
            panel_refresh_delivery_attempt=snapshot.panel_refresh_delivery_attempt + 1,
        )

    def _complete_panel_refresh(
        self,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot:
        _require_utc(at)
        self._require_panel_refresh_claim(expected, claim_owner, at)
        required_at = expected.panel_refresh_required_at
        if required_at is None:  # pragma: no cover - model invariant narrows this
            raise RepositoryConflict("panel refresh has no desired version")
        updated = replace(
            expected,
            panel_refreshed_at=at,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=None,
        )
        set_index = not updated.state.phase.is_terminal and _panel_context_complete(updated)
        update_expression = (
            "SET panel_refreshed_at=:at"
            + (", gsi2pk=:recoverable, gsi2sk=:recoverable_sort" if set_index else "")
            + " REMOVE panel_refresh_claim_owner, panel_refresh_claim_expiry, "
            "panel_refresh_next_attempt_at" + ("" if set_index else ", gsi2pk, gsi2sk")
        )
        values = self._panel_refresh_claim_values(expected, claim_owner, at)
        if set_index:
            values.update(
                {
                    ":recoverable": "RECOVERABLE",
                    ":recoverable_sort": (
                        f"{_timestamp(updated.state.updated_at)}#"
                        f"{updated.state.debate_id}#{updated.state.attempt_id}"
                    ),
                }
            )
        action = self._panel_refresh_update(
            expected,
            update_expression=update_expression,
            values=values,
        )
        self._transact(
            [
                self._current_attempt_check(
                    expected.state.debate_id,
                    expected.state.attempt_id,
                ),
                action,
                self._panel_refresh_count_action(-1, at),
            ],
            token=_client_token(
                f"{self._table_name}:panel-complete:{expected.state.attempt_id}:"
                f"{expected.panel_refresh_delivery_attempt}:{required_at}"
            ),
        )
        return updated

    def _reschedule_panel_refresh(
        self,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> DebateSnapshot:
        _require_utc(at)
        _require_utc(next_attempt_at)
        if next_attempt_at <= at:
            raise ValueError("panel refresh retry must be in the future")
        self._require_panel_refresh_claim(expected, claim_owner, at)
        updated = replace(
            expected,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=next_attempt_at,
        )
        values = self._panel_refresh_claim_values(expected, claim_owner, at)
        values.update(
            {
                ":retry": _timestamp(next_attempt_at),
                ":panel_index": "PANEL_REFRESH",
                ":panel_index_sort": (
                    f"{_timestamp(next_attempt_at)}#{expected.state.debate_id}#"
                    f"{expected.state.attempt_id}"
                ),
            }
        )
        action = self._panel_refresh_update(
            expected,
            update_expression=(
                "SET panel_refresh_next_attempt_at=:retry, "
                "gsi2pk=:panel_index, gsi2sk=:panel_index_sort "
                "REMOVE panel_refresh_claim_owner, panel_refresh_claim_expiry"
            ),
            values=values,
        )
        self._transact(
            [
                self._current_attempt_check(
                    expected.state.debate_id,
                    expected.state.attempt_id,
                ),
                action,
            ],
            token=_client_token(
                f"{self._table_name}:panel-retry:{expected.state.attempt_id}:"
                f"{expected.panel_refresh_delivery_attempt}:{next_attempt_at}"
            ),
        )
        return updated

    def _abandon_panel_refresh(
        self,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> DebateSnapshot:
        _require_utc(at)
        if not error_code.strip():
            raise ValueError("panel refresh error code must not be empty")
        if len(error_code) > 100:
            raise ValueError("panel refresh error code must be at most 100 characters")
        self._require_panel_refresh_claim(expected, claim_owner, at)
        required_at = expected.panel_refresh_required_at
        if required_at is None:  # pragma: no cover - model invariant narrows this
            raise RepositoryConflict("panel refresh has no desired version")
        updated = replace(
            expected,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=None,
            panel_refresh_failed_at=at,
            panel_refresh_error_code=error_code,
        )
        values = self._panel_refresh_claim_values(expected, claim_owner, at)
        values[":error_code"] = error_code
        action = self._panel_refresh_update(
            expected,
            update_expression=(
                "SET panel_refresh_failed_at=:at, panel_refresh_error_code=:error_code "
                "REMOVE panel_refresh_claim_owner, panel_refresh_claim_expiry, "
                "panel_refresh_next_attempt_at, gsi2pk, gsi2sk"
            ),
            values=values,
        )
        self._transact(
            [
                self._current_attempt_check(
                    expected.state.debate_id,
                    expected.state.attempt_id,
                ),
                action,
                self._panel_refresh_count_action(-1, at),
                self._panel_refresh_abandoned_count_action(at),
            ],
            token=_client_token(
                f"{self._table_name}:panel-abandon:{expected.state.attempt_id}:"
                f"{expected.panel_refresh_delivery_attempt}:{required_at}:{error_code}"
            ),
        )
        return updated

    def _require_panel_refresh_claim(
        self,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> None:
        if (
            not expected.panel_refresh_pending
            or expected.panel_refresh_claim_owner != claim_owner
            or expected.panel_refresh_claim_expires_at is None
            or expected.panel_refresh_claim_expires_at < at
        ):
            raise RepositoryConflict("panel refresh claim is no longer current")

    def _panel_refresh_claim_values(
        self,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> DynamoItem:
        required_at = expected.panel_refresh_required_at
        expiry = expected.panel_refresh_claim_expires_at
        if required_at is None or expiry is None:
            raise RepositoryConflict("panel refresh claim is incomplete")
        return {
            ":phase": expected.state.phase.value,
            ":updated": _timestamp(expected.state.updated_at),
            ":required": _timestamp(required_at),
            ":owner": claim_owner,
            ":expiry": _timestamp(expiry),
            ":at": _timestamp(at),
            ":attempts": expected.panel_refresh_delivery_attempt,
        }

    def _panel_refresh_update(
        self,
        expected: DebateSnapshot,
        *,
        update_expression: str,
        values: DynamoItem,
    ) -> TransactWriteItemTypeDef:
        refreshed_condition = "attribute_not_exists(panel_refreshed_at)"
        if expected.panel_refreshed_at is not None:
            values[":refreshed"] = _timestamp(expected.panel_refreshed_at)
            refreshed_condition = "panel_refreshed_at=:refreshed"
        failed_condition = "attribute_not_exists(panel_refresh_failed_at)"
        if expected.panel_refresh_failed_at is not None:
            values[":failed"] = _timestamp(expected.panel_refresh_failed_at)
            failed_condition = "panel_refresh_failed_at=:failed"
        error_condition = "attribute_not_exists(panel_refresh_error_code)"
        if expected.panel_refresh_error_code is not None:
            values[":panel_error"] = expected.panel_refresh_error_code
            error_condition = "panel_refresh_error_code=:panel_error"
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
                        "#phase=:phase AND updated_at=:updated "
                        "AND panel_refresh_required_at=:required "
                        "AND "
                        + refreshed_condition
                        + " AND "
                        + failed_condition
                        + " AND "
                        + error_condition
                        + " AND panel_refresh_claim_owner=:owner "
                        "AND panel_refresh_claim_expiry=:expiry "
                        "AND panel_refresh_claim_expiry >= :at "
                        "AND panel_refresh_delivery_attempt=:attempts"
                    ),
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _reclaim_for_ingress(
        self,
        expected: DebateSnapshot,
        lease_owner: str,
        at: datetime,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        _require_utc(at)
        if not lease_owner.strip():
            raise ValueError("lease owner must not be empty")
        if expected.state.phase.is_terminal:
            raise RepositoryConflict("terminal debate cannot reacquire a runtime lease")
        try:
            ingress_claim = ingress_claim.for_write_at(at)
        except ValueError as error:
            raise RepositoryClaimLost("ingress claim expired before lease recovery") from error
        self._require_current_ingress_claim(
            ingress_claim,
            operation_id=ingress_claim.operation_id,
        )
        _require_ingress_origin(expected, ingress_claim)
        lease = expected.lease
        if lease is not None and lease.expires_at >= at:
            if lease.owner_id != lease_owner:
                raise RepositoryBusy("the replayed attempt still has an active runtime lease")
            if lease.expires_at >= at + timedelta(seconds=INGRESS_PREPARE_LEASE_MIN_SECONDS):
                return expected
            return self._renew_for_ingress(expected, at, ingress_claim)
        candidates = self._slot_candidates(lease_owner, at)
        if not candidates:
            raise RepositoryBusy("all global lease slots are occupied")

        for candidate in candidates:
            values: DynamoItem = {
                ":phase": expected.state.phase.value,
                ":recovery": expected.state.recovery_state.value,
                ":updated": _timestamp(expected.state.updated_at),
                ":owner": lease_owner,
                ":slot": candidate.grant.slot,
                ":token": candidate.grant.fencing_token,
                ":expiry": _timestamp(candidate.grant.expires_at),
                ":at": _timestamp(at),
                ":origin": ingress_claim.interaction_id,
            }
            lease_condition: str
            if lease is None:
                lease_condition = (
                    "attribute_not_exists(lease_owner) "
                    "AND attribute_not_exists(lease_expiry) "
                    "AND attribute_not_exists(fencing_token)"
                )
            else:
                lease_condition = (
                    "lease_owner=:old_owner AND lease_slot=:old_slot "
                    "AND fencing_token=:old_token AND lease_expiry=:old_expiry "
                    "AND lease_expiry < :at"
                )
                values.update(
                    {
                        ":old_owner": lease.owner_id,
                        ":old_slot": lease.slot,
                        ":old_token": lease.fencing_token,
                        ":old_expiry": _timestamp(lease.expires_at),
                    }
                )
            attempt_update = cast(
                TransactWriteItemTypeDef,
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": marshal_item(
                            _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                        ),
                        "UpdateExpression": (
                            "SET lease_owner=:owner, lease_slot=:slot, "
                            "fencing_token=:token, lease_expiry=:expiry"
                        ),
                        "ConditionExpression": (
                            "#phase=:phase AND recovery_state=:recovery "
                            "AND updated_at=:updated "
                            "AND origin_ingress_interaction_id=:origin AND " + lease_condition
                        ),
                        "ExpressionAttributeNames": {"#phase": "phase"},
                        "ExpressionAttributeValues": marshal_item(values),
                    }
                },
            )
            current_check = cast(
                TransactWriteItemTypeDef,
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": marshal_item(_debate_key(expected.state.debate_id)),
                        "ConditionExpression": "current_attempt_id=:attempt",
                        "ExpressionAttributeValues": marshal_item(
                            {":attempt": str(expected.state.attempt_id)}
                        ),
                    }
                },
            )
            actions = (
                *self._ingress_claim_actions(ingress_claim),
                candidate.action,
                current_check,
                attempt_update,
            )
            try:
                token_source = (
                    f"{self._table_name}:ingress-reclaim:{expected.state.attempt_id}:"
                    f"{_ingress_claim_token_component(ingress_claim)}"
                )
                self._transact(
                    actions,
                    token=_client_token(token_source, candidate.grant.slot),
                )
                return replace(expected, lease=candidate.grant)
            except RepositoryConflict:
                self._require_current_ingress_claim(
                    ingress_claim,
                    operation_id=ingress_claim.operation_id,
                )
                current = self._load_snapshot(expected.state.debate_id, None)
                if (
                    current is not None
                    and current.state.attempt_id == expected.state.attempt_id
                    and current.origin_ingress_interaction_id == ingress_claim.interaction_id
                    and current.lease is not None
                    and current.lease.owner_id == lease_owner
                    and current.lease.expires_at
                    >= at + timedelta(seconds=INGRESS_PREPARE_LEASE_MIN_SECONDS)
                ):
                    return current
                if current is None or current.state != expected.state:
                    raise RepositoryConflict("replayed attempt is no longer current") from None
        raise RepositoryBusy("all global lease slots were claimed concurrently")

    def _renew_for_ingress(
        self,
        expected: DebateSnapshot,
        at: datetime,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("ingress replay cannot renew a missing lease")
        renewed = replace(lease, expires_at=at + timedelta(seconds=LEASE_SECONDS))
        values: DynamoItem = {
            ":phase": expected.state.phase.value,
            ":recovery": expected.state.recovery_state.value,
            ":updated": _timestamp(expected.state.updated_at),
            ":owner": lease.owner_id,
            ":slot": lease.slot,
            ":token": lease.fencing_token,
            ":old_expiry": _timestamp(lease.expires_at),
            ":new_expiry": _timestamp(renewed.expires_at),
            ":now": _timestamp(at),
            ":origin": ingress_claim.interaction_id,
        }
        attempt_update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                    ),
                    "UpdateExpression": "SET lease_expiry=:new_expiry",
                    "ConditionExpression": (
                        "#phase=:phase AND recovery_state=:recovery AND updated_at=:updated "
                        "AND lease_owner=:owner AND lease_slot=:slot "
                        "AND fencing_token=:token AND lease_expiry=:old_expiry "
                        "AND lease_expiry >= :now AND origin_ingress_interaction_id=:origin"
                    ),
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )
        slot_values: DynamoItem = {
            ":owner": lease.owner_id,
            ":token": lease.fencing_token,
            ":old_expiry": _timestamp(lease.expires_at),
            ":new_expiry": _timestamp(renewed.expires_at),
            ":now": _timestamp(at),
        }
        slot_update = cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_slot_key(lease.slot)),
                    "UpdateExpression": "SET lease_expiry=:new_expiry, updated_at=:now",
                    "ConditionExpression": (
                        "lease_owner=:owner AND fencing_token=:token "
                        "AND lease_expiry=:old_expiry AND lease_expiry >= :now"
                    ),
                    "ExpressionAttributeValues": marshal_item(slot_values),
                }
            },
        )
        current_check = self._current_attempt_check(
            expected.state.debate_id,
            expected.state.attempt_id,
        )
        token_source = (
            f"{self._table_name}:ingress-renew:{expected.state.attempt_id}:"
            f"{_ingress_claim_token_component(ingress_claim)}:{lease.expires_at}"
        )
        try:
            self._transact(
                (
                    *self._ingress_claim_actions(ingress_claim),
                    current_check,
                    attempt_update,
                    slot_update,
                ),
                token=_client_token(token_source),
            )
        except RepositoryConflict:
            self._require_current_ingress_claim(
                ingress_claim,
                operation_id=ingress_claim.operation_id,
            )
            current = self._load_snapshot(expected.state.debate_id, None)
            if (
                current is not None
                and current.state.attempt_id == expected.state.attempt_id
                and current.origin_ingress_interaction_id == ingress_claim.interaction_id
                and current.lease is not None
                and current.lease.owner_id == lease.owner_id
                and current.lease.fencing_token == lease.fencing_token
                and current.lease.expires_at >= renewed.expires_at
            ):
                return current
            raise
        return replace(expected, lease=renewed)

    def _fail_pre_activation(
        self,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        _require_same_attempt(expected, updated)
        _require_ingress_origin(expected, ingress_claim)
        if expected.state.phase.is_terminal or updated.state.phase is not DebatePhase.FAILED:
            raise RepositoryConflict("pre-activation compensation requires an active attempt")
        if updated.error_code is None:
            raise ValueError("pre-activation compensation requires an error code")
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("pre-activation compensation requires a leased attempt")
        persisted = _require_panel_refresh(replace(updated, lease=None))
        old_items = _items_by_key(serialize_snapshot(expected))
        new_items = _items_by_key(serialize_snapshot(persisted))
        attempt_key = _attempt_key(expected.state.debate_id, expected.state.attempt_id)
        attempt_tuple = (_text(attempt_key, "PK"), _text(attempt_key, "SK"))
        previous_attempt = old_items[attempt_tuple]
        updated_attempt = new_items.pop(attempt_tuple)

        for _attempt in range(2):
            actions: list[TransactWriteItemTypeDef] = [
                *self._ingress_claim_actions(ingress_claim),
                self._update_pre_activation_attempt(
                    previous=previous_attempt,
                    updated=updated_attempt,
                    expected=expected,
                    ingress_claim=ingress_claim,
                ),
            ]
            for key, item in new_items.items():
                if old_items.get(key) != item:
                    debate_key = _debate_key(expected.state.debate_id)
                    debate_tuple = (_text(debate_key, "PK"), _text(debate_key, "SK"))
                    actions.append(
                        self._put_pre_activation_meta(item, expected)
                        if key == debate_tuple
                        else self._put(item)
                    )
            if not expected.panel_refresh_pending and persisted.panel_refresh_pending:
                actions.append(self._panel_refresh_count_action(1, persisted.state.updated_at))
            slot = self._get_item(_slot_key(lease.slot))
            if (
                slot is not None
                and slot.get("lease_owner") == lease.owner_id
                and slot.get("fencing_token") == lease.fencing_token
            ):
                actions.append(self._release_slot_action(lease, persisted.state.updated_at))
            token_source = (
                f"{self._table_name}:pre-activation-fail:{expected.state.attempt_id}:"
                f"{persisted.error_code}:{_ingress_claim_token_component(ingress_claim)}:"
                f"{len(actions)}"
            )
            try:
                self._transact(actions, token=_client_token(token_source))
                return persisted
            except RepositoryConflict:
                self._require_current_ingress_claim(
                    ingress_claim,
                    operation_id=ingress_claim.operation_id,
                )
                current = self._load_snapshot(expected.state.debate_id, None)
                if (
                    current is not None
                    and current.state.attempt_id == expected.state.attempt_id
                    and current.state.phase is DebatePhase.FAILED
                    and current.origin_ingress_interaction_id == ingress_claim.interaction_id
                    and current.error_code == persisted.error_code
                    and current.lease is None
                ):
                    return current
                if (
                    current is None
                    or current.state != expected.state
                    or current.lease != expected.lease
                ):
                    raise RepositoryConflict(
                        "pre-activation attempt is no longer current"
                    ) from None
        raise RepositoryConflict("pre-activation slot release lost repeated races")

    def _update_pre_activation_attempt(
        self,
        *,
        previous: DynamoItem,
        updated: DynamoItem,
        expected: DebateSnapshot,
        ingress_claim: IngressClaimFence,
    ) -> TransactWriteItemTypeDef:
        lease = expected.lease
        if lease is None:
            raise RepositoryConflict("pre-activation attempt has no lease")
        key_fields = {"PK", "SK"}
        set_fields = sorted(set(updated) - key_fields)
        remove_fields = sorted(set(previous) - set(updated) - key_fields)
        names = {"#phase": "phase"}
        values: DynamoItem = {
            ":phase": expected.state.phase.value,
            ":recovery": expected.state.recovery_state.value,
            ":updated": _timestamp(expected.state.updated_at),
            ":owner": lease.owner_id,
            ":slot": lease.slot,
            ":token": lease.fencing_token,
            ":expiry": _timestamp(lease.expires_at),
            ":origin": ingress_claim.interaction_id,
        }
        panel_condition = _panel_refresh_condition(expected, values)
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
        expression = f"SET {', '.join(assignments)}"
        if removals:
            expression += f" REMOVE {', '.join(removals)}"
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                    ),
                    "UpdateExpression": expression,
                    "ConditionExpression": (
                        "#phase=:phase AND recovery_state=:recovery AND updated_at=:updated "
                        "AND lease_owner=:owner AND lease_slot=:slot "
                        "AND fencing_token=:token AND lease_expiry=:expiry "
                        "AND origin_ingress_interaction_id=:origin AND " + panel_condition
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _origin_ingress_is_accepted(self, snapshot: DebateSnapshot) -> bool:
        interaction_id = snapshot.origin_ingress_interaction_id
        if interaction_id is None:
            return True
        item = self._get_item({"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RESULT"})
        if item is None:
            return False
        try:
            operation = deserialize_ingress_operation_result(item)
        except ValueError as error:
            raise RepositoryConflict("origin ingress operation is invalid") from error
        return (
            operation.status is IngressStatus.ACCEPTED
            and operation.interaction_id == interaction_id
            and operation.accepted_debate_id == snapshot.state.debate_id
            and operation.accepted_attempt_id == snapshot.state.attempt_id
        )

    def _claim_one(
        self,
        snapshot: DebateSnapshot,
        lease_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        if snapshot.state.phase.is_terminal:
            return None
        for candidate in self._slot_candidates(lease_owner, at):
            values: DynamoItem = {
                ":phase": snapshot.state.phase.value,
                ":recovery": snapshot.state.recovery_state.value,
                ":updated": _timestamp(snapshot.state.updated_at),
                ":owner": lease_owner,
                ":slot": candidate.grant.slot,
                ":token": candidate.grant.fencing_token,
                ":expiry": _timestamp(candidate.grant.expires_at),
                ":now": _timestamp(at),
            }
            previous_lease = snapshot.lease
            if previous_lease is None:
                lease_condition = (
                    "attribute_not_exists(lease_owner) "
                    "AND attribute_not_exists(lease_slot) "
                    "AND attribute_not_exists(lease_expiry) "
                    "AND attribute_not_exists(fencing_token)"
                )
            else:
                values.update(
                    {
                        ":old_owner": previous_lease.owner_id,
                        ":old_slot": previous_lease.slot,
                        ":old_token": previous_lease.fencing_token,
                        ":old_expiry": _timestamp(previous_lease.expires_at),
                    }
                )
                lease_condition = (
                    "lease_owner=:old_owner AND lease_slot=:old_slot "
                    "AND fencing_token=:old_token AND lease_expiry=:old_expiry "
                    "AND lease_expiry < :now"
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
                            "#phase=:phase AND recovery_state=:recovery "
                            "AND updated_at=:updated AND " + lease_condition
                        ),
                        "ExpressionAttributeNames": {"#phase": "phase"},
                        "ExpressionAttributeValues": marshal_item(values),
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

    def _get_claim_fenced_operation_result(
        self,
        operation_id: str,
        ingress_claim: IngressClaimFence | None,
    ) -> DebateSnapshot | None:
        self._require_current_ingress_claim(ingress_claim, operation_id=operation_id)
        return self._get_operation_result(operation_id)

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

    def _panel_refresh_count_action(
        self,
        delta: int,
        at: datetime,
    ) -> TransactWriteItemTypeDef:
        if delta not in {-1, 1}:
            raise ValueError("panel refresh counter delta must be minus or plus one")
        values: DynamoItem = {
            ":one": 1,
            ":type": "panel_refresh_pending_counter",
            ":schema": CURRENT_SCHEMA_VERSION,
            ":at": _timestamp(at),
        }
        if delta > 0:
            values[":zero"] = 0
            values[":limit"] = PANEL_REFRESH_COUNT_LIMIT
            update_expression = (
                "SET #count=if_not_exists(#count,:zero)+:one, "
                "record_type=if_not_exists(record_type,:type), "
                "schema_version=:schema, updated_at=:at"
            )
            condition = (
                "(attribute_not_exists(#count) OR #count < :limit) AND "
                "(attribute_not_exists(record_type) OR record_type=:type) AND "
                "(attribute_not_exists(schema_version) OR schema_version=:schema)"
            )
        else:
            update_expression = "SET #count=#count-:one, updated_at=:at"
            condition = "record_type=:type AND schema_version=:schema AND #count >= :one"
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"}),
                    "UpdateExpression": update_expression,
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _panel_refresh_abandoned_count_action(
        self,
        at: datetime,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item({"PK": "CONTROL#PANEL_REFRESH", "SK": "ABANDONED_COUNT"}),
                    "UpdateExpression": (
                        "SET #count=if_not_exists(#count,:zero)+:one, "
                        "record_type=if_not_exists(record_type,:type), "
                        "schema_version=:schema, updated_at=:at"
                    ),
                    "ConditionExpression": (
                        "(attribute_not_exists(record_type) OR record_type=:type) AND "
                        "(attribute_not_exists(schema_version) OR schema_version=:schema)"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":type": "panel_refresh_abandoned_counter",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _pending_panel_refresh_count(self) -> int:
        item = self._get_item({"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"})
        if item is None:
            return 0
        if (
            _text(item, "record_type") != "panel_refresh_pending_counter"
            or _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION
        ):
            raise RepositoryConflict("panel refresh counter is invalid")
        count = _integer(item, "count")
        if not 0 <= count <= PANEL_REFRESH_COUNT_LIMIT:
            raise RepositoryConflict("panel refresh counter is outside its bounds")
        return count

    def _abandoned_panel_refresh_count(self) -> int:
        item = self._get_item({"PK": "CONTROL#PANEL_REFRESH", "SK": "ABANDONED_COUNT"})
        if item is None:
            return 0
        if (
            _text(item, "record_type") != "panel_refresh_abandoned_counter"
            or _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION
        ):
            raise RepositoryConflict("abandoned panel refresh counter is invalid")
        count = _integer(item, "count")
        if count < 0:
            raise RepositoryConflict("abandoned panel refresh counter cannot be negative")
        return count

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
        panel_condition = _panel_refresh_condition(expected, values)
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
                        "AND lease_owner=:owner AND fencing_token=:token AND lease_expiry >= :at "
                        "AND " + panel_condition
                    ),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _condition_failed_attempt(self, expected: DebateSnapshot) -> TransactWriteItemTypeDef:
        values: DynamoItem = {
            ":failed": DebatePhase.FAILED.value,
            ":updated": _timestamp(expected.state.updated_at),
        }
        panel_condition = ""
        if _panel_context_complete(expected):
            required_at = expected.panel_refresh_required_at
            if required_at is None:
                raise RepositoryConflict("failed panel has no durable refresh requirement")
            values[":panel_required"] = _timestamp(required_at)
            panel_condition = (
                " AND panel_refresh_required_at=:panel_required "
                "AND panel_refreshed_at >= :panel_required"
            )
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        _attempt_key(expected.state.debate_id, expected.state.attempt_id)
                    ),
                    "ConditionExpression": (
                        "#phase=:failed AND updated_at=:updated" + panel_condition
                    ),
                    "ExpressionAttributeNames": {"#phase": "phase"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _current_attempt_check(
        self,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_debate_key(debate_id)),
                    "ConditionExpression": "current_attempt_id=:attempt",
                    "ExpressionAttributeValues": marshal_item({":attempt": str(attempt_id)}),
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

    def _put_pre_activation_meta(
        self,
        item: DynamoItem,
        expected: DebateSnapshot,
    ) -> TransactWriteItemTypeDef:
        context = (
            expected.starter_message_id,
            expected.thread_id,
            expected.control_panel_message_id,
        )
        values: DynamoItem = {":attempt": str(expected.state.attempt_id)}
        if all(value is None for value in context):
            context_condition = (
                "attribute_not_exists(starter_message_id) "
                "AND attribute_not_exists(thread_id) "
                "AND attribute_not_exists(control_panel_message_id)"
            )
        elif all(value is not None for value in context):
            context_condition = (
                "starter_message_id=:starter AND thread_id=:thread "
                "AND control_panel_message_id=:panel"
            )
            values.update(
                {
                    ":starter": expected.starter_message_id,
                    ":thread": expected.thread_id,
                    ":panel": expected.control_panel_message_id,
                }
            )
        else:
            raise RepositoryConflict("partially bound Discord context cannot be compensated")
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(item),
                    "ConditionExpression": ("current_attempt_id=:attempt AND " + context_condition),
                    "ExpressionAttributeValues": marshal_item(values),
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

    def _ingress_claim_actions(
        self,
        ingress_claim: IngressClaimFence | None,
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        if ingress_claim is None:
            return ()
        request_sort_key = ingress_request_sort_key_from_identity(
            created_at=ingress_claim.created_at,
            interaction_id=ingress_claim.interaction_id,
        )
        common_values: DynamoItem = {
            ":claimed_status": IngressStatus.CLAIMED.value,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": ingress_claim.schema_version,
            ":interaction_id": ingress_claim.interaction_id,
            ":operation_id": ingress_claim.operation_id,
            ":created_at": _timestamp(ingress_claim.created_at),
        }
        request_values: DynamoItem = {
            **common_values,
            ":request_type": "ingress_request",
            ":interaction_kind": ingress_claim.kind.value,
            ":claim_owner": ingress_claim.claim_owner,
            ":claim_expiry": _timestamp(ingress_claim.claim_expires_at),
            ":write_at": _timestamp(ingress_claim.write_at),
            ":delivery_attempt": ingress_claim.delivery_attempt,
        }
        operation_values: DynamoItem = {
            **common_values,
            ":operation_type": "ingress_operation_result",
            ":request_sort_key": request_sort_key,
        }
        return (
            cast(
                TransactWriteItemTypeDef,
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": marshal_item({"PK": "CONTROL#INGRESS", "SK": request_sort_key}),
                        "ConditionExpression": (
                            "#status=:claimed_status AND schema_version=:schema "
                            "AND record_schema_version=:record_schema "
                            "AND record_type=:request_type "
                            "AND interaction_id=:interaction_id "
                            "AND operation_id=:operation_id AND created_at=:created_at "
                            "AND interaction_kind=:interaction_kind "
                            "AND claim_owner=:claim_owner AND claim_expiry=:claim_expiry "
                            "AND claim_expiry > :write_at "
                            "AND delivery_attempt=:delivery_attempt"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": marshal_item(request_values),
                    }
                },
            ),
            cast(
                TransactWriteItemTypeDef,
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": marshal_item(
                            {
                                "PK": f"INGRESS_OPERATION#{ingress_claim.interaction_id}",
                                "SK": "RESULT",
                            }
                        ),
                        "ConditionExpression": (
                            "#status=:claimed_status AND schema_version=:schema "
                            "AND record_schema_version=:record_schema "
                            "AND record_type=:operation_type "
                            "AND interaction_id=:interaction_id "
                            "AND operation_id=:operation_id "
                            "AND request_sort_key=:request_sort_key "
                            "AND created_at=:created_at"
                        ),
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": marshal_item(operation_values),
                    }
                },
            ),
        )

    def _require_current_ingress_claim(
        self,
        ingress_claim: IngressClaimFence | None,
        *,
        operation_id: str | None,
    ) -> None:
        if ingress_claim is None:
            return
        if operation_id is None or ingress_claim.operation_id != operation_id:
            raise RepositoryClaimLost("ingress claim is bound to another operation")
        if ingress_claim.claim_expires_at <= ingress_claim.write_at:
            raise RepositoryClaimLost("ingress claim expired before the durable write")
        request_sort_key = ingress_request_sort_key_from_identity(
            created_at=ingress_claim.created_at,
            interaction_id=ingress_claim.interaction_id,
        )
        request = self._get_item({"PK": "CONTROL#INGRESS", "SK": request_sort_key})
        operation = self._get_item(
            {
                "PK": f"INGRESS_OPERATION#{ingress_claim.interaction_id}",
                "SK": "RESULT",
            }
        )
        expected_common: DynamoItem = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": ingress_claim.schema_version,
            "interaction_id": ingress_claim.interaction_id,
            "operation_id": ingress_claim.operation_id,
            "status": IngressStatus.CLAIMED.value,
            "created_at": _timestamp(ingress_claim.created_at),
        }
        expected_request: DynamoItem = {
            **expected_common,
            "record_type": "ingress_request",
            "interaction_kind": ingress_claim.kind.value,
            "claim_owner": ingress_claim.claim_owner,
            "claim_expiry": _timestamp(ingress_claim.claim_expires_at),
            "delivery_attempt": ingress_claim.delivery_attempt,
        }
        expected_operation: DynamoItem = {
            **expected_common,
            "record_type": "ingress_operation_result",
            "request_sort_key": request_sort_key,
        }
        if not _contains_exact_values(request, expected_request) or not _contains_exact_values(
            operation,
            expected_operation,
        ):
            raise RepositoryClaimLost("ingress claim is no longer current")

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

    def _query_due_panel_refreshes(self, at: datetime) -> list[DynamoItem]:
        response = self._client.query(
            TableName=self._table_name,
            IndexName=RECOVERABLE_INDEX,
            KeyConditionExpression="gsi2pk=:panel AND gsi2sk <= :due",
            ExpressionAttributeValues=marshal_item(
                {
                    ":panel": "PANEL_REFRESH",
                    ":due": f"{_timestamp(at)}\uffff",
                }
            ),
            ScanIndexForward=True,
            # Read past stale GSI entries while still claiming at most one item.
            Limit=PANEL_REFRESH_QUERY_LIMIT,
        )
        return [unmarshal_item(item) for item in response.get("Items", [])]


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
        message_id=snapshot.control_panel_message_id,
    )


def _panel_context_complete(snapshot: DebateSnapshot) -> bool:
    values = (
        snapshot.starter_message_id,
        snapshot.thread_id,
        snapshot.control_panel_message_id,
    )
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise RepositoryConflict("Discord panel context is partially bound")
    return all(value is not None for value in values)


def _require_panel_refresh(snapshot: DebateSnapshot) -> DebateSnapshot:
    if not _panel_context_complete(snapshot):
        return replace(
            snapshot,
            panel_refresh_required_at=None,
            panel_refreshed_at=None,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=None,
            panel_refresh_delivery_attempt=0,
            panel_refresh_failed_at=None,
            panel_refresh_error_code=None,
        )
    return replace(
        snapshot,
        panel_refresh_required_at=snapshot.state.updated_at,
        panel_refresh_claim_owner=None,
        panel_refresh_claim_expires_at=None,
        panel_refresh_next_attempt_at=None,
        panel_refresh_delivery_attempt=0,
        panel_refresh_failed_at=None,
        panel_refresh_error_code=None,
    )


def _panel_refresh_condition(snapshot: DebateSnapshot, values: DynamoItem) -> str:
    conditions: list[str] = []
    for field, value in (
        ("panel_refresh_required_at", snapshot.panel_refresh_required_at),
        ("panel_refreshed_at", snapshot.panel_refreshed_at),
        ("panel_refresh_claim_expiry", snapshot.panel_refresh_claim_expires_at),
        ("panel_refresh_next_attempt_at", snapshot.panel_refresh_next_attempt_at),
        ("panel_refresh_failed_at", snapshot.panel_refresh_failed_at),
    ):
        if value is None:
            conditions.append(f"attribute_not_exists({field})")
        else:
            placeholder = f":expected_{field}"
            values[placeholder] = _timestamp(value)
            conditions.append(f"{field}={placeholder}")
    if snapshot.panel_refresh_claim_owner is None:
        conditions.append("attribute_not_exists(panel_refresh_claim_owner)")
    else:
        values[":expected_panel_refresh_claim_owner"] = snapshot.panel_refresh_claim_owner
        conditions.append("panel_refresh_claim_owner=:expected_panel_refresh_claim_owner")
    if snapshot.panel_refresh_error_code is None:
        conditions.append("attribute_not_exists(panel_refresh_error_code)")
    else:
        values[":expected_panel_refresh_error_code"] = snapshot.panel_refresh_error_code
        conditions.append("panel_refresh_error_code=:expected_panel_refresh_error_code")
    if snapshot.panel_refresh_delivery_attempt == 0:
        conditions.append("attribute_not_exists(panel_refresh_delivery_attempt)")
    else:
        values[":expected_panel_refresh_delivery_attempt"] = snapshot.panel_refresh_delivery_attempt
        conditions.append("panel_refresh_delivery_attempt=:expected_panel_refresh_delivery_attempt")
    return " AND ".join(conditions)


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


def _require_ingress_origin(
    snapshot: DebateSnapshot,
    ingress_claim: IngressClaimFence | None,
) -> None:
    expected = None if ingress_claim is None else ingress_claim.interaction_id
    if snapshot.origin_ingress_interaction_id != expected:
        raise RepositoryClaimLost("attempt origin does not match the exact ingress claim")


def _with_ingress_origin(
    snapshot: DebateSnapshot,
    ingress_claim: IngressClaimFence | None,
) -> DebateSnapshot:
    origin = None if ingress_claim is None else ingress_claim.interaction_id
    return replace(snapshot, origin_ingress_interaction_id=origin)


def _client_token(value: str, slot: int | None = None) -> str:
    suffix = "" if slot is None else f"-{slot}"
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}{suffix}"


def _ingress_claim_token_component(ingress_claim: IngressClaimFence | None) -> str:
    if ingress_claim is None:
        return "gateway"
    return ":".join(
        (
            ingress_claim.interaction_id,
            ingress_claim.claim_owner,
            str(ingress_claim.delivery_attempt),
            _timestamp(ingress_claim.claim_expires_at),
            ingress_claim.kind.value,
        )
    )


def _contains_exact_values(
    item: Mapping[str, DynamoValue] | None,
    expected: Mapping[str, DynamoValue],
) -> bool:
    return item is not None and all(item.get(field) == value for field, value in expected.items())


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


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
