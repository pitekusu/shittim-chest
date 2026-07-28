"""Strong, bounded activity inspection for safe scale-to-zero decisions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactGetItemTypeDef
else:
    TransactGetItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import INGRESS_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.outbox import (
    OUTBOX_ACTIVITY_LIMIT,
    OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNT_LIMIT,
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
    GLOBAL_LEASE_SLOTS,
    PANEL_REFRESH_COUNT_LIMIT,
)
from shittim_chest.adapters.dynamodb.runtime_state import (
    RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
    RUNTIME_ACTIVITY_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
)
from shittim_chest.application.ports import (
    IngressRepository,
    RepositoryConflict,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_QUEUE_LIMIT,
    IngressStatus,
    RuntimeActivity,
)


class DynamoDbRuntimeActivityInspector:
    """Combine fixed control records without Scan or an eventually consistent GSI."""

    def __init__(
        self,
        *,
        client: DynamoDBClient,
        table_name: str,
        ingress: IngressRepository,
    ) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name
        self._ingress = ingress

    async def inspect(self, *, at: datetime) -> RuntimeActivity:
        """Return a conservative snapshot; the stop transaction is the final fence."""

        _require_utc(at)
        try:
            candidates, durable = await asyncio.gather(
                self._ingress.list_active_wake_candidates(),
                asyncio.to_thread(self._read_durable, at),
            )
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None

        pending = sum(candidate.status is IngressStatus.PENDING for candidate in candidates)
        claimed = sum(candidate.status is IngressStatus.CLAIMED for candidate in candidates)
        retrying = sum(candidate.status is IngressStatus.RETRYING for candidate in candidates)
        classified = pending + claimed + retrying
        if durable.active_ingress > classified:
            # A concurrent enqueue may commit between the pointer query and the
            # transactional counter read. Preserve the work conservatively.
            pending += durable.active_ingress - classified

        return RuntimeActivity(
            pending_ingress=pending,
            claimed_ingress=claimed,
            retrying_ingress=retrying,
            active_attempts=durable.active_attempts,
            application_tasks=durable.active_leases,
            active_leases=durable.active_leases,
            recovery_tasks=max(
                durable.expired_leases,
                durable.active_attempts - durable.active_leases,
            ),
            pending_outbox=durable.pending_outbox,
            claimed_outbox=durable.claimed_outbox,
            pending_status_updates=durable.pending_status,
            pending_panel_refreshes=durable.pending_panel_refreshes,
            # Every application/checkpoint task owns one of the three durable
            # lease slots. The slot is the cross-process stop fence.
            checkpoint_tasks=0,
        )

    def _read_durable(self, at: datetime) -> _DurableActivity:
        keys = (
            {"PK": "CONTROL#RUNTIME", "SK": "ACTIVITY_SCHEMA"},
            {"PK": "CONTROL#INGRESS", "SK": "COUNTER"},
            {"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"},
            {"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"},
            {"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"},
            {"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"},
            *({"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{slot}"} for slot in range(GLOBAL_LEASE_SLOTS)),
        )
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
        raw_responses = response.get("Responses", [])
        if len(raw_responses) != len(keys):
            raise RepositoryConflict("runtime activity transaction returned an invalid shape")
        items = tuple(
            None if raw.get("Item") is None else unmarshal_item(raw["Item"])
            for raw in raw_responses
        )
        _require_activity_schema(items[0])
        active_ingress = _counter(
            items[1],
            record_type="ingress_queue_counter",
            field="count",
            limit=INGRESS_QUEUE_LIMIT,
            record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
        )
        pending_status = _counter(
            items[2],
            record_type="ingress_status_pending_counter",
            field="count",
            limit=100_000,
            record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
        )
        pending_panel_refreshes = _counter(
            items[3],
            record_type="panel_refresh_pending_counter",
            field="count",
            limit=PANEL_REFRESH_COUNT_LIMIT,
        )
        pending_outbox, claimed_outbox = _outbox_counts(items[4])
        active_attempts = _counter(
            items[5],
            record_type="active_attempt_counter",
            field="count",
            limit=ACTIVE_ATTEMPT_COUNT_LIMIT,
            record_schema_version=ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
        )
        active_leases = 0
        expired_leases = 0
        for slot, item in enumerate(items[6:]):
            active, expired = _lease_activity(item, slot=slot, at=at)
            active_leases += active
            expired_leases += expired
        if active_leases + expired_leases > active_attempts:
            raise RepositoryConflict("runtime lease slots exceed active attempts")
        return _DurableActivity(
            active_ingress=active_ingress,
            active_attempts=active_attempts,
            pending_status=pending_status,
            pending_panel_refreshes=pending_panel_refreshes,
            pending_outbox=pending_outbox,
            claimed_outbox=claimed_outbox,
            active_leases=active_leases,
            expired_leases=expired_leases,
        )


class _DurableActivity:
    __slots__ = (
        "active_attempts",
        "active_ingress",
        "active_leases",
        "claimed_outbox",
        "expired_leases",
        "pending_outbox",
        "pending_panel_refreshes",
        "pending_status",
    )

    def __init__(
        self,
        *,
        active_ingress: int,
        active_attempts: int,
        pending_status: int,
        pending_panel_refreshes: int,
        pending_outbox: int,
        claimed_outbox: int,
        active_leases: int,
        expired_leases: int,
    ) -> None:
        self.active_ingress = active_ingress
        self.active_attempts = active_attempts
        self.pending_status = pending_status
        self.pending_panel_refreshes = pending_panel_refreshes
        self.pending_outbox = pending_outbox
        self.claimed_outbox = claimed_outbox
        self.active_leases = active_leases
        self.expired_leases = expired_leases


def _require_activity_schema(item: DynamoItem | None) -> None:
    if (
        item is None
        or item.get("record_type") != RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE
        or item.get("schema_version") != CURRENT_SCHEMA_VERSION
        or item.get("record_schema_version") != RUNTIME_ACTIVITY_SCHEMA_VERSION
    ):
        raise RepositoryConflict("runtime activity schema marker is invalid")


def _counter(
    item: DynamoItem | None,
    *,
    record_type: str,
    field: str,
    limit: int,
    record_schema_version: int | None = None,
) -> int:
    if item is None:
        raise RepositoryConflict(f"{record_type} is missing")
    value = item.get(field)
    if (
        item.get("record_type") != record_type
        or item.get("schema_version") != CURRENT_SCHEMA_VERSION
        or (
            record_schema_version is not None
            and item.get("record_schema_version") != record_schema_version
        )
        or (record_schema_version is None and "record_schema_version" in item)
        or isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= limit
    ):
        raise RepositoryConflict(f"{record_type} is invalid")
    return value


def _outbox_counts(item: DynamoItem | None) -> tuple[int, int]:
    if item is None:
        raise RepositoryConflict("outbox activity counter is missing")
    pending = item.get("pending_count")
    claimed = item.get("claimed_count")
    if (
        item.get("record_type") != "outbox_activity_counter"
        or item.get("schema_version") != CURRENT_SCHEMA_VERSION
        or item.get("record_schema_version") != OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION
        or isinstance(pending, bool)
        or not isinstance(pending, int)
        or isinstance(claimed, bool)
        or not isinstance(claimed, int)
        or not 0 <= pending <= OUTBOX_ACTIVITY_LIMIT
        or not 0 <= claimed <= OUTBOX_ACTIVITY_LIMIT
    ):
        raise RepositoryConflict("outbox activity counter is invalid")
    return pending, claimed


def _lease_activity(
    item: DynamoItem | None,
    *,
    slot: int,
    at: datetime,
) -> tuple[int, int]:
    if item is None:
        raise RepositoryConflict(f"runtime lease slot {slot} is missing")
    if (
        item.get("record_type") != "lease_slot"
        or item.get("schema_version") != CURRENT_SCHEMA_VERSION
        or item.get("slot") != slot
    ):
        raise RepositoryConflict("runtime lease slot is invalid")
    fencing_token = item.get("fencing_token")
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 0:
        raise RepositoryConflict("runtime lease slot fencing token is invalid")
    has_owner = "lease_owner" in item
    has_expiry = "lease_expiry" in item
    if not has_owner and not has_expiry:
        return 0, 0
    if has_owner != has_expiry:
        raise RepositoryConflict("runtime lease ownership is incomplete")
    owner = item.get("lease_owner")
    raw_expiry = item.get("lease_expiry")
    if not isinstance(owner, str) or not owner.strip() or not isinstance(raw_expiry, str):
        raise RepositoryConflict("runtime lease ownership is invalid")
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        _require_utc(expiry)
    except ValueError:
        raise RepositoryConflict("runtime lease expiry is invalid") from None
    return (1, 0) if expiry >= at else (0, 1)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")


__all__ = ("DynamoDbRuntimeActivityInspector",)
