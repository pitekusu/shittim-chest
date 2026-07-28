"""DynamoDB Local coverage for the complete scale-to-zero activity fence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.ingress import (
    INGRESS_RECORD_SCHEMA_VERSION,
    DynamoDbIngressRepository,
)
from shittim_chest.adapters.dynamodb.outbox import OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
    GLOBAL_LEASE_SLOTS,
)
from shittim_chest.adapters.dynamodb.runtime_activity import (
    DynamoDbRuntimeActivityInspector,
)
from shittim_chest.adapters.dynamodb.runtime_state import (
    RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
    RUNTIME_ACTIVITY_SCHEMA_VERSION,
    DynamoDbRuntimeStateRepository,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    serialize_runtime_state,
)
from shittim_chest.application import RuntimeState, RuntimeStatus
from shittim_chest.application.ports import RepositoryConflict

NOW = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _activity_records() -> tuple[DynamoItem, ...]:
    common = {"created_at": _timestamp(NOW), "updated_at": _timestamp(NOW)}
    return (
        {
            "PK": "CONTROL#RUNTIME",
            "SK": "ACTIVITY_SCHEMA",
            "record_type": RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": RUNTIME_ACTIVITY_SCHEMA_VERSION,
            **common,
        },
        {
            "PK": "CONTROL#INGRESS",
            "SK": "COUNTER",
            "record_type": "ingress_queue_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": INGRESS_RECORD_SCHEMA_VERSION,
            "count": 0,
            **common,
        },
        {
            "PK": "CONTROL#INGRESS",
            "SK": "STATUS_PENDING_COUNTER",
            "record_type": "ingress_status_pending_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": INGRESS_RECORD_SCHEMA_VERSION,
            "count": 0,
            **common,
        },
        {
            "PK": "CONTROL#PANEL_REFRESH",
            "SK": "PENDING_COUNT",
            "record_type": "panel_refresh_pending_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "count": 0,
            **common,
        },
        {
            "PK": "CONTROL#OUTBOX",
            "SK": "ACTIVITY",
            "record_type": "outbox_activity_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
            "pending_count": 0,
            "claimed_count": 0,
            **common,
        },
        {
            "PK": "CONTROL#DEBATE",
            "SK": "ACTIVE_ATTEMPT_COUNT",
            "record_type": "active_attempt_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
            "count": 0,
            **common,
        },
        *(
            {
                "PK": "CONTROL#GLOBAL",
                "SK": f"SLOT#{slot}",
                "record_type": "lease_slot",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "slot": slot,
                "fencing_token": 0,
                **common,
            }
            for slot in range(GLOBAL_LEASE_SLOTS)
        ),
    )


def _put_records(
    client: DynamoDBClient,
    table_name: str,
    records: tuple[DynamoItem, ...],
) -> None:
    for item in records:
        client.put_item(TableName=table_name, Item=marshal_item(item))


def _key(item: DynamoItem) -> DynamoItem:
    return {"PK": item["PK"], "SK": item["SK"]}


def _idle_state() -> RuntimeState:
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )
    return ready.begin_idle(at=NOW + timedelta(seconds=4))


@pytest.mark.asyncio
async def test_activity_inspection_requires_marker_and_every_counter(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    records = _activity_records()
    _put_records(dynamodb_client, dynamodb_table, records)
    inspector = DynamoDbRuntimeActivityInspector(
        client=dynamodb_client,
        table_name=dynamodb_table,
        ingress=DynamoDbIngressRepository(
            client=dynamodb_client,
            table_name=dynamodb_table,
        ),
    )

    assert (await inspector.inspect(at=NOW)).is_complete

    for record in records:
        dynamodb_client.delete_item(
            TableName=dynamodb_table,
            Key=marshal_item(_key(record)),
        )
        with pytest.raises(RepositoryConflict):
            await inspector.inspect(at=NOW)
        dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(record))


@pytest.mark.asyncio
async def test_activity_inspection_rejects_inexact_record_schemas(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    records = _activity_records()
    inspector = DynamoDbRuntimeActivityInspector(
        client=dynamodb_client,
        table_name=dynamodb_table,
        ingress=DynamoDbIngressRepository(
            client=dynamodb_client,
            table_name=dynamodb_table,
        ),
    )
    corruptions = (
        (0, {**records[0], "record_schema_version": RUNTIME_ACTIVITY_SCHEMA_VERSION + 1}),
        (1, {**records[1], "record_schema_version": INGRESS_RECORD_SCHEMA_VERSION + 1}),
        (2, {**records[2], "record_type": "wrong_status_counter"}),
        (3, {**records[3], "record_schema_version": 1}),
        (
            4,
            {
                **records[4],
                "record_schema_version": OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION + 1,
            },
        ),
        (
            5,
            {
                **records[5],
                "record_schema_version": ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION + 1,
            },
        ),
        (6, {**records[6], "fencing_token": -1}),
        (
            6,
            {
                **records[6],
                "lease_owner": "worker-alpha",
                "lease_expiry": "2026-07-28T12:00:00.000000+09:00",
            },
        ),
    )

    for index, corrupted in corruptions:
        current = tuple(
            corrupted if position == index else item for position, item in enumerate(records)
        )
        _put_records(dynamodb_client, dynamodb_table, current)
        with pytest.raises(RepositoryConflict):
            await inspector.inspect(at=NOW)


@pytest.mark.asyncio
async def test_idle_stop_requires_marker_all_zero_counters_and_free_slots(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    records = _activity_records()
    _put_records(dynamodb_client, dynamodb_table, records)
    idle = _idle_state()
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(serialize_runtime_state(idle)),
    )
    repository = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    stop_at = idle.stop_eligible_at
    assert stop_at is not None

    for record in records:
        dynamodb_client.delete_item(
            TableName=dynamodb_table,
            Key=marshal_item(_key(record)),
        )
        with pytest.raises(RepositoryConflict, match="activity fence rejected"):
            await repository.begin_idle_stop(expected=idle, at=stop_at)
        assert await repository.get() == idle
        dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(record))

    nonzero_fields = (
        (records[1], "count"),
        (records[2], "count"),
        (records[3], "count"),
        (records[4], "pending_count"),
        (records[4], "claimed_count"),
        (records[5], "count"),
    )
    for record, field in nonzero_fields:
        dynamodb_client.put_item(
            TableName=dynamodb_table,
            Item=marshal_item({**record, field: 1}),
        )
        with pytest.raises(RepositoryConflict, match="activity fence rejected"):
            await repository.begin_idle_stop(expected=idle, at=stop_at)
        assert await repository.get() == idle
        dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(record))

    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(
            {
                "PK": "CONTROL#GLOBAL",
                "SK": "SLOT#0",
                "record_type": "lease_slot",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "slot": 0,
                "lease_owner": "runtime-alpha",
                "lease_expiry": _timestamp(stop_at + timedelta(minutes=1)),
                "fencing_token": 1,
            }
        ),
    )
    with pytest.raises(RepositoryConflict, match="activity fence rejected"):
        await repository.begin_idle_stop(expected=idle, at=stop_at)
    assert await repository.get() == idle
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(records[6]),
    )

    stopping = await repository.begin_idle_stop(expected=idle, at=stop_at)
    assert stopping.status is RuntimeStatus.STOPPING
    assert await repository.get() == stopping


@pytest.mark.asyncio
async def test_unneeded_start_stop_also_requires_zero_status_counter(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    records = _activity_records()
    _put_records(dynamodb_client, dynamodb_table, records)
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(serialize_runtime_state(starting)),
    )
    repository = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    status = records[2]
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item({**status, "count": 1}),
    )

    with pytest.raises(RepositoryConflict, match="activity fence rejected"):
        await repository.begin_unneeded_start_stop(
            expected=starting,
            at=NOW + timedelta(seconds=2),
        )
    assert await repository.get() == starting
