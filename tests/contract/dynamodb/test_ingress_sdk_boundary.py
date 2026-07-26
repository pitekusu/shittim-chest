"""SDK-level ingress contracts, including complete GSI pagination."""

from __future__ import annotations

import traceback
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
    serialize_ingress_semantic_binding,
    serialize_ingress_status_publication,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application import (
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressSemanticOperationBinding,
    IngressStatusPublication,
)
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)
DEBATE_ID = DebateId.new()
ATTEMPT_ID = AttemptId.new()


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
        application_id="application-id",
        question=f"question-{index}",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW + timedelta(microseconds=index),
    )


def component_request(interaction_id: str) -> IngressRequest:
    return IngressRequest.control_operation(
        interaction_id=interaction_id,
        operation_id=f"cancel:{DEBATE_ID}:{ATTEMPT_ID}",
        kind=IngressKind.CANCEL,
        application_id="application-id",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        requester_can_manage_messages=False,
        guild_id="guild-id",
        channel_id="thread-id",
        parent_channel_id="channel-id",
        custom_id=f"shittim:cancel:{DEBATE_ID}:{ATTEMPT_ID}",
        source_message_id="panel-id",
        source_thread_id="thread-id",
        target_debate_id=DEBATE_ID,
        expected_attempt_id=ATTEMPT_ID,
        created_at=NOW,
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


@pytest.mark.asyncio
async def test_component_semantic_replay_is_bounded_to_two_sdk_rounds() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    canonical = component_request("301")
    incoming = component_request("302")
    request_sort_key = ingress_request_sort_key(canonical)
    operation = IngressOperationResult(
        operation_id=canonical.operation_id,
        interaction_id=canonical.interaction_id,
        request_sort_key=request_sort_key,
        status=canonical.status,
        created_at=canonical.created_at,
        updated_at=canonical.updated_at,
    )
    binding = IngressSemanticOperationBinding(
        operation_id=canonical.operation_id,
        canonical_interaction_id=canonical.interaction_id,
        request_sort_key=request_sort_key,
        created_at=canonical.created_at,
    )
    publication = IngressStatusPublication.prepared(canonical)

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_ingress_semantic_binding(binding))},
            {
                "TableName": "test-table",
                "Key": marshal_item(
                    {
                        "PK": f"INGRESS_SEMANTIC_OPERATION#{canonical.operation_id}",
                        "SK": "BINDING",
                    }
                ),
                "ConsistentRead": True,
            },
        )
        stubber.add_response(
            "transact_get_items",
            {
                "Responses": [
                    {"Item": marshal_item(serialize_ingress_operation_result(operation))},
                    {"Item": marshal_item(serialize_ingress_request(canonical))},
                    {"Item": marshal_item(serialize_ingress_status_publication(publication))},
                ]
            },
            {
                "TransactItems": [
                    {
                        "Get": {
                            "TableName": "test-table",
                            "Key": marshal_item(
                                {
                                    "PK": f"INGRESS_OPERATION#{canonical.interaction_id}",
                                    "SK": "RESULT",
                                }
                            ),
                        }
                    },
                    {
                        "Get": {
                            "TableName": "test-table",
                            "Key": marshal_item({"PK": "CONTROL#INGRESS", "SK": request_sort_key}),
                        }
                    },
                    {
                        "Get": {
                            "TableName": "test-table",
                            "Key": marshal_item(
                                {
                                    "PK": f"INGRESS_OPERATION#{canonical.interaction_id}",
                                    "SK": "STATUS_PUBLICATION",
                                }
                            ),
                        }
                    },
                ],
                "ReturnConsumedCapacity": "NONE",
            },
        )

        replay = await repository.get_replay(incoming)
        stubber.assert_no_pending_responses()

    assert replay is not None
    assert not replay.created
    assert replay.request == canonical


@pytest.mark.asyncio
async def test_sdk_failure_is_mapped_to_content_free_repository_unavailable() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    source = request(1)
    expected = {
        "TableName": "test-table",
        "Key": marshal_item({"PK": f"INGRESS_OPERATION#{source.interaction_id}", "SK": "RESULT"}),
        "ConsistentRead": True,
    }

    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "get_item",
            service_error_code="InternalServerError",
            service_message="sensitive provider detail",
            http_status_code=500,
            expected_params=expected,
        )

        with pytest.raises(RepositoryUnavailable, match=r"^repository_unavailable$") as caught:
            await repository.get_operation_result(source.interaction_id)
        assert "sensitive provider detail" not in str(caught.value)
        assert "sensitive provider detail" not in "".join(traceback.format_exception(caught.value))
        stubber.assert_no_pending_responses()
