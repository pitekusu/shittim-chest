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
from shittim_chest.adapters.dynamodb.serializer import (
    deserialize_ingress_active_pointer,
    deserialize_ingress_semantic_binding,
    deserialize_ingress_status_publication,
    serialize_ingress_active_pointer,
    serialize_ingress_semantic_binding,
    serialize_ingress_status_publication,
)
from shittim_chest.application import (
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
)
from shittim_chest.application.scale_to_zero import (
    IngressSemanticOperationBinding,
    IngressStatusPublication,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationState,
)
from shittim_chest.application.status_publication import render_public_status
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 4, 5, 6, 789, tzinfo=UTC)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-0001",
        operation_id="interaction-0001",
        application_id="application-id",
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
    assert "gsi2pk" not in item
    assert "gsi2sk" not in item
    assert item["schema_version"] == CURRENT_SCHEMA_VERSION
    assert item["record_schema_version"] == 1
    assert item["application_id"] == "application-id"
    assert item["requester_can_manage_messages"] is False
    assert item["status_message_state"] == StatusMessageState.STARTING.value
    assert "token" not in item
    assert deserialize_ingress_request(item) == source


def test_active_pointer_round_trip_is_fifo_ordered_and_contains_no_pii() -> None:
    source = request()

    item = serialize_ingress_active_pointer(source)

    assert item == {
        "PK": "CONTROL#INGRESS#ACTIVE",
        "SK": "REQUEST#2026-07-26T04:05:06.000789Z#interaction-0001",
        "record_type": "ingress_active_pointer",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": 1,
        "interaction_id": "interaction-0001",
        "request_sort_key": "REQUEST#2026-07-26T04:05:06.000789Z#interaction-0001",
        "created_at": "2026-07-26T04:05:06.000789Z",
    }
    assert "question" not in item
    assert "requester_id" not in item
    assert "guild_id" not in item
    assert "ttl" not in item
    assert deserialize_ingress_active_pointer(item).request_sort_key == item["SK"]

    with pytest.raises(PersistenceFormatError, match="targets another request"):
        deserialize_ingress_active_pointer({**item, "request_sort_key": "REQUEST#other"})


def test_control_request_round_trip_preserves_immutable_authorization_context() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    source = IngressRequest.control_operation(
        interaction_id="component-interaction",
        operation_id="semantic-operation",
        kind=IngressKind.CANCEL,
        application_id="application-id",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        requester_can_manage_messages=True,
        guild_id="guild-id",
        channel_id="thread-id",
        parent_channel_id="channel-id",
        source_message_id="panel-message-id",
        source_thread_id="thread-id",
        target_debate_id=debate_id,
        expected_attempt_id=attempt_id,
        custom_id=f"shittim:cancel:{debate_id}:{attempt_id}",
        created_at=NOW,
    )

    item = serialize_ingress_request(source)

    assert item["parent_channel_id"] == "channel-id"
    assert item["target_debate_id"] == str(debate_id)
    assert item["expected_attempt_id"] == str(attempt_id)
    assert item["requester_can_manage_messages"] is True
    assert "token" not in item
    assert deserialize_ingress_request(item) == source


def test_inactive_ingress_request_does_not_use_the_recoverable_debate_index() -> None:
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


def test_semantic_binding_round_trip_is_fail_closed() -> None:
    source = request()
    binding = IngressSemanticOperationBinding(
        operation_id="semantic-operation",
        canonical_interaction_id=source.interaction_id,
        request_sort_key=ingress_request_sort_key(source),
        created_at=NOW,
    )
    item = serialize_ingress_semantic_binding(binding)

    assert item["PK"] == "INGRESS_SEMANTIC_OPERATION#semantic-operation"
    assert item["SK"] == "BINDING"
    assert deserialize_ingress_semantic_binding(item) == binding
    with pytest.raises(PersistenceFormatError, match="partition key"):
        deserialize_ingress_semantic_binding({**item, "PK": "OTHER"})


def test_prepared_status_publication_round_trip_separates_desired_and_delivered() -> None:
    source_request = request()
    source = IngressStatusPublication.prepared(
        source_request,
        content=render_public_status(source_request, StatusMessageState.STARTING),
    )
    item = serialize_ingress_status_publication(source)

    assert item["PK"] == "INGRESS_OPERATION#interaction-0001"
    assert item["SK"] == "STATUS_PUBLICATION"
    assert item["publication_state"] == StatusPublicationState.PREPARED.value
    assert item["desired_state"] == StatusMessageState.STARTING.value
    assert item["record_schema_version"] == 3
    assert "delivered_state" not in item
    assert item["gsi1pk"] == "INGRESS#STATUS_DUE"
    assert len(str(item["nonce"])) == 22
    assert deserialize_ingress_status_publication(item) == source

    with pytest.raises(PersistenceFormatError, match="due index sort key"):
        deserialize_ingress_status_publication({**item, "gsi1sk": "wrong"})
    with pytest.raises(PersistenceFormatError, match="auxiliary record schema"):
        deserialize_ingress_status_publication({**item, "record_schema_version": 2})

    numeric_request = replace(
        source_request,
        interaction_id="300",
        operation_id="300",
    )
    numeric = IngressStatusPublication.prepared(
        numeric_request,
        content=render_public_status(numeric_request, StatusMessageState.STARTING),
    )
    scanning = replace(
        numeric,
        history_checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="500",
            history_verified_head_message_id="700",
            history_gap_cursor_message_id="800",
            history_gap_upper_message_id="900",
        ),
        history_reconciliation_required=True,
    )
    scanning_item = serialize_ingress_status_publication(scanning)
    assert scanning_item["history_cursor_message_id"] == "500"
    assert scanning_item["history_verified_head_message_id"] == "700"
    assert scanning_item["history_gap_cursor_message_id"] == "800"
    assert scanning_item["history_gap_upper_message_id"] == "900"
    assert deserialize_ingress_status_publication(scanning_item) == scanning
    with pytest.raises(ValueError, match="follow the interaction"):
        replace(
            numeric,
            history_checkpoint=StatusHistoryCheckpoint(
                history_cursor_message_id="200",
                history_verified_head_message_id="700",
            ),
            history_reconciliation_required=True,
        )
    missing_head = dict(scanning_item)
    del missing_head["history_verified_head_message_id"]
    with pytest.raises(PersistenceFormatError, match="verified head"):
        deserialize_ingress_status_publication(missing_head)

    delivered = replace(
        source,
        state=StatusPublicationState.DELIVERED,
        delivered_state=StatusMessageState.STARTING,
        status_message_id="status-message-id",
        status_message_updated_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=1),
        next_attempt_at=None,
    )
    delivered_item = serialize_ingress_status_publication(delivered)
    assert delivered_item["desired_state"] == StatusMessageState.STARTING.value
    assert delivered_item["delivered_state"] == StatusMessageState.STARTING.value
    assert "gsi1pk" not in delivered_item
    assert "gsi1sk" not in delivered_item
    assert deserialize_ingress_status_publication(delivered_item) == delivered


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", CURRENT_SCHEMA_VERSION - 1, "shared table schema"),
        ("record_schema_version", 2, "auxiliary record schema"),
        ("PK", "CONTROL#OTHER", "partition key"),
        ("gsi2pk", "OTHER", "recoverable debate index"),
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
