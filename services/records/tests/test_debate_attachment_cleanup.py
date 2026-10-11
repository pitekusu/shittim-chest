"""SDK pagination keeps orphan cleanup streaming and restartable."""

from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import boto3
import pytest
from botocore.stub import ANY, Stubber
from shittim_chest.adapters.assets.images import DebateImageFailure
from shittim_chest.adapters.dynamodb.codec import marshal_item
from tests.factories import NOW

from shittim_records.debate_attachments import (
    DebateAttachmentService,
    DynamoDebateAttachmentRepository,
)

CLEANUP_AT = NOW + timedelta(days=2)


class Assets:
    def __init__(self, fail_once: str | None = None):
        self.fail_once = fail_once
        self.attempts: list[str] = []
        self.deleted: list[str] = []

    def delete_orphan(self, attachment_id: str, content_type: str, sha256: str) -> None:
        assert content_type == "image/png" and sha256 == "a" * 64
        self.attempts.append(attachment_id)
        if self.fail_once == attachment_id:
            self.fail_once = None
            raise DebateImageFailure("ATTACHMENT_UNAVAILABLE")
        self.deleted.append(attachment_id)


def repository(monkeypatch):
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - offline Stubber credential
    )
    repo = DynamoDebateAttachmentRepository(client, "source")
    # Ownership and persisted descriptor validation have their own real DynamoDB
    # coverage; these tests focus on query continuation and per-item cleanup order.
    monkeypatch.setattr(
        repo,
        "get",
        lambda owner, attachment_id: {
            "PK": f"DEBATE_UPLOAD#{owner}",
            "SK": f"IMAGE#{attachment_id}",
            "attachment_id": attachment_id,
            "content_type": "image/png",
            "sha256": "a" * 64,
        },
    )
    return client, repo


def cursor(attachment_id):
    return marshal_item(
        {
            "PK": "DEBATE_UPLOAD#owner",
            "SK": f"IMAGE#{attachment_id}",
            "gsi1pk": "ATTACHMENT#EXPIRY",
            "gsi1sk": f"expiry#{attachment_id}",
        }
    )


def page(stubber, ids, *, after=None, more=None):
    params = {
        "TableName": "source",
        "IndexName": "gsi1",
        "KeyConditionExpression": "gsi1pk = :pk AND gsi1sk <= :cutoff",
        "ExpressionAttributeValues": marshal_item(
            {
                ":pk": "ATTACHMENT#EXPIRY",
                ":cutoff": f"{int(CLEANUP_AT.timestamp()):020d}#~",
            }
        ),
        "Limit": 100,
    }
    if after is not None:
        params["ExclusiveStartKey"] = cursor(after)
    response = {
        "Items": [marshal_item({"owner_key": "owner", "attachment_id": id_}) for id_ in ids]
    }
    if more is not None:
        response["LastEvaluatedKey"] = cursor(more)
    stubber.add_response("query", response, params)


def update(stubber, attachment_id, *, conflict=False):
    params = {
        "TableName": "source",
        "Key": marshal_item({"PK": "DEBATE_UPLOAD#owner", "SK": f"IMAGE#{attachment_id}"}),
        "UpdateExpression": ANY,
        "ConditionExpression": ANY,
        "ExpressionAttributeNames": ANY,
        "ExpressionAttributeValues": ANY,
    }
    if conflict:
        stubber.add_client_error(
            "update_item", "ConditionalCheckFailedException", expected_params=params
        )
    else:
        stubber.add_response("update_item", {}, params)


def test_cleanup_deletes_more_than_one_hundred_images_without_buffering_pages(monkeypatch):
    client, repo = repository(monkeypatch)
    ids = [str(uuid4()) for _ in range(101)]
    assets = Assets()
    with Stubber(client) as stubber:
        page(stubber, ids[:100], more=ids[99])
        for attachment_id in ids[:100]:
            update(stubber, attachment_id)  # conditionally expire
            update(stubber, attachment_id)  # cleaned before visiting the next row
        page(stubber, ids[100:], after=ids[99])
        update(stubber, ids[100])
        update(stubber, ids[100])
        assert DebateAttachmentService(repo, cast(Any, assets)).cleanup(CLEANUP_AT) == 101
        stubber.assert_no_pending_responses()
    assert assets.deleted == ids


def test_first_page_consumption_conflict_does_not_block_later_page(monkeypatch):
    client, repo = repository(monkeypatch)
    first, second = str(uuid4()), str(uuid4())
    assets = Assets()
    with Stubber(client) as stubber:
        page(stubber, [first], more=first)
        update(stubber, first, conflict=True)
        page(stubber, [second], after=first)
        update(stubber, second)
        update(stubber, second)
        assert DebateAttachmentService(repo, cast(Any, assets)).cleanup(CLEANUP_AT) == 1
        stubber.assert_no_pending_responses()
    assert assets.deleted == [second]


def test_first_page_delete_failure_keeps_failed_row_and_retries_later_pages(monkeypatch):
    client, repo = repository(monkeypatch)
    first, failed, later = (str(uuid4()) for _ in range(3))
    assets = Assets(fail_once=failed)
    service = DebateAttachmentService(repo, cast(Any, assets))
    with Stubber(client) as stubber:
        page(stubber, [first, failed], more=failed)
        update(stubber, first)
        update(stubber, first)
        update(stubber, failed)
        # No cleaned write or page-two fetch occurs after the S3 failure.
        with pytest.raises(DebateImageFailure, match="ATTACHMENT_UNAVAILABLE"):
            service.cleanup(CLEANUP_AT)
        stubber.assert_no_pending_responses()
    assert assets.deleted == [first]
    with Stubber(client) as stubber:
        # Successful rows lost their GSI fields; the failed row remains indexed.
        page(stubber, [failed], more=failed)
        update(stubber, failed)
        update(stubber, failed)
        page(stubber, [later], after=failed)
        update(stubber, later)
        update(stubber, later)
        assert service.cleanup(CLEANUP_AT) == 2
        stubber.assert_no_pending_responses()
    assert assets.attempts == [first, failed, failed, later]
    assert assets.deleted == [first, failed, later]
