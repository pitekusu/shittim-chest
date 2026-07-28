"""DynamoDB Local adoption test for deployment-owned control records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    CONTROL_RECORD_MANIFEST,
    CURRENT_SCHEMA_VERSION,
    ControlRecordInitializationStatus,
    DynamoDbControlRecordInitializer,
    DynamoDbIngressRepository,
    DynamoDbRuntimeStateRepository,
    serialize_runtime_state,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import PREVIOUS_SCHEMA_VERSION, DynamoItem
from shittim_chest.application import IngressRequest, RuntimeState, RuntimeStatus

NOW = datetime(2026, 7, 28, 6, 0, tzinfo=UTC)


def _slot_number(item: DynamoItem) -> int:
    value = item.get("slot")
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="initializer-integration-interaction",
        operation_id="initializer-integration-operation",
        application_id="application-id",
        question="migration integration question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_previous_idle_controls_migrate_then_enqueue_and_wake(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    timestamp = "2026-07-28T05:59:00.000000Z"
    previous_records: list[DynamoItem] = []
    for spec in CONTROL_RECORD_MANIFEST.activity_records[1:]:
        item = {
            **spec.install_item,
            "schema_version": PREVIOUS_SCHEMA_VERSION,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if spec.slot is not None:
            item["fencing_token"] = 20 + spec.slot
        previous_records.append(item)
    stopped = RuntimeState(
        status=RuntimeStatus.STOPPED,
        generation=4,
        desired_count=0,
        version=9,
        updated_at=NOW,
        stopped_at=NOW,
    )
    previous_runtime = {
        **serialize_runtime_state(stopped),
        "schema_version": PREVIOUS_SCHEMA_VERSION,
    }
    for item in (*previous_records, previous_runtime):
        dynamodb_client.put_item(
            TableName=empty_dynamodb_table,
            Item=marshal_item(item),
        )

    result = DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()

    assert result.status is ControlRecordInitializationStatus.INITIALIZED
    response = dynamodb_client.scan(
        TableName=empty_dynamodb_table,
        ConsistentRead=True,
    )
    items = [unmarshal_item(item) for item in response["Items"]]
    assert len(items) == 10
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in items)
    migrated_counter = next(
        item for item in items if item.get("record_type") == "ingress_queue_counter"
    )
    assert migrated_counter["created_at"] == timestamp
    assert migrated_counter["updated_at"] == timestamp
    slots = sorted(
        (item for item in items if item.get("record_type") == "lease_slot"),
        key=_slot_number,
    )
    assert [item["fencing_token"] for item in slots] == [20, 21, 22]
    migrated_runtime = next(item for item in items if item.get("record_type") == "runtime_state")
    assert migrated_runtime["generation"] == 4
    assert migrated_runtime["version"] == 9

    ingress = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    runtime = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    request = _request()
    assert (await ingress.enqueue(request)).created

    started = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=NOW + timedelta(seconds=2),
    )

    assert started.status is RuntimeStatus.STARTING
    assert started.desired_count == 1
    assert started.generation == 5
    assert started.version == 10
