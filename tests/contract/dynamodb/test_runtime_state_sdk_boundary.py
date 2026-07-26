"""SDK contracts for strongly consistent runtime reads and wake transactions."""

from datetime import UTC, datetime

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import ANY, Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    DynamoDbRuntimeStateRepository,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_runtime_state,
    serialize_runtime_wake_result,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application import (
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    RuntimeState,
    RuntimeWakeResult,
)

NOW = datetime(2026, 7, 26, 5, 30, tzinfo=UTC)


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-alpha",
        operation_id="operation-alpha",
        question="question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW,
    )


def operation(ingress_request: IngressRequest) -> IngressOperationResult:
    return IngressOperationResult(
        operation_id=ingress_request.operation_id,
        interaction_id=ingress_request.interaction_id,
        request_sort_key=ingress_request_sort_key(ingress_request),
        status=IngressStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_runtime_get_is_strongly_consistent() -> None:
    sdk = client()
    repository = DynamoDbRuntimeStateRepository(client=sdk, table_name="test-table")
    state = RuntimeState.stopped(at=NOW)

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_runtime_state(state))},
            {
                "TableName": "test-table",
                "Key": marshal_item({"PK": "CONTROL#RUNTIME", "SK": "STATE"}),
                "ConsistentRead": True,
            },
        )

        assert await repository.get() == state
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_initial_wake_transaction_targets_four_distinct_items() -> None:
    sdk = client()
    repository = DynamoDbRuntimeStateRepository(client=sdk, table_name="test-table")
    ingress_request = request()
    ingress_operation = operation(ingress_request)
    updated = RuntimeState.stopped(at=NOW).request_wake(at=NOW)
    wake = RuntimeWakeResult(
        interaction_id=ingress_operation.interaction_id,
        generation=updated.generation,
        runtime_version=updated.version,
        recorded_at=NOW,
    )
    operation_key = {
        "PK": "INGRESS_OPERATION#interaction-alpha",
        "SK": "RESULT",
    }
    wake_key = {
        "PK": "INGRESS_OPERATION#interaction-alpha",
        "SK": "RUNTIME_WAKE",
    }
    runtime_key = {"PK": "CONTROL#RUNTIME", "SK": "STATE"}
    request_key = {
        "PK": "CONTROL#INGRESS",
        "SK": ingress_request_sort_key(ingress_request),
    }

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_ingress_operation_result(ingress_operation))},
            {
                "TableName": "test-table",
                "Key": marshal_item(operation_key),
                "ConsistentRead": True,
            },
        )
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(serialize_ingress_request(ingress_request))},
            {
                "TableName": "test-table",
                "Key": marshal_item(request_key),
                "ConsistentRead": True,
            },
        )
        stubber.add_response(
            "get_item",
            {},
            {
                "TableName": "test-table",
                "Key": marshal_item(wake_key),
                "ConsistentRead": True,
            },
        )
        stubber.add_response(
            "get_item",
            {},
            {
                "TableName": "test-table",
                "Key": marshal_item(runtime_key),
                "ConsistentRead": True,
            },
        )
        stubber.add_response(
            "transact_write_items",
            {},
            {
                "TransactItems": [
                    {
                        "ConditionCheck": {
                            "TableName": "test-table",
                            "Key": marshal_item(operation_key),
                            "ConditionExpression": (
                                "record_type=:operation_type AND schema_version=:schema "
                                "AND record_schema_version=:record_schema "
                                "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                                "AND request_sort_key=:request_sort_key AND created_at=:created_at "
                                "AND #operation_status IN (:pending,:claimed,:retrying)"
                            ),
                            "ExpressionAttributeNames": {"#operation_status": "status"},
                            "ExpressionAttributeValues": ANY,
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": "test-table",
                            "Key": marshal_item(request_key),
                            "ConditionExpression": (
                                "record_type=:request_type AND schema_version=:schema "
                                "AND record_schema_version=:record_schema "
                                "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                                "AND created_at=:created_at "
                                "AND #request_status IN (:pending,:claimed,:retrying) "
                                "AND terminal_deadline_at > :at"
                            ),
                            "ExpressionAttributeNames": {"#request_status": "status"},
                            "ExpressionAttributeValues": ANY,
                        }
                    },
                    {
                        "Put": {
                            "TableName": "test-table",
                            "Item": marshal_item(serialize_runtime_wake_result(wake)),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": "test-table",
                            "Item": marshal_item(serialize_runtime_state(updated)),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                ],
                "ClientRequestToken": ANY,
                "ReturnConsumedCapacity": "NONE",
            },
        )

        assert (
            await repository.request_wake(
                interaction_id="interaction-alpha",
                at=NOW,
            )
            == updated
        )
        stubber.assert_no_pending_responses()

    assert (
        len(
            {
                tuple(operation_key.values()),
                tuple(request_key.values()),
                tuple(wake_key.values()),
                tuple(runtime_key.values()),
            }
        )
        == 4
    )
