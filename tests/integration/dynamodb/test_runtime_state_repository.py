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
    PersistenceFormatError,
    ingress_request_sort_key,
    serialize_runtime_state,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application import IngressRequest, RuntimeStatus
from shittim_chest.application.ports import RepositoryConflict

NOW = datetime(2026, 7, 26, 6, 0, tzinfo=UTC)


def new_request(index: int, *, created_at: datetime | None = None) -> IngressRequest:
    interaction_id = f"interaction-{index:04d}"
    return IngressRequest.new_debate(
        interaction_id=interaction_id,
        operation_id=f"operation-{index:04d}",
        question=f"question-{index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=created_at or NOW + timedelta(microseconds=index),
    )


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
    stopping = idle.transition(
        RuntimeStatus.STOPPING,
        at=idle.stop_eligible_at or NOW,
    )
    await runtime.replace(expected=idle, updated=stopping)

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
    assert await runtime.get() is None
    state = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=1),
    )

    malformed = {**serialize_runtime_state(state), "generation": -1}
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(malformed),
    )
    with pytest.raises(PersistenceFormatError, match="invalid runtime state"):
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

    with pytest.raises(RepositoryConflict, match="missing runtime state"):
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
    assert await runtime.get() is None
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
    assert await runtime.get() is None


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
