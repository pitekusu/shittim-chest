"""SDK-level ingress contracts, including complete GSI pagination."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application import IngressOperationResult, IngressRequest

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def request(index: int) -> IngressRequest:
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
        created_at=NOW + timedelta(microseconds=index),
    )


@pytest.mark.asyncio
async def test_active_gsi_query_consumes_every_page_in_fifo_order() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    first = request(1)
    second = request(2)
    last_key = marshal_item(
        {
            "PK": "CONTROL#INGRESS",
            "SK": ingress_request_sort_key(first),
            "gsi2pk": "INGRESS#ACTIVE",
            "gsi2sk": ingress_request_sort_key(first),
        }
    )
    common = {
        "TableName": "test-table",
        "IndexName": "gsi2",
        "KeyConditionExpression": "gsi2pk=:active",
        "ExpressionAttributeValues": marshal_item({":active": "INGRESS#ACTIVE"}),
        "ScanIndexForward": True,
    }

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "query",
            {
                "Items": [marshal_item(serialize_ingress_request(first))],
                "Count": 1,
                "ScannedCount": 1,
                "LastEvaluatedKey": last_key,
            },
            common,
        )
        stubber.add_response(
            "query",
            {
                "Items": [marshal_item(serialize_ingress_request(second))],
                "Count": 1,
                "ScannedCount": 1,
            },
            {**common, "ExclusiveStartKey": last_key},
        )

        assert await repository.list_ready(at=NOW + timedelta(seconds=1)) == (
            first,
            second,
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_operation_replay_uses_a_strongly_consistent_get() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    source = request(1)
    operation = IngressOperationResult(
        operation_id="operation-0001",
        interaction_id=source.interaction_id,
        request_sort_key=ingress_request_sort_key(source),
        status=source.status,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_ingress_operation_result(operation))},
            {
                "TableName": "test-table",
                "Key": marshal_item(
                    {"PK": f"INGRESS_OPERATION#{source.interaction_id}", "SK": "RESULT"}
                ),
                "ConsistentRead": True,
            },
        )

        assert await repository.get_operation_result(source.interaction_id) == operation
        stubber.assert_no_pending_responses()
