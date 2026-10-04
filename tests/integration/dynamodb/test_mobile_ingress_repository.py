"""Actual transaction boundaries for owned mobile admission and recovery."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from discord.utils import time_snowflake
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.adapters.dynamodb.repository import DynamoDbDebateRepository
from shittim_chest.application.models import DebateSnapshot
from shittim_chest.application.ports import (
    MobileIngressSessionInvalid,
    RepositoryIdentityConflict,
    RepositoryQuotaExceeded,
)
from shittim_chest.application.scale_to_zero import IngressClaimFence, IngressRequest, IngressStatus
from shittim_chest.domain import AttemptId, DebateId, DebateState

NOW = datetime(2026, 10, 4, tzinfo=UTC)
OWNER = "a" * 43


def request(index: int, *, owner: str = OWNER) -> IngressRequest:
    candidate = IngressRequest.mobile_debate(
        request_id=f"00000000-0000-4000-8000-a{index:011x}",
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
    return replace(
        candidate,
        history_after_snowflake=str(time_snowflake(candidate.created_at - timedelta(seconds=1))),
    )


@pytest.mark.asyncio
async def test_replay_owner_isolation_and_public_pagination(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> None:
    repository = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    first = request(1)
    assert first.mobile_request_id is not None
    unprepared = replace(first, history_after_snowflake=None)
    created = await asyncio.gather(
        repository.enqueue_mobile(unprepared), repository.enqueue_mobile(unprepared)
    )
    assert sum(result.created for result in created) == 1
    assert all(
        result.request.history_after_snowflake == first.history_after_snowflake
        for result in created
    )
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


@pytest.mark.asyncio
async def test_mobile_admission_uses_existing_lease_quota_and_accept_replay(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> None:
    ingress = DynamoDbIngressRepository(client=dynamodb_client, table_name=dynamodb_table)
    debates = DynamoDbDebateRepository(
        client=dynamodb_client, table_name=dynamodb_table, identity_hmac_key=b"i" * 32
    )
    first = (await ingress.enqueue_mobile(request(1))).request
    claimed = await ingress.claim(
        request=first, claim_owner="runtime", at=NOW + timedelta(seconds=1)
    )
    assert claimed is not None
    accepted_at = NOW + timedelta(seconds=2)
    fence = IngressClaimFence.from_claimed_request(
        claimed, claim_owner="runtime", write_at=accepted_at
    )
    debate_id, attempt_id = DebateId.new(), AttemptId.new()
    snapshot = DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=accepted_at),
        question=first.question or "",
        requester_id=first.requester_id,
        requester_username=first.requester_username,
        requester_display_name=first.requester_display_name,
        guild_id=first.guild_id,
        channel_id=first.channel_id,
        created_at=accepted_at,
        attempt_created_at=accepted_at,
    )
    accepted = await debates.create(
        snapshot, operation_id=first.operation_id, lease_owner="runtime", ingress_claim=fence
    )
    assert accepted.lease is not None
    assert accepted.origin_ingress_interaction_id == first.interaction_id
    assert (
        await debates.create(
            snapshot, operation_id=first.operation_id, lease_owner="runtime", ingress_claim=fence
        )
        == accepted
    )
    receipt = await ingress.mark_accepted(
        request=claimed,
        claim_owner="runtime",
        at=accepted_at + timedelta(microseconds=1),
        debate_id=debate_id,
        attempt_id=attempt_id,
    )
    assert receipt.status is IngressStatus.ACCEPTED
    assert first.mobile_request_id is not None
    assert (
        await ingress.get_mobile_request(owner_key=OWNER, request_id=first.mobile_request_id)
        == receipt
    )
    quota_key = {"PK": f"QUOTA#GUILD#{first.guild_id}", "SK": "DAY#2026-10-04"}
    quota = dynamodb_client.get_item(TableName=dynamodb_table, Key=marshal_item(quota_key))
    assert unmarshal_item(quota["Item"])["count"] == 1
    dynamodb_client.update_item(
        TableName=dynamodb_table,
        Key=marshal_item(quota_key),
        UpdateExpression="SET #count=:count",
        ExpressionAttributeNames={"#count": "count"},
        ExpressionAttributeValues=marshal_item({":count": 30}),
    )
    second = (await ingress.enqueue_mobile(request(2))).request
    second_claim = await ingress.claim(
        request=second, claim_owner="runtime", at=NOW + timedelta(seconds=3)
    )
    assert second_claim is not None
    second_at = NOW + timedelta(seconds=4)
    second_fence = IngressClaimFence.from_claimed_request(
        second_claim, claim_owner="runtime", write_at=second_at
    )
    second_id = DebateId.new()
    with pytest.raises(RepositoryQuotaExceeded):
        await debates.create(
            replace(
                snapshot,
                state=DebateState.accepted(second_id, AttemptId.new(), at=second_at),
                question=second.question or "",
                created_at=second_at,
                attempt_created_at=second_at,
            ),
            operation_id=second.operation_id,
            lease_owner="runtime",
            ingress_claim=second_fence,
        )
    assert await debates.get(second_id) is None
