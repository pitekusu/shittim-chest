"""Actual transaction boundaries for owned mobile admission and recovery."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.application.ports import (
    MobileIngressSessionInvalid,
    RepositoryIdentityConflict,
)
from shittim_chest.application.scale_to_zero import IngressRequest

NOW = datetime(2026, 10, 4, tzinfo=UTC)
OWNER = "a" * 43


def request(index: int, *, owner: str = OWNER) -> IngressRequest:
    return IngressRequest.mobile_debate(
        request_id=f"00000000-0000-4000-8000-{index:012d}",
        owner_key=owner,
        application_id="application-id",
        question=f"Fictional question {index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        created_at=NOW + timedelta(microseconds=index),
    )


@pytest.mark.asyncio
async def test_replay_owner_isolation_and_public_pagination(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    first = request(1)
    assert first.mobile_request_id is not None
    created = await asyncio.gather(
        repository.enqueue_mobile(first), repository.enqueue_mobile(first)
    )
    assert sum(result.created for result in created) == 1
    with pytest.raises(RepositoryIdentityConflict):
        await repository.enqueue_mobile(replace(first, question="Changed payload"))
    for index in (2, 3):
        await repository.enqueue_mobile(request(index))
    other = request(1, owner="b" * 43)
    assert (await repository.enqueue_mobile(other)).created
    assert (
        await repository.get_mobile_request(owner_key=OWNER, request_id=first.mobile_request_id)
        == first
    )
    assert (
        await repository.get_mobile_request(owner_key="c" * 43, request_id=first.mobile_request_id)
        is None
    )
    page = await repository.list_mobile_requests(owner_key=OWNER, since=NOW, limit=2)
    assert page.requests == (request(3), request(2))
    assert page.next_created_at == request(2).created_at
    assert page.next_request_id == request(2).mobile_request_id
    remainder = await repository.list_mobile_requests(
        owner_key=OWNER,
        since=NOW,
        limit=2,
        cursor_created_at=page.next_created_at,
        cursor_request_id=page.next_request_id,
    )
    assert remainder.requests == (first,)
    assert remainder.next_request_id is None
    counter = dynamodb_client.get_item(
        TableName=dynamodb_table, Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "COUNTER"})
    )
    assert unmarshal_item(counter["Item"])["count"] == 4


@pytest.mark.asyncio
async def test_session_revocation_atomically_prevents_all_admission_writes(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    session_key = {"PK": "MOBILE_SESSION#fictional", "SK": "SESSION"}
    condition = cast(
        TransactWriteItemTypeDef,
        {
            "ConditionCheck": {
                "TableName": dynamodb_table,
                "Key": marshal_item(session_key),
                "ConditionExpression": "attribute_exists(PK) AND expires_at>:at",
                "ExpressionAttributeValues": marshal_item({":at": int(NOW.timestamp())}),
            }
        },
    )
    first = request(1)
    assert first.mobile_request_id is not None
    for expiry in (None, int(NOW.timestamp())):
        if expiry is not None:
            dynamodb_client.put_item(
                TableName=dynamodb_table, Item=marshal_item({**session_key, "expires_at": expiry})
            )
        with pytest.raises(MobileIngressSessionInvalid):
            await repository.enqueue_mobile(first, session_condition=condition)
        assert (
            await repository.get_mobile_request(owner_key=OWNER, request_id=first.mobile_request_id)
            is None
        )
        assert not (await repository.list_mobile_requests(owner_key=OWNER, since=NOW)).requests
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item({**session_key, "expires_at": int(NOW.timestamp()) + 1}),
    )
    assert (await repository.enqueue_mobile(first, session_condition=condition)).created
    dynamodb_client.delete_item(TableName=dynamodb_table, Key=marshal_item(session_key))
    with pytest.raises(MobileIngressSessionInvalid):
        await repository.enqueue_mobile(request(2), session_condition=condition)
    counter = dynamodb_client.get_item(
        TableName=dynamodb_table, Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "COUNTER"})
    )
    assert unmarshal_item(counter["Item"])["count"] == 1
