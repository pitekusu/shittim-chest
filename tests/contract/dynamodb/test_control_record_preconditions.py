"""SDK contracts for deployment-owned DynamoDB control records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.adapters.dynamodb.outbox import (
    DynamoDbOutboxRepository,
    outbox_activity_action,
)
from shittim_chest.adapters.dynamodb.repository import (
    GLOBAL_LEASE_SLOTS,
    DynamoDbDebateRepository,
)
from shittim_chest.adapters.dynamodb.serializer import CURRENT_SCHEMA_VERSION, DynamoItem
from shittim_chest.application.ports import RepositoryConflict

NOW = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)
TABLE_NAME = "test-table"


def _client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def _update(action: object) -> dict[str, Any]:
    return cast(dict[str, Any], action)["Update"]


def test_counter_increments_require_preexisting_exact_control_records() -> None:
    sdk = _client()
    ingress = DynamoDbIngressRepository(client=sdk, table_name=TABLE_NAME)
    debates = DynamoDbDebateRepository(client=sdk, table_name=TABLE_NAME)
    actions = (
        ingress._increment_counter_action(NOW),
        ingress._increment_status_counter_action(NOW),
        debates._panel_refresh_count_action(1, NOW),
        debates._active_attempt_count_action(1, NOW),
        outbox_activity_action(
            table_name=TABLE_NAME,
            pending_delta=1,
            claimed_delta=0,
            at=NOW,
        ),
    )

    for action in actions:
        update = _update(action)
        assert "if_not_exists" not in update["UpdateExpression"]
        assert "attribute_not_exists(PK)" not in update["ConditionExpression"]
        assert "record_type=:type" in update["ConditionExpression"]
        assert "schema_version=:schema" in update["ConditionExpression"]


def test_counter_reads_reject_missing_control_records() -> None:
    sdk = _client()
    ingress = DynamoDbIngressRepository(client=sdk, table_name=TABLE_NAME)
    debates = DynamoDbDebateRepository(client=sdk, table_name=TABLE_NAME)
    outbox = DynamoDbOutboxRepository(client=sdk, table_name=TABLE_NAME)
    keys = (
        {"PK": "CONTROL#INGRESS", "SK": "COUNTER"},
        {"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"},
        {"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"},
        {"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"},
        {"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"},
    )

    with Stubber(sdk) as stubber:
        for key in keys:
            stubber.add_response(
                "get_item",
                {},
                {
                    "TableName": TABLE_NAME,
                    "Key": marshal_item(key),
                    "ConsistentRead": True,
                },
            )

        with pytest.raises(RepositoryConflict, match="ingress counter is missing"):
            ingress._active_count()
        with pytest.raises(RepositoryConflict, match="publication counter is missing"):
            ingress._pending_status_count()
        with pytest.raises(RepositoryConflict, match="panel refresh counter is missing"):
            debates._pending_panel_refresh_count()
        with pytest.raises(RepositoryConflict, match="active attempt counter is missing"):
            debates._active_attempt_count()
        with pytest.raises(RepositoryConflict, match="outbox activity counter is missing"):
            outbox._activity()
        stubber.assert_no_pending_responses()


def test_slot_acquisition_rejects_a_missing_deployment_owned_slot() -> None:
    sdk = _client()
    repository = DynamoDbDebateRepository(client=sdk, table_name=TABLE_NAME)

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_item",
            {},
            {
                "TableName": TABLE_NAME,
                "Key": marshal_item({"PK": "CONTROL#GLOBAL", "SK": "SLOT#0"}),
                "ConsistentRead": True,
            },
        )

        with pytest.raises(RepositoryConflict, match="global lease slot 0 is missing"):
            repository._slot_candidates("worker-alpha", NOW)
        stubber.assert_no_pending_responses()


def test_slot_acquisition_preserves_schema_and_fencing_in_the_transaction() -> None:
    sdk = _client()
    repository = DynamoDbDebateRepository(client=sdk, table_name=TABLE_NAME)

    with Stubber(sdk) as stubber:
        for slot in range(GLOBAL_LEASE_SLOTS):
            key = {"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{slot}"}
            item: DynamoItem = {
                **key,
                "record_type": "lease_slot",
                "schema_version": CURRENT_SCHEMA_VERSION,
                "slot": slot,
                "fencing_token": 0,
            }
            stubber.add_response(
                "get_item",
                {"Item": marshal_item(item)},
                {
                    "TableName": TABLE_NAME,
                    "Key": marshal_item(key),
                    "ConsistentRead": True,
                },
            )

        candidates = repository._slot_candidates("worker-alpha", NOW)
        assert len(candidates) == GLOBAL_LEASE_SLOTS
        for candidate in candidates:
            update = _update(candidate.action)
            condition = update["ConditionExpression"]
            assert "record_type=:type" in condition
            assert "schema_version=:schema" in condition
            assert "slot=:slot" in condition
            assert "fencing_token=:previous" in condition
            assert "attribute_not_exists(PK)" not in condition
        stubber.assert_no_pending_responses()
