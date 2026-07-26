"""SDK shape checks for strongly consistent runtime activity inspection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.ingress import INGRESS_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.outbox import OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
    GLOBAL_LEASE_SLOTS,
)
from shittim_chest.adapters.dynamodb.runtime_activity import (
    DynamoDbRuntimeActivityInspector,
)
from shittim_chest.adapters.dynamodb.runtime_state import (
    RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
    RUNTIME_ACTIVITY_SCHEMA_VERSION,
    DynamoDbRuntimeStateRepository,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    serialize_runtime_state,
)
from shittim_chest.application import RuntimeState, RuntimeStatus
from shittim_chest.application.ports import IngressRepository, RepositoryConflict

NOW = datetime(2026, 7, 27, 3, 0, tzinfo=UTC)
TABLE_NAME = "test-table"


def _client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def _keys() -> tuple[DynamoItem, ...]:
    return (
        {"PK": "CONTROL#RUNTIME", "SK": "ACTIVITY_SCHEMA"},
        {"PK": "CONTROL#INGRESS", "SK": "COUNTER"},
        {"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"},
        {"PK": "CONTROL#PANEL_REFRESH", "SK": "PENDING_COUNT"},
        {"PK": "CONTROL#OUTBOX", "SK": "ACTIVITY"},
        {"PK": "CONTROL#DEBATE", "SK": "ACTIVE_ATTEMPT_COUNT"},
        *({"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{slot}"} for slot in range(GLOBAL_LEASE_SLOTS)),
    )


def _zero_items() -> tuple[DynamoItem, ...]:
    return (
        {
            **_keys()[0],
            "record_type": RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": RUNTIME_ACTIVITY_SCHEMA_VERSION,
        },
        {
            **_keys()[1],
            "record_type": "ingress_queue_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": INGRESS_RECORD_SCHEMA_VERSION,
            "count": 0,
        },
        {
            **_keys()[2],
            "record_type": "ingress_status_pending_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": INGRESS_RECORD_SCHEMA_VERSION,
            "count": 0,
        },
        {
            **_keys()[3],
            "record_type": "panel_refresh_pending_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "count": 0,
        },
        {
            **_keys()[4],
            "record_type": "outbox_activity_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
            "pending_count": 0,
            "claimed_count": 0,
        },
        {
            **_keys()[5],
            "record_type": "active_attempt_counter",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
            "count": 0,
        },
    )


def _inspector(client: DynamoDBClient) -> DynamoDbRuntimeActivityInspector:
    return DynamoDbRuntimeActivityInspector(
        client=client,
        table_name=TABLE_NAME,
        ingress=cast(IngressRepository, object()),
    )


def _expected_request() -> dict[str, object]:
    return {
        "TransactItems": [
            {
                "Get": {
                    "TableName": TABLE_NAME,
                    "Key": marshal_item(key),
                }
            }
            for key in _keys()
        ],
        "ReturnConsumedCapacity": "NONE",
    }


def _idle_state() -> RuntimeState:
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )
    return ready.begin_idle(at=NOW + timedelta(seconds=4))


def test_durable_activity_reads_marker_counters_and_slots_in_one_transaction() -> None:
    sdk = _client()
    responses = [
        *({"Item": marshal_item(item)} for item in _zero_items()),
        *({} for _slot in range(GLOBAL_LEASE_SLOTS)),
    ]

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {"Responses": responses},
            _expected_request(),
        )

        durable = _inspector(sdk)._read_durable(NOW)

        assert durable.active_ingress == 0
        assert durable.active_attempts == 0
        assert durable.pending_status == 0
        assert durable.pending_panel_refreshes == 0
        assert durable.pending_outbox == 0
        assert durable.claimed_outbox == 0
        assert durable.active_leases == 0
        assert durable.expired_leases == 0
        stubber.assert_no_pending_responses()


def test_durable_activity_fails_closed_when_marker_is_missing() -> None:
    sdk = _client()
    items = _zero_items()
    responses = [
        {},
        *({"Item": marshal_item(item)} for item in items[1:]),
        *({} for _slot in range(GLOBAL_LEASE_SLOTS)),
    ]

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {"Responses": responses},
            _expected_request(),
        )

        with pytest.raises(RepositoryConflict, match="schema marker"):
            _inspector(sdk)._read_durable(NOW)
        stubber.assert_no_pending_responses()


def test_stop_transaction_fences_marker_all_counters_and_three_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _client()
    repository = DynamoDbRuntimeStateRepository(client=sdk, table_name=TABLE_NAME)
    idle = _idle_state()
    stop_at = idle.stop_eligible_at
    assert stop_at is not None
    stopping = idle.begin_idle_stop(at=stop_at)
    captured: dict[str, Any] = {}

    def capture_transaction(**kwargs: Any) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(sdk, "transact_write_items", capture_transaction)

    assert (
        repository._transact_stop(
            expected=idle,
            updated=stopping,
            transaction_kind="test-stop",
        )
        == stopping
    )
    actions = cast(list[dict[str, Any]], captured["TransactItems"])
    assert actions[0]["Put"]["Item"] == marshal_item(serialize_runtime_state(stopping))
    checks = actions[1:]
    assert len(checks) == 9
    assert [check["ConditionCheck"]["Key"] for check in checks] == [
        marshal_item(key) for key in _keys()
    ]
    assert "attribute_not_exists(PK)" not in checks[0]["ConditionCheck"]["ConditionExpression"]
    assert all(
        "attribute_not_exists(PK)" not in check["ConditionCheck"]["ConditionExpression"]
        for check in checks[1:6]
    )
    assert (
        "attribute_not_exists(record_schema_version)"
        in checks[3]["ConditionCheck"]["ConditionExpression"]
    )
    assert all(
        "attribute_not_exists(PK)" in check["ConditionCheck"]["ConditionExpression"]
        for check in checks[6:]
    )
