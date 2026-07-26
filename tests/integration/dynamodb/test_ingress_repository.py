"""DynamoDB Local coverage for the bounded ingress FIFO and transitions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    CURRENT_SCHEMA_VERSION,
    DynamoDbIngressRepository,
    ingress_request_sort_key,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application import (
    IngressRequest,
    IngressStatus,
    StatusMessageState,
)
from shittim_chest.application.ports import (
    RepositoryConflict,
    RepositoryQueueFull,
)
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)


def new_request(index: int, *, created_at: datetime | None = None) -> IngressRequest:
    at = created_at or NOW + timedelta(microseconds=index)
    interaction_id = f"interaction-{index:04d}"
    return IngressRequest.new_debate(
        interaction_id=interaction_id,
        operation_id=interaction_id,
        question=f"question-{index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=at,
    )


@pytest.mark.asyncio
async def test_enqueue_replay_fifo_claim_retry_accept_and_counter(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    later = new_request(2)
    earlier = new_request(1)

    assert (await repository.enqueue(later)).created
    first = await repository.enqueue(earlier)
    replay = await repository.enqueue(
        replace(
            earlier,
            operation_id="different-operation-id",
            question="different retry payload",
        )
    )

    assert first.created
    assert not replay.created
    assert replay.request == earlier
    assert await repository.active_count() == 2
    assert await repository.list_ready(at=NOW + timedelta(seconds=1)) == (earlier, later)

    claimed = await repository.claim(
        request=earlier,
        claim_owner="runtime-1",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.status is IngressStatus.CLAIMED
    assert claimed.delivery_attempt == 1
    assert (
        await repository.claim(
            request=earlier,
            claim_owner="runtime-2",
            at=NOW + timedelta(seconds=1),
        )
        is None
    )

    retried = await repository.reschedule(
        request=claimed,
        claim_owner="runtime-1",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=NOW + timedelta(seconds=5),
        error_code="slots_busy",
    )
    assert retried.status is IngressStatus.RETRYING
    assert retried.claim_owner is None
    assert retried not in await repository.list_ready(at=NOW + timedelta(seconds=4))
    assert (await repository.list_ready(at=NOW + timedelta(seconds=5)))[0] == retried

    reclaimed = await repository.claim(
        request=retried,
        claim_owner="runtime-2",
        at=NOW + timedelta(seconds=5),
    )
    assert reclaimed is not None
    assert reclaimed.delivery_attempt == 2
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    accepted = await repository.mark_accepted(
        request=reclaimed,
        claim_owner="runtime-2",
        at=NOW + timedelta(seconds=6),
        debate_id=debate_id,
        attempt_id=attempt_id,
    )

    assert accepted.status is IngressStatus.ACCEPTED
    assert accepted.accepted_debate_id == debate_id
    assert accepted.accepted_attempt_id == attempt_id
    assert await repository.active_count() == 1
    accepted_replay = await repository.mark_accepted(
        request=reclaimed,
        claim_owner="runtime-2",
        at=NOW + timedelta(seconds=7),
        debate_id=debate_id,
        attempt_id=attempt_id,
    )
    assert accepted_replay == accepted
    assert await repository.active_count() == 1
    operation = await repository.get_operation_result(accepted.interaction_id)
    assert operation is not None
    assert operation.status is IngressStatus.ACCEPTED
    assert operation.accepted_debate_id == debate_id


@pytest.mark.asyncio
async def test_queue_limit_is_atomic_and_terminal_transition_releases_capacity(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    requests = [new_request(index) for index in range(1, 21)]
    for request in requests:
        await repository.enqueue(request)

    with pytest.raises(RepositoryQueueFull):
        await repository.enqueue(new_request(21))
    assert await repository.active_count() == 20
    assert await repository.get_operation_result(new_request(21).interaction_id) is None

    rejected = await repository.mark_terminal(
        request=requests[0],
        at=NOW + timedelta(seconds=1),
        status=IngressStatus.REJECTED,
        error_code="request_not_allowed",
    )
    assert rejected.status is IngressStatus.REJECTED
    assert await repository.active_count() == 19
    assert (
        await repository.mark_terminal(
            request=requests[0],
            at=NOW + timedelta(seconds=2),
            status=IngressStatus.REJECTED,
            error_code="request_not_allowed",
        )
        == rejected
    )
    assert await repository.active_count() == 19
    assert (await repository.enqueue(new_request(21))).created
    assert await repository.active_count() == 20

    replay = await repository.enqueue(requests[1])
    assert not replay.created
    assert await repository.active_count() == 20


@pytest.mark.asyncio
async def test_concurrent_twentieth_slot_has_exactly_one_winner(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    for index in range(1, 20):
        await repository.enqueue(new_request(index))

    results = await asyncio.gather(
        repository.enqueue(new_request(20)),
        repository.enqueue(new_request(21)),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, RepositoryQueueFull) for result in results) == 1
    assert await repository.active_count() == 20


@pytest.mark.asyncio
async def test_concurrent_duplicate_creates_one_request_and_increments_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)

    first, second = await asyncio.gather(
        repository.enqueue(request),
        repository.enqueue(replace(request, operation_id="another-operation")),
    )

    assert sorted((first.created, second.created)) == [False, True]
    assert first.request == second.request
    assert first.request.operation_id in {request.operation_id, "another-operation"}
    assert first.operation == second.operation
    assert await repository.active_count() == 1


@pytest.mark.asyncio
async def test_malformed_counter_fails_closed_without_overwriting_it(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    counter = {
        "PK": "CONTROL#INGRESS",
        "SK": "COUNTER",
        "record_type": "unexpected_record",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": 1,
        "count": 0,
    }
    dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(counter))

    with pytest.raises(RepositoryConflict):
        await repository.enqueue(new_request(1))

    response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "COUNTER"}),
        ConsistentRead=True,
    )
    assert unmarshal_item(response["Item"]) == counter
    assert await repository.get_operation_result(new_request(1).interaction_id) is None

    negative_counter = {
        **counter,
        "record_type": "ingress_queue_counter",
        "count": -1,
    }
    dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(negative_counter))
    with pytest.raises(RepositoryConflict):
        await repository.enqueue(new_request(2))
    response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "COUNTER"}),
        ConsistentRead=True,
    )
    assert unmarshal_item(response["Item"]) == negative_counter


@pytest.mark.asyncio
async def test_ttl_is_not_used_for_queue_correctness(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(new_request(1), ttl=0)

    await repository.enqueue(request)

    assert await repository.active_count() == 1
    assert await repository.list_ready(at=NOW + timedelta(seconds=1)) == (request,)


@pytest.mark.asyncio
async def test_expired_claim_is_reclaimable_and_stale_owner_is_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await repository.enqueue(request)
    claimed = await repository.claim(
        request=request,
        claim_owner="runtime-old",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None

    before_expiry = NOW + timedelta(seconds=120)
    assert await repository.list_ready(at=before_expiry) == ()
    at_expiry = NOW + timedelta(seconds=121)
    ready = await repository.list_ready(at=at_expiry)
    assert ready == (claimed,)
    reclaimed = await repository.claim(
        request=ready[0],
        claim_owner="runtime-new",
        at=at_expiry,
    )
    assert reclaimed is not None
    assert reclaimed.claim_owner == "runtime-new"

    with pytest.raises(RepositoryConflict):
        await repository.reschedule(
            request=claimed,
            claim_owner="runtime-old",
            at=at_expiry + timedelta(seconds=1),
            next_attempt_at=at_expiry + timedelta(seconds=2),
            error_code="stale",
        )


@pytest.mark.asyncio
async def test_expired_claim_cannot_reschedule_or_accept(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await repository.enqueue(request)
    claimed = await repository.claim(
        request=request,
        claim_owner="runtime-old",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.claim_expires_at is not None
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()

    for at in (claimed.claim_expires_at, claimed.claim_expires_at + timedelta(seconds=1)):
        with pytest.raises(RepositoryConflict):
            await repository.reschedule(
                request=claimed,
                claim_owner="runtime-old",
                at=at,
                next_attempt_at=at + timedelta(seconds=1),
                error_code="slots_busy",
            )
        with pytest.raises(RepositoryConflict):
            await repository.mark_accepted(
                request=claimed,
                claim_owner="runtime-old",
                at=at,
                debate_id=debate_id,
                attempt_id=attempt_id,
            )

    forged = replace(
        claimed,
        claim_expires_at=claimed.claim_expires_at + timedelta(minutes=1),
    )
    with pytest.raises(RepositoryConflict):
        await repository.reschedule(
            request=forged,
            claim_owner="runtime-old",
            at=claimed.claim_expires_at,
            next_attempt_at=claimed.claim_expires_at + timedelta(seconds=1),
            error_code="slots_busy",
        )
    with pytest.raises(RepositoryConflict):
        await repository.mark_accepted(
            request=forged,
            claim_owner="runtime-old",
            at=claimed.claim_expires_at,
            debate_id=debate_id,
            attempt_id=attempt_id,
        )

    assert await repository.active_count() == 1
    operation = await repository.get_operation_result(request.interaction_id)
    assert operation is not None
    assert operation.status is IngressStatus.CLAIMED
    assert await repository.list_ready(at=claimed.claim_expires_at) == (claimed,)


@pytest.mark.asyncio
async def test_response_loss_replay_after_claim_expiry_returns_committed_transition(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    retry_request = new_request(1)
    accept_request = new_request(2)
    await repository.enqueue(retry_request)
    await repository.enqueue(accept_request)
    retry_claim = await repository.claim(
        request=retry_request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=1),
    )
    accept_claim = await repository.claim(
        request=accept_request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=1),
    )
    assert retry_claim is not None
    assert accept_claim is not None
    assert retry_claim.claim_expires_at is not None
    assert accept_claim.claim_expires_at is not None

    next_attempt_at = NOW + timedelta(seconds=10)
    retried = await repository.reschedule(
        request=retry_claim,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=next_attempt_at,
        error_code="slots_busy",
    )
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    accepted = await repository.mark_accepted(
        request=accept_claim,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=2),
        debate_id=debate_id,
        attempt_id=attempt_id,
    )
    assert await repository.active_count() == 1

    replay_at = max(retry_claim.claim_expires_at, accept_claim.claim_expires_at) + timedelta(
        seconds=1
    )
    assert (
        await repository.reschedule(
            request=retry_claim,
            claim_owner="runtime",
            at=replay_at,
            next_attempt_at=next_attempt_at,
            error_code="slots_busy",
        )
        == retried
    )
    assert (
        await repository.mark_accepted(
            request=accept_claim,
            claim_owner="runtime",
            at=replay_at,
            debate_id=debate_id,
            attempt_id=attempt_id,
        )
        == accepted
    )
    assert await repository.active_count() == 1


@pytest.mark.asyncio
async def test_transition_conditions_bind_immutable_request_and_operation_identity(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await repository.enqueue(request)
    request_key = marshal_item({"PK": "CONTROL#INGRESS", "SK": ingress_request_sort_key(request)})
    operation_key = marshal_item(
        {"PK": f"INGRESS_OPERATION#{request.interaction_id}", "SK": "RESULT"}
    )

    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=request_key,
        UpdateExpression="SET operation_id=:corrupt",
        ExpressionAttributeValues=marshal_item({":corrupt": "corrupt-operation"}),
    )
    assert (
        await repository.claim(
            request=request,
            claim_owner="runtime",
            at=NOW + timedelta(seconds=1),
        )
        is None
    )
    raw_request = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=request_key,
        ConsistentRead=True,
    )
    assert unmarshal_item(raw_request["Item"])["operation_id"] == "corrupt-operation"

    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=request_key,
        UpdateExpression="SET operation_id=:expected",
        ExpressionAttributeValues=marshal_item({":expected": request.operation_id}),
    )
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=operation_key,
        UpdateExpression="SET request_sort_key=:corrupt",
        ExpressionAttributeValues=marshal_item({":corrupt": "REQUEST#corrupt"}),
    )
    assert (
        await repository.claim(
            request=request,
            claim_owner="runtime",
            at=NOW + timedelta(seconds=1),
        )
        is None
    )
    raw_request = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=request_key,
        ConsistentRead=True,
    )
    raw_operation = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=operation_key,
        ConsistentRead=True,
    )
    assert unmarshal_item(raw_request["Item"])["status"] == IngressStatus.PENDING.value
    assert unmarshal_item(raw_operation["Item"])["request_sort_key"] == "REQUEST#corrupt"
    assert await repository.active_count() == 1


@pytest.mark.asyncio
async def test_startup_timeout_is_nonterminal_and_fifteen_minutes_is_terminal(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)

    assert await repository.list_startup_deadlines(at=NOW + timedelta(minutes=2, seconds=59)) == ()
    assert (
        await repository.list_terminal_deadlines(at=NOW + timedelta(minutes=14, seconds=59)) == ()
    )
    assert await repository.list_startup_deadlines(at=NOW + timedelta(minutes=3)) == (request,)

    timed_out = await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    assert timed_out.status is IngressStatus.PENDING
    assert timed_out.status_message_state is StatusMessageState.STARTUP_TIMEOUT
    assert (
        await repository.mark_startup_timeout(
            request=request,
            at=NOW + timedelta(minutes=3, seconds=1),
        )
        == timed_out
    )
    with_message = await repository.update_status_message(
        request=timed_out,
        state=StatusMessageState.STARTUP_TIMEOUT,
        message_id="status-message-id",
        at=NOW + timedelta(minutes=3, seconds=2),
    )
    assert (
        await repository.update_status_message(
            request=timed_out,
            state=StatusMessageState.STARTUP_TIMEOUT,
            message_id="status-message-id",
            at=NOW + timedelta(minutes=3, seconds=3),
        )
        == with_message
    )
    desired_at = NOW + timedelta(minutes=3, seconds=4)
    desired_timestamp = desired_at.isoformat().replace("+00:00", "Z")
    for key in (
        {"PK": "CONTROL#INGRESS", "SK": ingress_request_sort_key(request)},
        {"PK": f"INGRESS_OPERATION#{request.interaction_id}", "SK": "RESULT"},
    ):
        dynamodb_client.update_item(
            TableName=dynamodb_table,
            Key=marshal_item(key),
            UpdateExpression="SET updated_at=:updated",
            ExpressionAttributeValues=marshal_item({":updated": desired_timestamp}),
        )
    desired = replace(with_message, updated_at=desired_at)
    delivered_again = await repository.update_status_message(
        request=desired,
        state=StatusMessageState.STARTUP_TIMEOUT,
        message_id="status-message-id",
        at=NOW + timedelta(minutes=3, seconds=5),
    )
    assert delivered_again.status_message_updated_at == NOW + timedelta(minutes=3, seconds=5)
    assert (
        await repository.update_status_message(
            request=desired,
            state=StatusMessageState.STARTUP_TIMEOUT,
            message_id="status-message-id",
            at=NOW + timedelta(minutes=3, seconds=6),
        )
        == delivered_again
    )
    assert await repository.active_count() == 1
    assert await repository.list_startup_deadlines(at=NOW + timedelta(minutes=4)) == ()
    assert await repository.list_terminal_deadlines(at=NOW + timedelta(minutes=15)) == (
        delivered_again,
    )
    assert (
        await repository.claim(
            request=delivered_again,
            claim_owner="runtime",
            at=NOW + timedelta(minutes=15),
        )
        is None
    )

    failed = await repository.mark_terminal(
        request=delivered_again,
        at=NOW + timedelta(minutes=15),
        status=IngressStatus.FAILED,
        error_code="startup_terminal_deadline_exceeded",
    )
    assert failed.status is IngressStatus.FAILED
    assert failed.completed_at == NOW + timedelta(minutes=15)
    assert await repository.active_count() == 0
    assert await repository.list_ready(at=NOW + timedelta(minutes=16)) == ()
