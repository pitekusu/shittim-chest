"""Low-level DynamoDB request shapes for control-record initialization."""

from __future__ import annotations

from typing import Any, cast

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

import shittim_chest.adapters.dynamodb.control_records as control_records
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.control_records import (
    CONTROL_RECORD_MANIFEST,
    ControlRecordInitializationStatus,
    DynamoDbControlRecordInitializer,
)
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

TABLE_NAME = "control-record-test"


def _client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def _items() -> tuple[DynamoItem, ...]:
    return (
        *(spec.install_item for spec in CONTROL_RECORD_MANIFEST.activity_records),
        CONTROL_RECORD_MANIFEST.initial_runtime_item,
    )


def _get_request() -> dict[str, object]:
    return {
        "TransactItems": [
            {
                "Get": {
                    "TableName": TABLE_NAME,
                    "Key": marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                }
            }
            for item in _items()
        ],
        "ReturnConsumedCapacity": "NONE",
    }


def _scan_request() -> dict[str, object]:
    return {
        "TableName": TABLE_NAME,
        "ConsistentRead": True,
        "Limit": 100,
        "ProjectionExpression": ",".join(
            f"#f{index}" for index in range(len(control_records._LEGACY_SCAN_FIELDS))
        ),
        "ExpressionAttributeNames": {
            f"#f{index}": field for index, field in enumerate(control_records._LEGACY_SCAN_FIELDS)
        },
        "ReturnConsumedCapacity": "NONE",
    }


def test_installed_manifest_is_one_ten_item_transactional_snapshot() -> None:
    sdk = _client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {"Responses": [{"Item": marshal_item(item)} for item in _items()]},
            _get_request(),
        )

        result = DynamoDbControlRecordInitializer(
            client=sdk,
            table_name=TABLE_NAME,
        ).initialize()

        assert result.status is ControlRecordInitializationStatus.ALREADY_INITIALIZED
        stubber.assert_no_pending_responses()


def test_first_install_paginates_scan_and_uses_one_atomic_ten_action_write() -> None:
    sdk = _client()
    empty_snapshot: tuple[DynamoItem | None, ...] = (None,) * 10
    last_key = marshal_item({"PK": "DEBATE#historical", "SK": "META"})
    historical = marshal_item(
        {
            "PK": "DEBATE#historical",
            "SK": "META",
            "record_type": "debate_meta",
            "schema_version": control_records.PREVIOUS_SCHEMA_VERSION,
            "debate_id": "historical",
            "current_attempt_id": "historical-attempt",
            "current_phase": "completed",
            "created_at": "2026-07-28T04:00:00.000000Z",
            "updated_at": "2026-07-28T04:00:00.000000Z",
        }
    )
    expected_actions = [
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": marshal_item(item),
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        }
        for item in _items()
    ]
    expected_write = {
        "TransactItems": expected_actions,
        "ClientRequestToken": control_records._client_token(TABLE_NAME, empty_snapshot),
        "ReturnConsumedCapacity": "NONE",
    }

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {"Responses": [{} for _index in range(10)]},
            _get_request(),
        )
        stubber.add_response(
            "scan",
            {"Items": [historical], "ScannedCount": 1, "LastEvaluatedKey": last_key},
            _scan_request(),
        )
        second_scan = {**_scan_request(), "ExclusiveStartKey": last_key}
        stubber.add_response("scan", {"Items": [], "ScannedCount": 0}, second_scan)
        stubber.add_response(
            "transact_write_items",
            {},
            cast(dict[str, Any], expected_write),
        )

        result = DynamoDbControlRecordInitializer(
            client=sdk,
            table_name=TABLE_NAME,
        ).initialize()

        assert result.status is ControlRecordInitializationStatus.INITIALIZED
        stubber.assert_no_pending_responses()
