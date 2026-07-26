"""Contracts for independent ingress records in the shared DynamoDB table."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.adapters.dynamodb import (
    CURRENT_SCHEMA_VERSION,
    ItemTooLarge,
    PersistenceFormatError,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
)
from shittim_chest.application import (
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
)

NOW = datetime(2026, 7, 26, 4, 5, 6, 789, tzinfo=UTC)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-0001",
        operation_id="interaction-0001",
        question="Which sweet breakfast should I choose?",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW,
    )


def test_ingress_request_round_trip_has_fifo_and_independent_schema_keys() -> None:
    source = request()

    item = serialize_ingress_request(source)

    assert item["PK"] == "CONTROL#INGRESS"
    assert item["SK"] == "REQUEST#2026-07-26T04:05:06.000789Z#interaction-0001"
    assert item["gsi2pk"] == "INGRESS#ACTIVE"
    assert item["gsi2sk"] == item["SK"]
    assert item["schema_version"] == CURRENT_SCHEMA_VERSION
    assert item["record_schema_version"] == 1
    assert deserialize_ingress_request(item) == source


def test_inactive_ingress_request_is_removed_from_active_index() -> None:
    source = request()
    failed = replace(
        source,
        status=IngressStatus.FAILED,
        completed_at=NOW + timedelta(minutes=15),
        updated_at=NOW + timedelta(minutes=15),
        error_code="startup_deadline_exceeded",
    )

    item = serialize_ingress_request(failed)

    assert "gsi2pk" not in item
    assert "gsi2sk" not in item
    assert deserialize_ingress_request(item) == failed


def test_ingress_operation_round_trip_binds_the_request_sort_key() -> None:
    source = request()
    operation = IngressOperationResult(
        operation_id="operation-0001",
        interaction_id=source.interaction_id,
        request_sort_key=ingress_request_sort_key(source),
        status=source.status,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )

    item = serialize_ingress_operation_result(operation)

    assert item["PK"] == f"INGRESS_OPERATION#{source.interaction_id}"
    assert item["SK"] == "RESULT"
    assert item["schema_version"] == CURRENT_SCHEMA_VERSION
    assert item["record_schema_version"] == 1
    assert deserialize_ingress_operation_result(item) == operation


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", CURRENT_SCHEMA_VERSION - 1, "shared table schema"),
        ("record_schema_version", 2, "auxiliary record schema"),
        ("PK", "CONTROL#OTHER", "partition key"),
        ("gsi2pk", "OTHER", "index key"),
    ],
)
def test_ingress_schema_and_keys_fail_closed(
    field: str,
    value: str | int,
    message: str,
) -> None:
    item = {**serialize_ingress_request(request()), field: value}

    with pytest.raises(PersistenceFormatError, match=message):
        deserialize_ingress_request(item)


def test_ingress_serializer_rejects_records_larger_than_400_kb() -> None:
    oversized = replace(request(), requester_display_name="x" * (400 * 1024))

    with pytest.raises(ItemTooLarge, match="400 KB"):
        serialize_ingress_request(oversized)
