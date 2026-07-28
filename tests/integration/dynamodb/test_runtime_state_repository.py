"""DynamoDB Local races for the generation-fenced runtime state record."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Unpack

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import (
    TransactWriteItemsInputTypeDef,
    TransactWriteItemsOutputTypeDef,
)

from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    DynamoDbRuntimeStateRepository,
    ingress_request_sort_key,
    serialize_runtime_state,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.control_records import CONTROL_RECORD_MANIFEST
from shittim_chest.adapters.dynamodb.outbox import OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.runtime_state import (
    RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
    RUNTIME_ACTIVITY_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.serializer import CURRENT_SCHEMA_VERSION
from shittim_chest.application import (
    IngressRequest,
    IngressStatus,
    RuntimeState,
    RuntimeStatus,
)
from shittim_chest.application.ports import RepositoryConflict

NOW = datetime(2026, 7, 26, 6, 0, tzinfo=UTC)
INITIAL_RUNTIME = RuntimeState.stopped(at=CONTROL_RECORD_MANIFEST.initial_runtime_at)


def new_request(index: int, *, created_at: datetime | None = None) -> IngressRequest:
    interaction_id = f"interaction-{index:04d}"
    return IngressRequest.new_debate(
        interaction_id=interaction_id,
        operation_id=f"operation-{index:04d}",
        application_id="application-id",
        question=f"question-{index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=created_at or NOW + timedelta(microseconds=index),
    )


def force_stopped_runtime(
    client: DynamoDBClient,
    table_name: str,
    *,
    previous: RuntimeState,
    at: datetime,
) -> RuntimeState:
    """Persist a valid stopped observation produced outside the runtime task."""

    stopped = RuntimeState(
        status=RuntimeStatus.STOPPED,
        generation=previous.generation,
        desired_count=0,
        version=previous.version + 1,
        updated_at=at,
        stopped_at=at,
    )
    client.put_item(
        TableName=table_name,
        Item=marshal_item(serialize_runtime_state(stopped)),
    )
    return stopped


def wake_marker(client: DynamoDBClient, table_name: str, interaction_id: str) -> object:
    response = client.get_item(
        TableName=table_name,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{interaction_id}",
                "SK": "RUNTIME_WAKE",
            }
        ),
        ConsistentRead=True,
    )
    return response.get("Item")


def put_zero_non_ingress_activity_records(
    client: DynamoDBClient,
    table_name: str,
    *,
    at: datetime,
) -> None:
    """Seed deployment-owned zero records while ingress exercises its own counters."""

    timestamp = at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    common = {"created_at": timestamp, "updated_at": timestamp}
    records = (
        {
            "PK": "CONTROL#RUNTIME",
            "SK": "ACTIVITY_SCHEMA",
            "record_type": RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": RUNTIME_ACTIVITY_SCHEMA_VERSION,
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
    )
    for record in records:
        client.put_item(TableName=table_name, Item=marshal_item(record))


@pytest.mark.asyncio
async def test_ensure_wake_creates_one_marker_from_the_preseeded_runtime_state(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)

    first = await runtime.ensure_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    marker_before = wake_marker(dynamodb_client, dynamodb_table, request.interaction_id)
    replay = await runtime.ensure_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=2),
    )

    assert first.status is RuntimeStatus.STARTING
    assert replay == first
    assert marker_before is not None
    assert wake_marker(dynamodb_client, dynamodb_table, request.interaction_id) == marker_before


@pytest.mark.asyncio
async def test_ensure_wake_restarts_stopped_runtime_without_replacing_marker(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    first = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    marker_before = wake_marker(dynamodb_client, dynamodb_table, request.interaction_id)
    force_stopped_runtime(
        dynamodb_client,
        dynamodb_table,
        previous=first,
        at=NOW + timedelta(seconds=2),
    )

    restarted = await runtime.ensure_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=3),
    )

    assert restarted.status is RuntimeStatus.STARTING
    assert restarted.desired_count == 1
    assert restarted.generation == first.generation + 1
    assert restarted.version == first.version + 2
    assert wake_marker(dynamodb_client, dynamodb_table, request.interaction_id) == marker_before


@pytest.mark.asyncio
async def test_ensure_wake_fails_closed_when_active_pointer_is_missing(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS#ACTIVE",
                "SK": ingress_request_sort_key(request),
            }
        ),
    )

    with pytest.raises(RepositoryConflict, match="active ingress pointer"):
        await runtime.ensure_wake(
            interaction_id=request.interaction_id,
            at=NOW + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_concurrent_ensure_wake_restarts_one_generation_exactly_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    first = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    force_stopped_runtime(
        dynamodb_client,
        dynamodb_table,
        previous=first,
        at=NOW + timedelta(seconds=2),
    )

    results = await asyncio.gather(
        *(
            runtime.ensure_wake(
                interaction_id=request.interaction_id,
                at=NOW + timedelta(seconds=3),
            )
            for _ in range(8)
        )
    )

    assert all(result.generation == first.generation + 1 for result in results)
    current = await runtime.get()
    assert current is not None
    assert current.generation == first.generation + 1


@pytest.mark.asyncio
async def test_ensure_wake_repairs_postdeadline_only_with_predeadline_processing_marker(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    first = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    claimed = await ingress.claim(
        request=request,
        claim_owner="runtime-alpha",
        at=NOW + timedelta(seconds=2),
    )
    assert claimed is not None
    processing_started_at = NOW + timedelta(seconds=3)
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS",
                "SK": ingress_request_sort_key(claimed),
            }
        ),
        UpdateExpression="SET processing_started_at=:processing_started_at",
        ExpressionAttributeValues=marshal_item(
            {
                ":processing_started_at": processing_started_at.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z")
            }
        ),
    )
    force_stopped_runtime(
        dynamodb_client,
        dynamodb_table,
        previous=first,
        at=request.terminal_deadline_at + timedelta(seconds=1),
    )

    restarted = await runtime.ensure_wake(
        interaction_id=request.interaction_id,
        at=request.terminal_deadline_at + timedelta(seconds=2),
    )

    assert restarted.status is RuntimeStatus.STARTING
    assert restarted.generation == first.generation + 1


@pytest.mark.asyncio
async def test_ensure_wake_rejects_postdeadline_unstarted_request(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    first = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    stopped = force_stopped_runtime(
        dynamodb_client,
        dynamodb_table,
        previous=first,
        at=request.terminal_deadline_at + timedelta(seconds=1),
    )

    with pytest.raises(RepositoryConflict, match="terminal deadline"):
        await runtime.ensure_wake(
            interaction_id=request.interaction_id,
            at=request.terminal_deadline_at + timedelta(seconds=2),
        )
    assert await runtime.get() == stopped


@pytest.mark.asyncio
async def test_wake_is_immutable_per_interaction_and_generation_is_monotonic(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    first_request = new_request(1)
    second_request = new_request(2)
    await ingress.enqueue(first_request)
    await ingress.enqueue(second_request)

    first = await runtime.request_wake(
        interaction_id=first_request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    replay = await runtime.request_wake(
        interaction_id=first_request.interaction_id,
        at=NOW + timedelta(seconds=2),
    )
    second = await runtime.request_wake(
        interaction_id=second_request.interaction_id,
        at=NOW + timedelta(seconds=3),
    )

    assert first.status is RuntimeStatus.STARTING
    assert first.generation == 1
    assert replay.generation == 1
    assert second.generation == 2
    assert second.wake_started_at == first.wake_started_at
    assert await runtime.get() == second


@pytest.mark.asyncio
async def test_concurrent_same_and_distinct_wakes_increment_exactly_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    requests = [new_request(index) for index in range(1, 9)]
    for request in requests:
        await ingress.enqueue(request)

    same_results = await asyncio.gather(
        *(
            runtime.request_wake(
                interaction_id=requests[0].interaction_id,
                at=NOW + timedelta(seconds=1),
            )
            for _ in range(8)
        )
    )
    assert all(result.generation == 1 for result in same_results)

    await asyncio.gather(
        *(
            runtime.request_wake(
                interaction_id=request.interaction_id,
                at=NOW + timedelta(seconds=index + 2),
            )
            for index, request in enumerate(requests[1:])
        )
    )
    current = await runtime.get()
    assert current is not None
    assert current.generation == len(requests)
    assert current.version == len(requests)


@pytest.mark.asyncio
async def test_state_replacement_is_version_and_generation_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    starting = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    assert await runtime.replace(expected=starting, updated=started) == started
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )
    assert await runtime.replace(expected=started, updated=ready) == ready
    idle = ready.begin_idle(at=NOW + timedelta(seconds=4))
    assert await runtime.replace(expected=ready, updated=idle) == idle
    assert idle.begin_idle(at=NOW + timedelta(seconds=5)) is idle
    assert await runtime.replace(expected=idle, updated=idle) is idle

    stale_ready = started.transition(
        RuntimeStatus.BUSY,
        at=NOW + timedelta(seconds=6),
        runtime_instance_id="runtime-alpha",
    )
    with pytest.raises(RepositoryConflict, match="changed before replacement"):
        await runtime.replace(expected=started, updated=stale_ready)

    second_request = new_request(2)
    await ingress.enqueue(second_request)
    await runtime.request_wake(
        interaction_id=second_request.interaction_id,
        at=NOW + timedelta(seconds=7),
    )
    with pytest.raises(RepositoryConflict, match="no-op replacement"):
        await runtime.replace(expected=idle, updated=idle)


@pytest.mark.asyncio
async def test_new_wake_fences_stale_stop_completion(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    first_request = new_request(1)
    second_request = new_request(2, created_at=NOW + timedelta(minutes=31))
    await ingress.enqueue(first_request)
    starting = await runtime.request_wake(
        interaction_id=first_request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    await runtime.replace(expected=starting, updated=started)
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )
    await runtime.replace(expected=started, updated=ready)
    idle = ready.begin_idle(at=NOW + timedelta(seconds=4))
    await runtime.replace(expected=ready, updated=idle)
    terminal_request = await ingress.mark_terminal(
        request=first_request,
        at=NOW + timedelta(seconds=5),
        status=IngressStatus.FAILED,
        error_code="test_request_finished",
    )
    status_work = await ingress.claim_status_publication(
        interaction_id=terminal_request.interaction_id,
        claim_owner="status-publisher",
        at=NOW + timedelta(seconds=6),
    )
    assert status_work is not None
    await ingress.mark_status_delivered(
        work=status_work,
        claim_owner="status-publisher",
        message_id="status-message",
        at=NOW + timedelta(seconds=7),
    )
    put_zero_non_ingress_activity_records(
        dynamodb_client,
        dynamodb_table,
        at=NOW + timedelta(seconds=7),
    )
    stop_at = idle.stop_eligible_at
    assert stop_at is not None
    stopping = await runtime.begin_idle_stop(expected=idle, at=stop_at)

    await ingress.enqueue(second_request)
    rewoken = await runtime.request_wake(
        interaction_id=second_request.interaction_id,
        at=NOW + timedelta(minutes=31),
    )
    assert rewoken.status is RuntimeStatus.STARTING
    assert rewoken.generation == stopping.generation + 1

    stale_stopped = stopping.transition(
        RuntimeStatus.STOPPED,
        at=NOW + timedelta(minutes=32),
    )
    with pytest.raises(RepositoryConflict, match="changed before replacement"):
        await runtime.replace(expected=stopping, updated=stale_stopped)


@pytest.mark.asyncio
async def test_missing_or_malformed_runtime_state_fails_closed(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    assert await runtime.get() == INITIAL_RUNTIME
    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#RUNTIME", "SK": "STATE"}),
    )
    with pytest.raises(RepositoryConflict, match="runtime state record is missing"):
        await runtime.get()
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(serialize_runtime_state(INITIAL_RUNTIME)),
    )
    state = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )

    malformed = {**serialize_runtime_state(state), "generation": -1}
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(malformed),
    )
    with pytest.raises(RepositoryConflict, match="runtime state record is invalid"):
        await runtime.get()


@pytest.mark.asyncio
async def test_wake_marker_without_runtime_state_is_corruption(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#RUNTIME", "SK": "STATE"}),
    )

    with pytest.raises(RepositoryConflict, match="runtime state record is missing"):
        await runtime.request_wake(
            interaction_id=request.interaction_id,
            at=NOW + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_terminal_transition_winning_before_transaction_blocks_wake(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    original = dynamodb_client.transact_write_items
    raced = False

    def transact_after_terminal_transition(
        **kwargs: Unpack[TransactWriteItemsInputTypeDef],
    ) -> TransactWriteItemsOutputTypeDef:
        nonlocal raced
        if not raced:
            raced = True
            dynamodb_client.update_item(
                TableName=dynamodb_table,
                Key=marshal_item(
                    {
                        "PK": f"INGRESS_OPERATION#{request.interaction_id}",
                        "SK": "RESULT",
                    }
                ),
                UpdateExpression="SET #status=:failed, updated_at=:at",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=marshal_item(
                    {
                        ":failed": "failed",
                        ":at": (NOW + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )
        return original(**kwargs)

    monkeypatch.setattr(
        dynamodb_client,
        "transact_write_items",
        transact_after_terminal_transition,
    )

    with pytest.raises(RepositoryConflict, match="terminal ingress operation"):
        await runtime.request_wake(
            interaction_id=request.interaction_id,
            at=NOW + timedelta(seconds=1),
        )
    assert raced
    assert await runtime.get() == INITIAL_RUNTIME
    marker = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{request.interaction_id}",
                "SK": "RUNTIME_WAKE",
            }
        ),
        ConsistentRead=True,
    )
    assert "Item" not in marker


@pytest.mark.asyncio
async def test_same_version_logical_corruption_is_not_overwritten(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    starting = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    corrupted_time = NOW + timedelta(seconds=30)
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#RUNTIME", "SK": "STATE"}),
        UpdateExpression="SET last_request_at=:corrupted",
        ExpressionAttributeValues=marshal_item(
            {":corrupted": corrupted_time.isoformat().replace("+00:00", "Z")}
        ),
    )

    with pytest.raises(RepositoryConflict, match="changed before replacement"):
        await runtime.replace(expected=starting, updated=started)
    current = await runtime.get()
    assert current is not None
    assert current.version == starting.version
    assert current.last_request_at == corrupted_time


@pytest.mark.asyncio
async def test_request_and_operation_identity_mismatch_fails_closed(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await ingress.enqueue(request)
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS",
                "SK": ingress_request_sort_key(request),
            }
        ),
        UpdateExpression="SET operation_id=:other",
        ExpressionAttributeValues=marshal_item({":other": "operation-other"}),
    )

    with pytest.raises(RepositoryConflict, match="do not match"):
        await runtime.request_wake(
            interaction_id=request.interaction_id,
            at=NOW + timedelta(seconds=1),
        )
    assert await runtime.get() == INITIAL_RUNTIME


@pytest.mark.asyncio
async def test_terminal_deadline_fixed_width_boundary_is_atomic(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)
    before_boundary = new_request(0)
    at_boundary = IngressRequest.new_debate(
        interaction_id="interaction-boundary",
        operation_id="operation-boundary",
        application_id="application-id",
        question="boundary question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW + timedelta(seconds=1),
    )
    await ingress.enqueue(before_boundary)
    await ingress.enqueue(at_boundary)

    accepted = await runtime.request_wake(
        interaction_id=before_boundary.interaction_id,
        at=before_boundary.terminal_deadline_at - timedelta(microseconds=500_000),
    )
    assert accepted.generation == 1

    with pytest.raises(RepositoryConflict, match="terminal deadline"):
        await runtime.request_wake(
            interaction_id=at_boundary.interaction_id,
            at=at_boundary.terminal_deadline_at,
        )
    current = await runtime.get()
    assert current == accepted
    marker = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{at_boundary.interaction_id}",
                "SK": "RUNTIME_WAKE",
            }
        ),
        ConsistentRead=True,
    )
    assert "Item" not in marker
