"""Real DynamoDB transactions protect image ownership, admission and cleanup races."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.control_records import DynamoDbControlRecordInitializer
from shittim_chest.application.ports import RepositoryIdentityConflict
from shittim_chest.application.scale_to_zero import IngressRequest
from tests.test_debate_attachments import ID, REQUEST, image
from tests.test_dynamodb_integration import dynamodb_client as dynamodb_client
from tests.test_dynamodb_integration import table_names as table_names
from tests.test_mobile_debate import CHANNEL, GUILD, KEY, NOW, USER, Sessions

from shittim_records.auth import AuthFailure
from shittim_records.contracts import DebateAttachmentUploadRequest
from shittim_records.debate_attachments import (
    AttachmentFailure,
    DynamoDebateAttachmentRepository,
)
from shittim_records.mobile_auth_adapters import _session_item
from shittim_records.mobile_debate_adapters import DynamoMobileDebateRepository


@pytest.fixture
def attachment_source_table(dynamodb_client) -> Iterator[str]:
    name = f"records-attachment-source-{uuid.uuid4().hex}"
    dynamodb_client.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": key, "AttributeType": "S"}
            for key in ("PK", "SK", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": index,
                "KeySchema": [
                    {"AttributeName": f"{index}pk", "KeyType": "HASH"},
                    {"AttributeName": f"{index}sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in ("gsi1", "gsi2")
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.get_waiter("table_exists").wait(TableName=name)
    try:
        yield name
    finally:
        dynamodb_client.delete_table(TableName=name)


def payload(attachment_id=ID, request_id=REQUEST):
    value = image(attachment_id)
    return DebateAttachmentUploadRequest(
        request_id=request_id,
        attachment_id=value.attachment_id,
        content_type="image/png",
        size_bytes=value.size_bytes,
        sha256=value.sha256,
    )


def prepare(repository, owner, attachment_id=ID):
    item = repository.reserve(owner, payload(attachment_id), NOW)
    token = repository.claim(item, NOW)
    value = image(attachment_id)
    repository.complete(item, token, value, NOW + timedelta(seconds=1))
    return value


def test_uuid_ownership_cannot_be_stolen_and_cap_is_transactional(
    dynamodb_client, attachment_source_table
):
    repository = DynamoDebateAttachmentRepository(dynamodb_client, attachment_source_table)
    first = repository.reserve("owner", payload(), NOW)
    assert repository.reserve("owner", payload(), NOW) == first
    assert repository.get("other", ID) is None
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_CONFLICT"):
        repository.reserve("other", payload(), NOW)
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_CONFLICT"):
        repository.reserve("owner", payload(request_id="00000000-0000-4000-8000-00000000000d"), NOW)
    others = [str(uuid.uuid4()) for _ in range(3)]

    def reserve(attachment_id):
        try:
            repository.reserve("owner", payload(attachment_id), NOW)
            return True
        except AttachmentFailure as error:
            assert error.code == "ATTACHMENT_LIMIT"
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        successes = list(pool.map(reserve, others))
    assert sum(successes) == 2
    count = unmarshal_item(
        dynamodb_client.get_item(
            TableName=attachment_source_table,
            Key=marshal_item({"PK": "DEBATE_UPLOAD#owner", "SK": f"REQUEST#{REQUEST}"}),
            ConsistentRead=True,
        )["Item"]
    )
    assert count["image_count"] == 3
    rejected = others[successes.index(False)]
    assert "Item" not in dynamodb_client.get_item(
        TableName=attachment_source_table,
        Key=marshal_item({"PK": f"DEBATE_UPLOAD_ID#{rejected}", "SK": "OWNER"}),
        ConsistentRead=True,
    )


def test_promotion_lease_and_expiry_fence_stale_workers(dynamodb_client, attachment_source_table):
    repository = DynamoDebateAttachmentRepository(dynamodb_client, attachment_source_table)
    item = repository.reserve("owner", payload(), NOW)
    first = repository.claim(item, NOW)
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_NOT_READY"):
        repository.claim(item, NOW + timedelta(seconds=30))
    second = repository.claim(item, NOW + timedelta(minutes=3))
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_NOT_READY"):
        repository.complete(item, first, image(), NOW + timedelta(minutes=3))
    repository.complete(item, second, image(), NOW + timedelta(minutes=3, seconds=1))
    assert repository.ready_images("owner", REQUEST, (ID,), NOW + timedelta(minutes=4)) == (
        image(),
    )
    expired = tuple(repository.expire(NOW + timedelta(days=2)))
    assert len(expired) == 1
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_EXPIRED"):
        repository.ready_images("owner", REQUEST, (ID,), NOW + timedelta(days=2))
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_NOT_READY"):
        repository.complete(item, second, image(), NOW + timedelta(days=2))


def admission(client, source, tables, images):
    session = Sessions().session
    assert session is not None
    session = session.model_copy(update={"expires_at": int((NOW + timedelta(days=7)).timestamp())})
    session_hash = "a" * 64
    client.put_item(TableName=tables[0], Item=marshal_item(_session_item(session_hash, session)))
    DynamoDbControlRecordInitializer(client=client, table_name=source).initialize()
    attachments = DynamoDebateAttachmentRepository(client, source)
    request = IngressRequest.mobile_debate(
        request_id=REQUEST,
        owner_key=session.requester_key,
        application_id="fixture-application",
        question="Fictional image atomic admission",
        requester_id=USER,
        requester_username="fixture",
        requester_display_name="Fixture",
        guild_id=GUILD,
        channel_id=CHANNEL,
        created_at=NOW,
        attachments=images,
    )
    repository = DynamoMobileDebateRepository(
        client=client,
        source_table=source,
        session_table=tables[0],
        archive_table=tables[1],
        identity_key=KEY,
        attachments=attachments,
    )
    return session, session_hash, request, repository, attachments


@pytest.mark.parametrize("revoked", (False, True))
def test_live_session_and_ready_images_are_consumed_with_queue_atomically(
    dynamodb_client, attachment_source_table, table_names, revoked
):
    session, session_hash, request, repository, attachments = admission(
        dynamodb_client, attachment_source_table, table_names, (image(),)
    )
    prepare(attachments, session.requester_key)
    if revoked:
        dynamodb_client.delete_item(
            TableName=table_names[0],
            Key=marshal_item({"PK": f"MOBILE_SESSION#{session_hash}", "SK": "META"}),
        )
    operation = repository.enqueue(
        request, session_hash=session_hash, session=session, now=NOW + timedelta(seconds=2)
    )
    if revoked:
        with pytest.raises(AuthFailure, match="session_required"):
            asyncio.run(operation)
        assert (
            asyncio.run(repository.get(owner_key=session.requester_key, request_id=REQUEST)) is None
        )
        assert attachments.get(session.requester_key, ID)["state"] == "ready"
    else:
        assert asyncio.run(operation).created
        persisted = asyncio.run(repository.get(owner_key=session.requester_key, request_id=REQUEST))
        assert persisted.attachments == (image(),)
        assert attachments.get(session.requester_key, ID)["state"] == "consumed"
        assert not tuple(attachments.expire(NOW + timedelta(days=30)))
        assert not asyncio.run(
            repository.enqueue(
                request, session_hash=session_hash, session=session, now=NOW + timedelta(seconds=3)
            )
        ).created


def test_cleanup_fence_prevents_partial_enqueue(
    dynamodb_client, attachment_source_table, table_names
):
    session, session_hash, request, repository, attachments = admission(
        dynamodb_client, attachment_source_table, table_names, (image(),)
    )
    prepare(attachments, session.requester_key)
    assert len(tuple(attachments.expire(NOW + timedelta(days=2)))) == 1
    with pytest.raises(RepositoryIdentityConflict):
        asyncio.run(
            repository.enqueue(
                request, session_hash=session_hash, session=session, now=NOW + timedelta(seconds=2)
            )
        )
    assert asyncio.run(repository.get(owner_key=session.requester_key, request_id=REQUEST)) is None
    control = unmarshal_item(
        dynamodb_client.get_item(
            TableName=attachment_source_table,
            Key=marshal_item({"PK": "CONTROL#INGRESS", "SK": "COUNTER"}),
            ConsistentRead=True,
        )["Item"]
    )
    assert control["count"] == 0


def test_consumption_cleanup_race_has_one_durable_winner(
    dynamodb_client, attachment_source_table, table_names
):
    session, session_hash, request, repository, attachments = admission(
        dynamodb_client, attachment_source_table, table_names, (image(),)
    )
    prepare(attachments, session.requester_key)

    def enqueue():
        try:
            return asyncio.run(
                repository.enqueue(
                    request,
                    session_hash=session_hash,
                    session=session,
                    now=NOW + timedelta(hours=23),
                )
            ).created
        except RepositoryIdentityConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        acceptance = pool.submit(enqueue)
        expiry = pool.submit(lambda: tuple(attachments.expire(NOW + timedelta(days=2))))
        accepted, expired = acceptance.result(), expiry.result()
    state = attachments.get(session.requester_key, ID)["state"]
    exists = asyncio.run(repository.get(owner_key=session.requester_key, request_id=REQUEST))
    assert (accepted, state, bool(exists), bool(expired)) in {
        (True, "consumed", True, False),
        (False, "expired", False, True),
    }


def test_expiry_paginator_continues_after_cleaned_items_leave_index(
    dynamodb_client, attachment_source_table
):
    attachments = DynamoDebateAttachmentRepository(dynamodb_client, attachment_source_table)
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for attachment_id in ids:
        prepare(attachments, "owner", attachment_id)
    cleaned = []
    # A one-item SDK page exercises real LastEvaluatedKey continuation while each
    # successful item is removed from that same index before the next page loads.
    for item in attachments.expire(NOW + timedelta(days=2), limit=1):
        cleaned.append(item["attachment_id"])
        attachments.cleaned(item)
    assert set(cleaned) == set(ids)
    assert len(cleaned) == 3
    assert not tuple(attachments.expire(NOW + timedelta(days=2), limit=1))
    for attachment_id in ids:
        stored = attachments.get("owner", attachment_id)
        assert stored is not None and stored["state"] == "expired"
        assert "gsi1pk" not in stored and "gsi1sk" not in stored
