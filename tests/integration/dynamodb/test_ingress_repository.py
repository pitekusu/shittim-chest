"""DynamoDB Local coverage for the bounded ingress FIFO and transitions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Unpack

import pytest
from botocore.exceptions import ReadTimeoutError
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import (
    TransactWriteItemsInputTypeDef,
    TransactWriteItemsOutputTypeDef,
)

from shittim_chest.adapters.dynamodb import (
    CURRENT_SCHEMA_VERSION,
    DynamoDbIngressRepository,
    ingress_request_sort_key,
    serialize_ingress_request,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application import (
    IngressKind,
    IngressRequest,
    IngressStatus,
    StatusMessageState,
)
from shittim_chest.application.ports import (
    RepositoryConflict,
    RepositoryQueueFull,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    StatusHistoryCheckpoint,
    StatusPublicationState,
)
from shittim_chest.application.status_publication import status_content_hash
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)
TARGET_DEBATE_ID = DebateId.new()
EXPECTED_ATTEMPT_ID = AttemptId.new()


def new_request(index: int, *, created_at: datetime | None = None) -> IngressRequest:
    at = created_at or NOW + timedelta(microseconds=index)
    interaction_id = f"interaction-{index:04d}"
    return IngressRequest.new_debate(
        interaction_id=interaction_id,
        operation_id=interaction_id,
        application_id="application-id",
        question=f"question-{index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=at,
    )


def control_request(
    index: int,
    *,
    operation_id: str = "retry-operation",
    requester_id: str = "requester-id",
) -> IngressRequest:
    return IngressRequest.control_operation(
        interaction_id=f"component-interaction-{index:04d}",
        operation_id=operation_id,
        kind=IngressKind.RETRY,
        application_id="application-id",
        requester_id=requester_id,
        requester_username="requester",
        requester_display_name="Requester",
        requester_can_manage_messages=False,
        guild_id="guild-id",
        channel_id="thread-id",
        parent_channel_id="channel-id",
        source_message_id="panel-message-id",
        source_thread_id="thread-id",
        target_debate_id=TARGET_DEBATE_ID,
        expected_attempt_id=EXPECTED_ATTEMPT_ID,
        custom_id=f"shittim:retry:{TARGET_DEBATE_ID}:{EXPECTED_ATTEMPT_ID}",
        created_at=NOW + timedelta(microseconds=index),
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
    with pytest.raises(RepositoryConflict, match="immutable identity"):
        await repository.enqueue(
            replace(
                earlier,
                operation_id="different-operation-id",
                question="different retry payload",
            )
        )
    replay = await repository.enqueue(earlier)

    assert first.created
    assert not replay.created
    assert replay.request == earlier
    assert await repository.active_count() == 2
    assert await repository.list_ready(at=NOW + timedelta(seconds=1)) == (earlier, later)
    pointers = dynamodb_client.query(
        TableName=dynamodb_table,
        KeyConditionExpression="PK=:pk",
        ExpressionAttributeValues=marshal_item({":pk": "CONTROL#INGRESS#ACTIVE"}),
        ConsistentRead=True,
        ScanIndexForward=True,
    )
    pointer_items = [unmarshal_item(item) for item in pointers["Items"]]
    assert [item["SK"] for item in pointer_items] == [
        ingress_request_sort_key(earlier),
        ingress_request_sort_key(later),
    ]
    assert all("question" not in item and "requester_id" not in item for item in pointer_items)

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
    assert await repository.list_ready(at=NOW + timedelta(seconds=4)) == ()
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
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS#ACTIVE",
                "SK": ingress_request_sort_key(earlier),
            }
        ),
        ConsistentRead=True,
    )
    accepted_publication = await repository.get_status_publication(accepted.interaction_id)
    assert accepted_publication is not None
    assert accepted_publication.desired_state is StatusMessageState.ACCEPTED
    assert accepted_publication.state is StatusPublicationState.PREPARED
    assert await repository.pending_status_count() == 2
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
    durable_replay = await repository.get_replay(earlier)
    assert durable_replay is not None
    assert durable_replay.request == accepted


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
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS#ACTIVE",
                "SK": ingress_request_sort_key(requests[0]),
            }
        ),
        ConsistentRead=True,
    )
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
async def test_active_pointer_corruption_fails_closed(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)
    await repository.enqueue(request)
    accepted = replace(
        request,
        status=IngressStatus.ACCEPTED,
        status_message_state=StatusMessageState.ACCEPTED,
        updated_at=NOW + timedelta(seconds=1),
        accepted_debate_id=DebateId.new(),
        accepted_attempt_id=AttemptId.new(),
    )
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(serialize_ingress_request(accepted)),
    )

    with pytest.raises(RepositoryConflict, match="retains an active pointer"):
        await repository.list_ready(at=NOW + timedelta(seconds=2))

    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS",
                "SK": ingress_request_sort_key(request),
            }
        ),
    )
    with pytest.raises(RepositoryConflict, match="missing request"):
        await repository.list_ready(at=NOW + timedelta(seconds=2))


@pytest.mark.asyncio
async def test_claim_terminal_tolerates_status_metadata_updates_but_fences_generation(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim(
        request=request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    request_key = {
        "PK": "CONTROL#INGRESS",
        "SK": ingress_request_sort_key(claimed),
    }
    metadata_at = NOW + timedelta(seconds=2)
    metadata_timestamp = metadata_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(request_key),
        UpdateExpression=(
            "SET updated_at=:at, status_message_id=:message, status_message_updated_at=:at"
        ),
        ExpressionAttributeValues=marshal_item({":at": metadata_timestamp, ":message": "500"}),
    )
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": f"INGRESS_OPERATION#{claimed.interaction_id}", "SK": "RESULT"}),
        UpdateExpression="SET updated_at=:at",
        ExpressionAttributeValues=marshal_item({":at": metadata_timestamp}),
    )

    rejected = await repository.mark_claim_terminal(
        request=claimed,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=3),
        status=IngressStatus.REJECTED,
        error_code="request_not_allowed",
    )

    assert rejected.status is IngressStatus.REJECTED
    assert rejected.status_message_id == "500"
    assert rejected.status_message_updated_at == metadata_at
    assert await repository.active_count() == 0
    assert "Item" not in dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": "CONTROL#INGRESS#ACTIVE",
                "SK": ingress_request_sort_key(request),
            }
        ),
        ConsistentRead=True,
    )

    second = new_request(2, created_at=NOW + timedelta(seconds=4))
    await repository.enqueue(second)
    first_claim = await repository.claim(
        request=second,
        claim_owner="old-runtime",
        at=NOW + timedelta(seconds=5),
    )
    assert first_claim is not None and first_claim.claim_expires_at is not None
    replacement_claim = await repository.claim(
        request=first_claim,
        claim_owner="new-runtime",
        at=first_claim.claim_expires_at,
    )
    assert replacement_claim is not None
    with pytest.raises(RepositoryConflict, match="exact live ingress claimant"):
        await repository.mark_claim_terminal(
            request=first_claim,
            claim_owner="old-runtime",
            at=first_claim.claim_expires_at,
            status=IngressStatus.FAILED,
            error_code="application_failure",
        )
    assert await repository.active_count() == 1


@pytest.mark.asyncio
async def test_claim_settlements_use_generation_not_status_publication_revision(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    retry_request = new_request(1, created_at=NOW)
    await repository.enqueue(retry_request)
    retry_claim = await repository.claim(
        request=retry_request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=1),
    )
    assert retry_claim is not None
    await repository.request_status_publication(
        request=retry_claim,
        state=StatusMessageState.READY,
        at=NOW + timedelta(seconds=2),
    )

    rescheduled = await repository.reschedule(
        request=retry_claim,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=3),
        next_attempt_at=NOW + timedelta(seconds=10),
        error_code="slot_busy",
    )

    assert rescheduled.status is IngressStatus.RETRYING
    assert rescheduled.status_message_state is StatusMessageState.READY

    accept_request = new_request(2, created_at=NOW + timedelta(seconds=4))
    await repository.enqueue(accept_request)
    accept_claim = await repository.claim(
        request=accept_request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=5),
    )
    assert accept_claim is not None
    await repository.request_status_publication(
        request=accept_claim,
        state=StatusMessageState.READY,
        at=NOW + timedelta(seconds=6),
    )

    accepted = await repository.mark_accepted(
        request=accept_claim,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=7),
        debate_id=DebateId.new(),
        attempt_id=AttemptId.new(),
    )

    assert accepted.status is IngressStatus.ACCEPTED
    assert accepted.status_message_state is StatusMessageState.ACCEPTED


@pytest.mark.asyncio
async def test_terminal_replay_returns_latest_status_message_metadata_without_counter_drift(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim(
        request=request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    accepted = await repository.mark_accepted(
        request=claimed,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=2),
        debate_id=DebateId.new(),
        attempt_id=AttemptId.new(),
    )
    completed = await repository.mark_terminal(
        request=accepted,
        at=NOW + timedelta(seconds=3),
        status=IngressStatus.COMPLETED,
        error_code=None,
    )
    publication = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=4),
    )
    assert publication is not None
    await repository.mark_status_delivered(
        work=publication,
        claim_owner="publisher",
        message_id="500",
        at=NOW + timedelta(seconds=5),
    )

    replay = await repository.mark_terminal(
        request=completed,
        at=NOW + timedelta(seconds=6),
        status=IngressStatus.COMPLETED,
        error_code=None,
    )

    assert replay.status is IngressStatus.COMPLETED
    assert replay.status_message_id == "500"
    assert replay.status_message_updated_at == NOW + timedelta(seconds=5)
    assert await repository.active_count() == 0
    assert await repository.pending_status_count() == 0


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
    first_request = control_request(1)
    second_request = control_request(2)

    first, second = await asyncio.gather(
        repository.enqueue(first_request),
        repository.enqueue(second_request),
    )

    assert sorted((first.created, second.created)) == [False, True]
    assert first.request == second.request
    assert first.request.interaction_id in {
        first_request.interaction_id,
        second_request.interaction_id,
    }
    assert first.operation == second.operation
    assert await repository.active_count() == 1
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_semantic_replay_precedes_full_queue_check_and_rejects_changed_identity(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    canonical = control_request(1)
    await repository.enqueue(canonical)
    for index in range(2, 21):
        await repository.enqueue(new_request(index))

    replay = await repository.enqueue(control_request(21))
    direct_replay = await repository.get_replay(control_request(23))

    assert not replay.created
    assert replay.request == canonical
    assert direct_replay is not None
    assert direct_replay.request == canonical
    assert await repository.active_count() == 20
    assert await repository.pending_status_count() == 20
    with pytest.raises(RepositoryConflict, match="immutable identity"):
        await repository.enqueue(control_request(22, requester_id="another-requester"))
    with pytest.raises(RepositoryQueueFull):
        await repository.enqueue(new_request(21))


@pytest.mark.asyncio
async def test_enqueue_prepares_independent_status_publication_without_token(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1)

    result = await repository.enqueue(request)
    publication = await repository.get_status_publication(result.request.interaction_id)

    assert publication is not None
    assert publication.state is StatusPublicationState.PREPARED
    assert publication.desired_state is StatusMessageState.STARTING
    assert publication.delivered_state is None
    assert publication.status_channel_id == request.status_channel_id
    assert publication.request_sort_key == ingress_request_sort_key(request)
    assert len(publication.nonce) == 22
    assert publication.next_attempt_at == request.created_at
    assert await repository.pending_status_count() == 1
    raw = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{request.interaction_id}",
                "SK": "STATUS_PUBLICATION",
            }
        ),
        ConsistentRead=True,
    )
    persisted = unmarshal_item(raw["Item"])
    assert "token" not in persisted
    assert persisted["gsi1pk"] == "INGRESS#STATUS_DUE"
    assert persisted["desired_state"] == StatusMessageState.STARTING.value
    assert "delivered_state" not in persisted


@pytest.mark.asyncio
async def test_status_claim_uses_the_versioned_persisted_content_as_authoritative(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    legacy_content = "以前のrendererで永続化された状態本文"
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{request.interaction_id}",
                "SK": "STATUS_PUBLICATION",
            }
        ),
        UpdateExpression="SET content=:content, content_hash=:content_hash",
        ExpressionAttributeValues=marshal_item(
            {
                ":content": legacy_content,
                ":content_hash": status_content_hash(legacy_content),
            }
        ),
    )

    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )

    assert claimed is not None
    assert claimed.publication.content == legacy_content
    assert claimed.publication.content_hash == status_content_hash(legacy_content)


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
async def test_malformed_status_counter_rolls_back_the_entire_enqueue(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    counter = {
        "PK": "CONTROL#INGRESS",
        "SK": "STATUS_PENDING_COUNTER",
        "record_type": "unexpected_record",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": 1,
        "count": 0,
    }
    dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(counter))
    request = new_request(1)

    with pytest.raises(RepositoryUnavailable):
        await repository.enqueue(request)

    assert await repository.active_count() == 0
    assert await repository.get_operation_result(request.interaction_id) is None
    assert await repository.get_status_publication(request.interaction_id) is None
    response = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"}),
        ConsistentRead=True,
    )
    assert unmarshal_item(response["Item"]) == counter


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
async def test_replay_fails_closed_when_status_publication_is_missing_or_rebound(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    missing = new_request(1)
    rebound = control_request(2)
    await repository.enqueue(missing)
    await repository.enqueue(rebound)

    dynamodb_client.delete_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{missing.interaction_id}",
                "SK": "STATUS_PUBLICATION",
            }
        ),
    )
    with pytest.raises(RepositoryConflict, match="no status publication"):
        await repository.enqueue(missing)

    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{rebound.interaction_id}",
                "SK": "STATUS_PUBLICATION",
            }
        ),
        UpdateExpression="SET status_channel_id=:other",
        ExpressionAttributeValues=marshal_item({":other": "other-channel"}),
    )
    with pytest.raises(RepositoryConflict, match="another request"):
        await repository.enqueue(control_request(3))


@pytest.mark.asyncio
async def test_startup_timeout_is_nonterminal_and_fifteen_minutes_is_terminal(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    stale_before_status_delivery = request
    await repository.enqueue(request)

    starting = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-starting",
        at=NOW + timedelta(seconds=1),
    )
    assert starting is not None
    await repository.mark_status_delivered(
        work=starting,
        claim_owner="publisher-starting",
        message_id="status-message-id",
        at=NOW + timedelta(seconds=2),
    )
    assert await repository.pending_status_count() == 0
    with pytest.raises(RepositoryConflict):
        await repository.mark_startup_timeout(
            request=stale_before_status_delivery,
            at=NOW + timedelta(minutes=3),
        )
    replay = await repository.get_replay(request)
    assert replay is not None
    request = replay.request

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
    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.desired_state is StatusMessageState.STARTUP_TIMEOUT
    assert publication.state is StatusPublicationState.PREPARED
    assert await repository.pending_status_count() == 1

    timeout_work = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-timeout",
        at=NOW + timedelta(minutes=3, seconds=2),
    )
    assert timeout_work is not None
    await repository.mark_status_delivered(
        work=timeout_work,
        claim_owner="publisher-timeout",
        message_id="status-message-id",
        at=NOW + timedelta(minutes=3, seconds=3),
    )
    replay_after_timeout = await repository.get_replay(request)
    assert replay_after_timeout is not None
    delivered_again = replay_after_timeout.request
    assert delivered_again.status_message_updated_at == NOW + timedelta(minutes=3, seconds=3)
    assert await repository.pending_status_count() == 0
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
    terminal_publication = await repository.get_status_publication(request.interaction_id)
    assert terminal_publication is not None
    assert terminal_publication.desired_state is StatusMessageState.TERMINAL_FAILED
    assert terminal_publication.state is StatusPublicationState.PREPARED
    assert await repository.pending_status_count() == 1
    assert await repository.list_ready(at=NOW + timedelta(minutes=16)) == ()


@pytest.mark.asyncio
async def test_status_claim_retry_delivery_and_counter_are_exactly_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)

    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-1",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert (
        await repository.claim_status_publication(
            interaction_id=request.interaction_id,
            claim_owner="publisher-2",
            at=NOW + timedelta(seconds=2),
        )
        is None
    )
    retry_at = NOW + timedelta(seconds=10)
    retried = await repository.reschedule_status_publication(
        work=claimed,
        claim_owner="publisher-1",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=retry_at,
        error_code="status_unavailable",
    )
    assert retried.state is StatusPublicationState.RETRYING
    assert retried.delivery_attempt == 1
    assert not retried.history_reconciliation_required
    assert await repository.pending_status_count() == 1
    assert (
        await repository.claim_status_publication(
            interaction_id=request.interaction_id,
            claim_owner="publisher-2",
            at=retry_at - timedelta(microseconds=1),
        )
        is None
    )
    due = await repository.list_due_status_publications(at=retry_at, limit=20)
    assert tuple(item.canonical_interaction_id for item in due) == (request.interaction_id,)

    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-2",
        at=retry_at,
    )
    assert reclaimed is not None
    delivered = await repository.mark_status_delivered(
        work=reclaimed,
        claim_owner="publisher-2",
        message_id="status-message-id",
        at=retry_at + timedelta(seconds=1),
    )
    assert delivered.state is StatusPublicationState.DELIVERED
    assert await repository.pending_status_count() == 0
    assert (
        await repository.mark_status_delivered(
            work=reclaimed,
            claim_owner="publisher-2",
            message_id="status-message-id",
            at=retry_at + timedelta(seconds=2),
        )
        == delivered
    )
    assert await repository.pending_status_count() == 0


@pytest.mark.asyncio
async def test_status_delivery_transaction_response_loss_replays_exactly_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    original = dynamodb_client.transact_write_items
    tokens: list[str] = []

    def commit_then_lose_response(
        **kwargs: Unpack[TransactWriteItemsInputTypeDef],
    ) -> TransactWriteItemsOutputTypeDef:
        response = original(**kwargs)
        token = kwargs.get("ClientRequestToken")
        assert isinstance(token, str)
        tokens.append(token)
        if len(tokens) == 1:
            raise ReadTimeoutError(
                endpoint_url="http://127.0.0.1/dynamodb-local",
                error="simulated response loss",
            )
        return response

    monkeypatch.setattr(dynamodb_client, "transact_write_items", commit_then_lose_response)
    delivered_at = NOW + timedelta(seconds=2)
    with pytest.raises(RepositoryUnavailable):
        await repository.mark_status_delivered(
            work=claimed,
            claim_owner="publisher",
            message_id="status-message-id",
            at=delivered_at,
        )

    delivered = await repository.mark_status_delivered(
        work=claimed,
        claim_owner="publisher",
        message_id="status-message-id",
        at=delivered_at,
    )

    assert tokens[0] == tokens[1]
    assert delivered.state is StatusPublicationState.DELIVERED
    assert delivered.status_message_id == "status-message-id"
    assert await repository.pending_status_count() == 0
    replay = await repository.get_replay(request)
    assert replay is not None
    assert replay.request.status_message_id == "status-message-id"


@pytest.mark.asyncio
async def test_ambiguous_create_persists_one_way_history_reconciliation(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None

    retry_at = NOW + timedelta(seconds=10)
    ambiguous = await repository.reschedule_status_publication(
        work=claimed,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=retry_at,
        error_code="status_unavailable",
        message_may_exist=True,
    )

    assert ambiguous.state is StatusPublicationState.RETRYING
    assert ambiguous.delivery_attempt == 1
    assert ambiguous.history_reconciliation_required
    assert (
        await repository.reschedule_status_publication(
            work=claimed,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=2),
            next_attempt_at=retry_at,
            error_code="status_unavailable",
            message_may_exist=True,
        )
        == ambiguous
    )
    with pytest.raises(RepositoryConflict):
        await repository.reschedule_status_publication(
            work=claimed,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=2),
            next_attempt_at=retry_at,
            error_code="status_unavailable",
            message_may_exist=False,
        )
    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="reconciler",
        at=retry_at,
    )
    assert reclaimed is not None
    assert reclaimed.publication.history_reconciliation_required
    assert reclaimed.publication.delivery_attempt == 2


@pytest.mark.asyncio
async def test_history_cursor_is_durable_and_ambiguity_cannot_rearm_create(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    retry_at = NOW + timedelta(seconds=10)

    progressed = await repository.reschedule_status_publication(
        work=claimed,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=retry_at,
        error_code="status_rate_limited",
        history_checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="500",
            history_verified_head_message_id="700",
            history_gap_cursor_message_id="800",
            history_gap_upper_message_id="900",
        ),
    )

    assert progressed.state is StatusPublicationState.RETRYING
    assert progressed.history_checkpoint == StatusHistoryCheckpoint(
        history_cursor_message_id="500",
        history_verified_head_message_id="700",
        history_gap_cursor_message_id="800",
        history_gap_upper_message_id="900",
    )
    assert progressed.history_reconciliation_required
    assert progressed.delivery_attempt == 0
    resumed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-resumed",
        at=retry_at,
    )
    assert resumed is not None
    assert resumed.publication.history_checkpoint == progressed.history_checkpoint
    exhausted_checkpoint = StatusHistoryCheckpoint(
        history_verified_head_message_id="900",
    )
    exhausted_retry_at = retry_at + timedelta(seconds=2)
    await repository.reschedule_status_publication(
        work=resumed,
        claim_owner="publisher-resumed",
        at=retry_at + timedelta(seconds=1),
        next_attempt_at=exhausted_retry_at,
        error_code="status_unavailable",
        history_checkpoint=exhausted_checkpoint,
    )
    exhausted_claim = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-exhausted",
        at=exhausted_retry_at,
    )
    assert exhausted_claim is not None

    failed = await repository.mark_status_failed(
        work=exhausted_claim,
        claim_owner="publisher-exhausted",
        at=retry_at + timedelta(seconds=3),
        error_code="status_message_ambiguous",
    )

    assert failed.state is StatusPublicationState.FAILED
    assert failed.history_checkpoint == exhausted_checkpoint
    assert failed.history_reconciliation_required
    assert failed.error_code == "status_message_ambiguous"
    assert await repository.pending_status_count() == 0
    assert (
        await repository.claim_status_publication(
            interaction_id=request.interaction_id,
            claim_owner="publisher-too-early",
            at=retry_at + timedelta(seconds=4),
        )
        is None
    )

    rearmed = await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    assert rearmed.status_message_state is StatusMessageState.STARTUP_TIMEOUT
    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.state is StatusPublicationState.PREPARED
    assert publication.history_reconciliation_required
    assert publication.history_checkpoint == exhausted_checkpoint
    assert publication.delivery_attempt == 0
    reconciler = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-reconciler",
        at=NOW + timedelta(minutes=3, seconds=1),
    )
    assert reconciler is not None
    assert reconciler.publication.history_reconciliation_required
    assert (
        await repository.claim_status_publication(
            interaction_id=request.interaction_id,
            claim_owner="publisher-recovery",
            at=retry_at + timedelta(seconds=183),
        )
        is None
    )


@pytest.mark.asyncio
async def test_expired_status_claim_is_reacquired_and_old_owner_is_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    old = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="old-owner",
        at=NOW + timedelta(seconds=1),
    )
    assert old is not None

    current = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="new-owner",
        at=NOW + timedelta(seconds=181),
    )

    assert current is not None
    assert current.publication.claim_owner == "new-owner"
    assert current.publication.history_reconciliation_required
    with pytest.raises(RepositoryConflict):
        await repository.reschedule_status_publication(
            work=old,
            claim_owner="old-owner",
            at=NOW + timedelta(seconds=182),
            next_attempt_at=NOW + timedelta(seconds=190),
            error_code="status_unavailable",
        )


@pytest.mark.asyncio
async def test_same_owner_reclaim_at_exact_expiry_is_fenced_by_delivery_attempt(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed_at = NOW + timedelta(seconds=1)
    stale = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=claimed_at,
    )
    assert stale is not None
    assert stale.publication.claim_expires_at is not None
    with pytest.raises(RepositoryConflict):
        await repository.reschedule_status_publication(
            work=stale,
            claim_owner="publisher",
            at=stale.publication.claim_expires_at,
            next_attempt_at=stale.publication.claim_expires_at + timedelta(seconds=1),
            error_code="status_unavailable",
        )

    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=stale.publication.claim_expires_at,
    )

    assert reclaimed is not None
    assert reclaimed.publication.delivery_attempt == stale.publication.delivery_attempt + 1
    assert reclaimed.publication.history_reconciliation_required
    with pytest.raises(RepositoryConflict):
        await repository.reschedule_status_publication(
            work=stale,
            claim_owner="publisher",
            at=stale.publication.claim_expires_at + timedelta(microseconds=1),
            next_attempt_at=stale.publication.claim_expires_at + timedelta(seconds=1),
            error_code="status_unavailable",
        )
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_expired_status_claim_preserves_the_complete_history_checkpoint(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    initial = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="scanner-initial",
        at=NOW + timedelta(seconds=1),
    )
    assert initial is not None
    checkpoint = StatusHistoryCheckpoint(
        history_cursor_message_id="500",
        history_verified_head_message_id="700",
        history_gap_cursor_message_id="800",
        history_gap_upper_message_id="900",
    )
    retry_at = NOW + timedelta(seconds=10)
    await repository.reschedule_status_publication(
        work=initial,
        claim_owner="scanner-initial",
        at=NOW + timedelta(seconds=2),
        next_attempt_at=retry_at,
        error_code="status_unavailable",
        history_checkpoint=checkpoint,
    )
    expired = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="scanner-expired",
        at=retry_at,
    )
    assert expired is not None
    with pytest.raises(ValueError, match="verified head cannot move backwards"):
        await repository.reschedule_status_publication(
            work=expired,
            claim_owner="scanner-expired",
            at=retry_at + timedelta(seconds=1),
            next_attempt_at=retry_at + timedelta(seconds=20),
            error_code="status_unavailable",
            history_checkpoint=replace(
                checkpoint,
                history_verified_head_message_id="600",
            ),
        )

    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="scanner-replacement",
        at=retry_at + timedelta(seconds=180),
    )

    assert reclaimed is not None
    assert reclaimed.publication.history_checkpoint == checkpoint
    assert reclaimed.publication.delivery_attempt == expired.publication.delivery_attempt + 1
    with pytest.raises(RepositoryConflict):
        await repository.reschedule_status_publication(
            work=expired,
            claim_owner="scanner-expired",
            at=retry_at + timedelta(seconds=181),
            next_attempt_at=retry_at + timedelta(seconds=190),
            error_code="status_unavailable",
            history_checkpoint=replace(
                checkpoint,
                history_gap_cursor_message_id="750",
            ),
        )
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_status_delivery_rolls_back_when_pending_counter_is_inconsistent(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"}),
        UpdateExpression="SET #count=:zero",
        ExpressionAttributeNames={"#count": "count"},
        ExpressionAttributeValues=marshal_item({":zero": 0}),
    )

    with pytest.raises(RepositoryConflict):
        await repository.mark_status_delivered(
            work=claimed,
            claim_owner="publisher",
            message_id="500",
            at=NOW + timedelta(seconds=2),
        )

    publication = await repository.get_status_publication(request.interaction_id)
    replay = await repository.get_replay(request)
    assert publication is not None
    assert publication.state is StatusPublicationState.CLAIMED
    assert replay is not None
    assert replay.request.status_message_id is None
    assert await repository.pending_status_count() == 0


@pytest.mark.asyncio
async def test_stale_rate_limit_preserves_latest_desired_state_and_retry_after(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    stale = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=2, seconds=59),
    )
    assert stale is not None
    await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    retry_at = NOW + timedelta(minutes=18)

    retried = await repository.reschedule_status_publication(
        work=stale,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=3, seconds=1),
        next_attempt_at=retry_at,
        error_code="status_rate_limited",
        history_checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="500",
            history_verified_head_message_id="700",
            history_gap_cursor_message_id="800",
            history_gap_upper_message_id="900",
        ),
    )

    assert retried.state is StatusPublicationState.RETRYING
    assert retried.desired_state is StatusMessageState.STARTUP_TIMEOUT
    assert retried.next_attempt_at == retry_at
    assert retried.error_code == "status_rate_limited"
    assert retried.history_reconciliation_required
    assert retried.history_checkpoint == StatusHistoryCheckpoint(
        history_cursor_message_id="500",
        history_verified_head_message_id="700",
        history_gap_cursor_message_id="800",
        history_gap_upper_message_id="900",
    )
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_safe_prewrite_retry_releases_a_concurrent_latest_desired_state(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    stale = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=2, seconds=59),
    )
    assert stale is not None
    await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    retry_at = NOW + timedelta(minutes=3, seconds=10)

    retried = await repository.reschedule_status_publication(
        work=stale,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=3, seconds=1),
        next_attempt_at=retry_at,
        error_code="status_rate_limited",
        message_may_exist=False,
    )

    assert retried.state is StatusPublicationState.RETRYING
    assert retried.desired_state is StatusMessageState.STARTUP_TIMEOUT
    assert retried.delivery_attempt == 0
    assert not retried.history_reconciliation_required
    assert retried.history_checkpoint is None
    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-next",
        at=retry_at,
    )
    assert reclaimed is not None
    assert reclaimed.publication.delivery_attempt == 1
    assert not reclaimed.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_ambiguous_stale_retry_keeps_latest_desired_state_in_reconciliation(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    stale = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=2, seconds=59),
    )
    assert stale is not None
    await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )

    retried = await repository.reschedule_status_publication(
        work=stale,
        claim_owner="publisher",
        at=NOW + timedelta(minutes=3, seconds=1),
        next_attempt_at=NOW + timedelta(minutes=3, seconds=10),
        error_code="status_unavailable",
        message_may_exist=True,
    )

    assert retried.desired_state is StatusMessageState.STARTUP_TIMEOUT
    assert retried.delivery_attempt == 0
    assert retried.history_reconciliation_required
    assert retried.history_checkpoint is None


@pytest.mark.asyncio
async def test_safe_permanent_failure_can_rearm_a_new_status_revision(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    failed = await repository.mark_status_failed(
        work=claimed,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=2),
        error_code="status_delivery_rejected",
    )
    assert not failed.history_reconciliation_required

    await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.state is StatusPublicationState.PREPARED
    assert publication.delivery_attempt == 0
    assert not publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_final_attempt_ambiguity_survives_failed_and_new_revision_rearm(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = replace(
        new_request(1, created_at=NOW),
        interaction_id="300",
        operation_id="300",
    )
    await repository.enqueue(request)
    claim_at = NOW + timedelta(seconds=1)

    for expected_attempt in range(1, 8):
        claimed = await repository.claim_status_publication(
            interaction_id=request.interaction_id,
            claim_owner=f"publisher-{expected_attempt}",
            at=claim_at,
        )
        assert claimed is not None
        assert claimed.publication.delivery_attempt == expected_attempt
        retry_at = claim_at + timedelta(seconds=1)
        await repository.reschedule_status_publication(
            work=claimed,
            claim_owner=f"publisher-{expected_attempt}",
            at=claim_at + timedelta(microseconds=1),
            next_attempt_at=retry_at,
            error_code="status_unavailable",
            message_may_exist=False,
        )
        claim_at = retry_at

    final_claim = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-final",
        at=claim_at,
    )
    assert final_claim is not None
    assert final_claim.publication.delivery_attempt == 8
    failed = await repository.mark_status_failed(
        work=final_claim,
        claim_owner="publisher-final",
        at=claim_at + timedelta(microseconds=1),
        error_code="status_unavailable",
        message_may_exist=True,
    )

    assert failed.state is StatusPublicationState.FAILED
    assert failed.history_reconciliation_required
    assert await repository.pending_status_count() == 0
    await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.state is StatusPublicationState.PREPARED
    assert publication.delivery_attempt == 0
    assert publication.history_reconciliation_required
    reclaimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="reconciler",
        at=NOW + timedelta(minutes=3, seconds=1),
    )
    assert reclaimed is not None
    assert reclaimed.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_rate_limit_settles_against_latest_request_when_public_state_is_unchanged(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    status_work = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert status_work is not None
    runtime_claim = await repository.claim(
        request=request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=2),
    )
    assert runtime_claim is not None
    retry_at = NOW + timedelta(minutes=15)

    retried = await repository.reschedule_status_publication(
        work=status_work,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=3),
        next_attempt_at=retry_at,
        error_code="status_rate_limited",
    )

    assert retried.state is StatusPublicationState.RETRYING
    assert retried.desired_state is StatusMessageState.STARTING
    assert retried.next_attempt_at == retry_at
    assert retried.error_code == "status_rate_limited"
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_permanent_failure_settles_against_latest_request_without_counter_drift(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    status_work = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert status_work is not None
    runtime_claim = await repository.claim(
        request=request,
        claim_owner="runtime",
        at=NOW + timedelta(seconds=2),
    )
    assert runtime_claim is not None

    failed = await repository.mark_status_failed(
        work=status_work,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=3),
        error_code="status_delivery_rejected",
    )

    assert failed.state is StatusPublicationState.FAILED
    assert failed.desired_state is StatusMessageState.STARTING
    assert failed.error_code == "status_delivery_rejected"
    assert await repository.pending_status_count() == 0


@pytest.mark.asyncio
async def test_known_status_message_cannot_be_rebound_without_confirmed_deletion(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    initial = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-initial",
        at=NOW + timedelta(seconds=1),
    )
    assert initial is not None
    await repository.mark_status_delivered(
        work=initial,
        claim_owner="publisher-initial",
        message_id="500",
        at=NOW + timedelta(seconds=2),
    )
    replay = await repository.get_replay(request)
    assert replay is not None
    await repository.request_status_publication(
        request=replay.request,
        state=StatusMessageState.RECOVERED,
        at=NOW + timedelta(seconds=3),
    )
    current = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-edit",
        at=NOW + timedelta(seconds=4),
    )
    assert current is not None

    with pytest.raises(RepositoryConflict, match="cannot rebind"):
        await repository.mark_status_delivered(
            work=current,
            claim_owner="publisher-edit",
            message_id="501",
            at=NOW + timedelta(seconds=5),
        )

    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.status_message_id == "500"
    assert publication.state is StatusPublicationState.CLAIMED
    assert await repository.pending_status_count() == 1


@pytest.mark.asyncio
async def test_stale_timeout_replay_cannot_hide_a_later_acceptance(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    timed_out = await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )
    claimed = await repository.claim(
        request=timed_out,
        claim_owner="runtime",
        at=NOW + timedelta(minutes=3, seconds=1),
    )
    assert claimed is not None
    accepted = await repository.mark_accepted(
        request=claimed,
        claim_owner="runtime",
        at=NOW + timedelta(minutes=3, seconds=2),
        debate_id=DebateId.new(),
        attempt_id=AttemptId.new(),
    )
    assert accepted.status is IngressStatus.ACCEPTED

    with pytest.raises(RepositoryConflict, match="stale"):
        await repository.mark_startup_timeout(
            request=timed_out,
            at=NOW + timedelta(minutes=3, seconds=3),
        )
    with pytest.raises(RepositoryConflict, match="stale"):
        await repository.request_status_publication(
            request=timed_out,
            state=StatusMessageState.STARTUP_TIMEOUT,
            at=NOW + timedelta(minutes=3, seconds=3),
        )


@pytest.mark.asyncio
async def test_latest_desired_state_serializes_a_stale_delivery_without_counter_drift(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    stale = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="stale-publisher",
        at=NOW + timedelta(minutes=2, seconds=59),
    )
    assert stale is not None

    timed_out = await repository.mark_startup_timeout(
        request=request,
        at=NOW + timedelta(minutes=3),
    )

    prepared = await repository.mark_status_delivered(
        work=stale,
        claim_owner="stale-publisher",
        message_id="500",
        at=NOW + timedelta(minutes=3, seconds=1),
    )
    current = await repository.get_status_publication(request.interaction_id)
    assert current is not None
    assert current == prepared
    assert current.desired_state is StatusMessageState.STARTUP_TIMEOUT
    assert current.state is StatusPublicationState.PREPARED
    assert current.delivered_state is StatusMessageState.STARTING
    assert current.status_message_id == "500"
    assert timed_out.status is IngressStatus.PENDING
    assert await repository.pending_status_count() == 1

    latest = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="latest-publisher",
        at=NOW + timedelta(minutes=3, seconds=2),
    )
    assert latest is not None
    assert latest.publication.status_message_id == "500"
    delivered = await repository.mark_status_delivered(
        work=latest,
        claim_owner="latest-publisher",
        message_id="500",
        at=NOW + timedelta(minutes=3, seconds=3),
    )
    assert delivered.state is StatusPublicationState.DELIVERED
    assert delivered.delivered_state is StatusMessageState.STARTUP_TIMEOUT
    assert await repository.pending_status_count() == 0


@pytest.mark.asyncio
async def test_permanent_status_failure_settles_then_a_new_state_rearms_once(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    claimed = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None

    failed = await repository.mark_status_failed(
        work=claimed,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=2),
        error_code="status_delivery_rejected",
    )
    assert failed.state is StatusPublicationState.FAILED
    assert await repository.pending_status_count() == 0
    assert (
        await repository.mark_status_failed(
            work=claimed,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=3),
            error_code="status_delivery_rejected",
        )
        == failed
    )
    assert await repository.pending_status_count() == 0

    recovered = await repository.request_status_publication(
        request=request,
        state=StatusMessageState.RECOVERED,
        at=NOW + timedelta(seconds=4),
    )
    assert recovered.status_message_state is StatusMessageState.RECOVERED
    assert await repository.pending_status_count() == 1
    publication = await repository.get_status_publication(request.interaction_id)
    assert publication is not None
    assert publication.state is StatusPublicationState.PREPARED
    assert publication.desired_state is StatusMessageState.RECOVERED


@pytest.mark.asyncio
async def test_confirmed_missing_message_rotates_nonce_and_fences_old_claim(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    request = new_request(1, created_at=NOW)
    await repository.enqueue(request)
    first_claim = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-starting",
        at=NOW + timedelta(seconds=1),
    )
    assert first_claim is not None
    await repository.mark_status_delivered(
        work=first_claim,
        claim_owner="publisher-starting",
        message_id="deleted-message-id",
        at=NOW + timedelta(seconds=2),
    )
    replay = await repository.get_replay(request)
    assert replay is not None
    accepted_desired = await repository.request_status_publication(
        request=replay.request,
        state=StatusMessageState.ACCEPTED,
        at=NOW + timedelta(seconds=3),
    )
    edit_claim = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-edit",
        at=NOW + timedelta(seconds=4),
    )
    assert edit_claim is not None
    old_nonce = edit_claim.publication.nonce

    replacement = await repository.replace_missing_status_message(
        work=edit_claim,
        claim_owner="publisher-edit",
        at=NOW + timedelta(seconds=5),
    )

    assert replacement.request.status_message_id is None
    assert replacement.publication.incarnation == 1
    assert replacement.publication.nonce != old_nonce
    assert replacement.publication.state is StatusPublicationState.RETRYING
    assert await repository.pending_status_count() == 1
    with pytest.raises(RepositoryConflict):
        await repository.mark_status_delivered(
            work=edit_claim,
            claim_owner="publisher-edit",
            message_id="stale-message-id",
            at=NOW + timedelta(seconds=6),
        )

    replacement_claim = await repository.claim_status_publication(
        interaction_id=request.interaction_id,
        claim_owner="publisher-replacement",
        at=NOW + timedelta(seconds=6),
    )
    assert replacement_claim is not None
    await repository.mark_status_delivered(
        work=replacement_claim,
        claim_owner="publisher-replacement",
        message_id="replacement-message-id",
        at=NOW + timedelta(seconds=7),
    )
    assert accepted_desired.status_message_state is StatusMessageState.ACCEPTED
    assert await repository.pending_status_count() == 0
