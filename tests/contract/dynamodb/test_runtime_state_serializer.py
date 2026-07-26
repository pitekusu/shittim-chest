"""Contracts for the singleton runtime state and immutable wake records."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.adapters.dynamodb import (
    CURRENT_SCHEMA_VERSION,
    ItemTooLarge,
    PersistenceFormatError,
    deserialize_runtime_state,
    deserialize_runtime_wake_result,
    serialize_runtime_state,
    serialize_runtime_wake_result,
)
from shittim_chest.application import (
    RuntimeState,
    RuntimeStatus,
    RuntimeWakeResult,
)

NOW = datetime(2026, 7, 26, 5, 0, tzinfo=UTC)


def ready_state() -> RuntimeState:
    return (
        RuntimeState.stopped(at=NOW)
        .request_wake(at=NOW + timedelta(seconds=1))
        .mark_started(
            at=NOW + timedelta(seconds=2),
            runtime_instance_id="runtime-alpha",
        )
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=3),
            runtime_instance_id="runtime-alpha",
        )
        .record_reconciled(at=NOW + timedelta(seconds=4))
    )


def test_runtime_state_round_trip_uses_canonical_singleton_key() -> None:
    source = ready_state()

    item = serialize_runtime_state(source)

    assert item["PK"] == "CONTROL#RUNTIME"
    assert item["SK"] == "STATE"
    assert item["state"] == "ready"
    assert item["schema_version"] == CURRENT_SCHEMA_VERSION
    assert item["record_schema_version"] == 1
    assert "ttl" not in item
    assert "gsi1pk" not in item
    assert "gsi2pk" not in item
    assert deserialize_runtime_state(item) == source


def test_persisted_timestamps_are_fixed_width_and_lexically_ordered() -> None:
    zero = serialize_runtime_state(RuntimeState.stopped(at=NOW))["updated_at"]
    half_second = serialize_runtime_state(
        RuntimeState.stopped(at=NOW + timedelta(microseconds=500_000))
    )["updated_at"]

    assert zero == "2026-07-26T05:00:00.000000Z"
    assert half_second == "2026-07-26T05:00:00.500000Z"
    assert isinstance(zero, str)
    assert isinstance(half_second, str)
    assert zero < half_second


def test_runtime_wake_result_round_trip_is_keyed_by_interaction_id() -> None:
    source = RuntimeWakeResult(
        interaction_id="interaction-alpha",
        generation=3,
        runtime_version=7,
        recorded_at=NOW,
    )

    item = serialize_runtime_wake_result(source)

    assert item["PK"] == "INGRESS_OPERATION#interaction-alpha"
    assert item["SK"] == "RUNTIME_WAKE"
    assert item["record_type"] == "runtime_wake_result"
    assert deserialize_runtime_wake_result(item) == source


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", CURRENT_SCHEMA_VERSION - 1, "shared table schema"),
        ("record_schema_version", 2, "auxiliary record schema"),
        ("PK", "CONTROL#OTHER", "invalid key"),
        ("state", "unknown", "invalid runtime state"),
        ("generation", -1, "invalid runtime state"),
    ],
)
def test_runtime_state_schema_and_shape_fail_closed(
    field: str,
    value: str | int,
    message: str,
) -> None:
    item = {**serialize_runtime_state(ready_state()), field: value}

    with pytest.raises(PersistenceFormatError, match=message):
        deserialize_runtime_state(item)


def test_runtime_wake_result_key_and_generation_fail_closed() -> None:
    source = RuntimeWakeResult(
        interaction_id="interaction-alpha",
        generation=1,
        runtime_version=1,
        recorded_at=NOW,
    )
    item = serialize_runtime_wake_result(source)

    with pytest.raises(PersistenceFormatError, match="partition key"):
        deserialize_runtime_wake_result({**item, "PK": "INGRESS_OPERATION#other"})
    with pytest.raises(PersistenceFormatError, match="invalid runtime wake result"):
        deserialize_runtime_wake_result({**item, "generation": 0})


def test_runtime_state_serializer_enforces_item_size_limit() -> None:
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))
    oversized = starting.transition(
        RuntimeStatus.DEGRADED,
        at=NOW + timedelta(seconds=2),
        error_code="x" * (400 * 1024),
    )

    with pytest.raises(ItemTooLarge, match="400 KB"):
        serialize_runtime_state(oversized)


def test_runtime_state_rejects_inconsistent_active_shape_before_serialization() -> None:
    with pytest.raises(ValueError, match="runtime instance"):
        replace(
            ready_state(),
            runtime_instance_id=None,
        )
