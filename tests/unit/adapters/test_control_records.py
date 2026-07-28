"""Fail-closed control-record manifest tests without an AWS endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

import shittim_chest.adapters.dynamodb.control_records as control_records
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.control_records import (
    CONTROL_RECORD_MANIFEST,
    CONTROL_RECORD_MANIFEST_HASH,
    CONTROL_RECORD_MANIFEST_VERSION,
    ControlRecordInitializationError,
    ControlRecordInitializationStatus,
    ControlRecordMigrationRequired,
    DynamoDbControlRecordInitializer,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    serialize_runtime_state,
)
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus

TABLE_NAME = "control-record-test"
NOW = datetime(2026, 7, 28, 4, 5, 6, tzinfo=UTC)
LEGACY_CREATED_AT = "2026-07-28T04:00:00.000000Z"
LEGACY_STARTUP_DEADLINE = "2026-07-28T04:03:00.000000Z"
LEGACY_TERMINAL_DEADLINE = "2026-07-28T04:15:00.000000Z"
DISCORD_MESSAGE_ID = "123"


class TransactionCanceledException(Exception):
    pass


class FakeClient:
    def __init__(
        self,
        *,
        snapshots: list[tuple[DynamoItem | None, ...]],
        pages: list[dict[str, object]] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.pages: list[dict[str, object]] = list(
            pages if pages is not None else [{"Items": [], "ScannedCount": 0}]
        )
        self.scan_requests: list[dict[str, object]] = []
        self.transact_get_requests: list[dict[str, object]] = []
        self.transact_write_requests: list[dict[str, object]] = []
        self.cancel_write = False
        self.exceptions = SimpleNamespace(
            TransactionCanceledException=TransactionCanceledException,
        )

    def transact_get_items(self, **kwargs: object) -> dict[str, object]:
        self.transact_get_requests.append(kwargs)
        snapshot = self.snapshots.pop(0)
        return {
            "Responses": [{} if item is None else {"Item": marshal_item(item)} for item in snapshot]
        }

    def scan(self, **kwargs: object) -> dict[str, object]:
        self.scan_requests.append(kwargs)
        return self.pages.pop(0)

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        self.transact_write_requests.append(kwargs)
        if self.cancel_write:
            raise TransactionCanceledException
        return {}


def _snapshot(*, marker: bool = True) -> tuple[DynamoItem | None, ...]:
    items: list[DynamoItem | None] = [
        spec.install_item for spec in CONTROL_RECORD_MANIFEST.activity_records
    ]
    if not marker:
        items[0] = None
    return (
        *items,
        CONTROL_RECORD_MANIFEST.initial_runtime_item,
        CONTROL_RECORD_MANIFEST.initial_deployment_lock_item,
    )


def _initializer(client: FakeClient) -> DynamoDbControlRecordInitializer:
    return DynamoDbControlRecordInitializer(
        client=cast(DynamoDBClient, client),
        table_name=TABLE_NAME,
    )


def _scan_page(
    *items: DynamoItem,
    last_key: DynamoItem | None = None,
    scanned_count: int | None = None,
) -> dict[str, object]:
    page: dict[str, object] = {
        "Items": [marshal_item(item) for item in items],
        "ScannedCount": len(items) if scanned_count is None else scanned_count,
    }
    if last_key is not None:
        page["LastEvaluatedKey"] = marshal_item(last_key)
    return page


def test_manifest_is_typed_deterministic_and_contains_eleven_records() -> None:
    assert CONTROL_RECORD_MANIFEST.version == CONTROL_RECORD_MANIFEST_VERSION == 2
    assert CONTROL_RECORD_MANIFEST.manifest_hash == CONTROL_RECORD_MANIFEST_HASH
    assert len(CONTROL_RECORD_MANIFEST.activity_records) == 9
    assert len(CONTROL_RECORD_MANIFEST_HASH) == 64
    assert CONTROL_RECORD_MANIFEST_HASH == (
        "f4679a4946a61faa79ef02e6bbc3305fe98cddcf803dafccf2e1a3ed41711de0"
    )
    assert control_records._manifest_hash() == CONTROL_RECORD_MANIFEST_HASH
    assert CONTROL_RECORD_MANIFEST.initial_runtime_item == {
        "PK": "CONTROL#RUNTIME",
        "SK": "STATE",
        "record_type": "runtime_state",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": 1,
        "state": "stopped",
        "generation": 0,
        "desired_count": 0,
        "version": 0,
        "updated_at": "1970-01-01T00:00:00.000000Z",
        "stopped_at": "1970-01-01T00:00:00.000000Z",
    }
    assert CONTROL_RECORD_MANIFEST.initial_deployment_lock_item == {
        "PK": "CONTROL#DEPLOYMENT",
        "SK": "LOCK",
        "record_type": "deployment_lock",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": 1,
        "lock_state": "open",
        "fencing_token": 0,
        "version": 0,
        "updated_at": "1970-01-01T00:00:00.000000Z",
    }


def test_client_request_identity_is_scoped_to_the_table_and_snapshot() -> None:
    empty = (None,) * 11

    assert control_records._client_token(TABLE_NAME, empty) == control_records._client_token(
        TABLE_NAME, empty
    )
    assert control_records._client_token(TABLE_NAME, empty) != control_records._client_token(
        "another-table", empty
    )


def test_first_install_scans_all_pages_then_atomically_puts_eleven_records() -> None:
    historical = {
        **_legacy_record("debate_meta", current_phase="completed"),
        "schema_version": PREVIOUS_SCHEMA_VERSION,
    }
    client = FakeClient(
        snapshots=[(None,) * 11],
        pages=[_scan_page(historical), _scan_page()],
    )

    result = _initializer(client).initialize()

    assert result.status is ControlRecordInitializationStatus.INITIALIZED
    assert len(client.transact_get_requests) == 1
    gets = cast(list[dict[str, Any]], client.transact_get_requests[0]["TransactItems"])
    assert len(gets) == 11
    assert client.scan_requests[0]["ConsistentRead"] is True
    assert client.scan_requests[0]["Limit"] == 100
    names = cast(dict[str, str], client.scan_requests[0]["ExpressionAttributeNames"])
    assert {
        "question",
        "content",
        "persona",
        "requester_id",
        "requester_username",
        "requester_display_name",
    }.isdisjoint(names.values())
    assert len(client.transact_write_requests) == 1
    write = client.transact_write_requests[0]
    actions = cast(list[dict[str, Any]], write["TransactItems"])
    assert len(actions) == 11
    assert all("Put" in action for action in actions)
    marker = unmarshal_item(actions[0]["Put"]["Item"])
    assert marker["manifest_hash"] == CONTROL_RECORD_MANIFEST_HASH
    assert marker["manifest_version"] == CONTROL_RECORD_MANIFEST_VERSION
    assert unmarshal_item(actions[9]["Put"]["Item"])["state"] == "stopped"
    request_token = cast(str, write["ClientRequestToken"])
    assert request_token.startswith("cr-")
    assert len(request_token) == 36


def test_first_install_condition_checks_existing_safe_records_without_rewriting() -> None:
    snapshot = list(_snapshot(marker=False))
    counter = cast(DynamoItem, snapshot[1])
    snapshot[1] = {
        **counter,
        "created_at": "2026-07-28T04:00:00.000000Z",
        "updated_at": "2026-07-28T04:00:00.000000Z",
    }
    snapshot[9] = serialize_runtime_state(RuntimeState.stopped(at=NOW))
    client = FakeClient(snapshots=[tuple(snapshot)])

    _initializer(client).initialize()

    actions = cast(
        list[dict[str, Any]],
        client.transact_write_requests[0]["TransactItems"],
    )
    assert "Put" in actions[0]
    assert "ConditionCheck" in actions[1]
    assert "ConditionCheck" in actions[9]
    assert "ConditionCheck" in actions[10]
    state_values = unmarshal_item(actions[9]["ConditionCheck"]["ExpressionAttributeValues"])
    assert "2026-07-28T04:05:06.000000Z" in state_values.values()


def test_first_install_migrates_previous_idle_records_without_resetting_state() -> None:
    snapshot = list(_snapshot(marker=False))
    for index in range(1, 9):
        previous = cast(DynamoItem, snapshot[index])
        snapshot[index] = {**previous, "schema_version": PREVIOUS_SCHEMA_VERSION}
    counter = cast(DynamoItem, snapshot[1])
    snapshot[1] = {
        **counter,
        "created_at": "2026-07-28T04:00:00.000000Z",
        "updated_at": "2026-07-28T04:01:00.000000Z",
    }
    slot = cast(DynamoItem, snapshot[6])
    snapshot[6] = {**slot, "fencing_token": 17}
    runtime = serialize_runtime_state(
        RuntimeState(
            status=RuntimeStatus.STOPPED,
            generation=4,
            desired_count=0,
            version=9,
            updated_at=NOW,
            stopped_at=NOW,
        )
    )
    snapshot[9] = {**runtime, "schema_version": PREVIOUS_SCHEMA_VERSION}
    lock = cast(DynamoItem, snapshot[10])
    snapshot[10] = {**lock, "schema_version": PREVIOUS_SCHEMA_VERSION}
    legacy_items = tuple(item for item in snapshot[1:] if item is not None)
    client = FakeClient(
        snapshots=[tuple(snapshot)],
        pages=[_scan_page(*legacy_items)],
    )

    result = _initializer(client).initialize()

    assert result.status is ControlRecordInitializationStatus.INITIALIZED
    actions = cast(list[dict[str, Any]], client.transact_write_requests[0]["TransactItems"])
    assert len(actions) == 11
    assert all("Put" in action for action in actions)
    migrated = [unmarshal_item(action["Put"]["Item"]) for action in actions[1:]]
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in migrated)
    assert migrated[0]["created_at"] == "2026-07-28T04:00:00.000000Z"
    assert migrated[0]["updated_at"] == "2026-07-28T04:01:00.000000Z"
    assert migrated[5]["fencing_token"] == 17
    assert migrated[-2]["updated_at"] == "2026-07-28T04:05:06.000000Z"
    assert migrated[-2]["generation"] == 4
    assert migrated[-2]["version"] == 9
    assert migrated[-1]["lock_state"] == "open"
    previous_values = unmarshal_item(actions[1]["Put"]["ExpressionAttributeValues"])
    assert PREVIOUS_SCHEMA_VERSION in previous_values.values()


def test_installed_marker_validates_dynamic_state_without_scan_or_write() -> None:
    snapshot = list(_snapshot())
    snapshot[6] = {**cast(DynamoItem, snapshot[6]), "fencing_token": 14}
    snapshot[9] = serialize_runtime_state(RuntimeState.stopped(at=NOW).request_wake(at=NOW))
    client = FakeClient(snapshots=[tuple(snapshot)])

    result = _initializer(client).initialize()

    assert result.status is ControlRecordInitializationStatus.ALREADY_INITIALIZED
    assert client.scan_requests == []
    assert client.transact_write_requests == []


def test_validate_is_strictly_read_only_and_requires_the_complete_manifest() -> None:
    client = FakeClient(snapshots=[_snapshot()])

    result = _initializer(client).validate()

    assert result.status is ControlRecordInitializationStatus.ALREADY_INITIALIZED
    assert client.scan_requests == []
    assert client.transact_write_requests == []

    incomplete = list(_snapshot())
    incomplete[10] = None
    with pytest.raises(ControlRecordInitializationError, match="missing"):
        _initializer(FakeClient(snapshots=[tuple(incomplete)])).validate()


def test_installed_marker_rejects_previous_runtime_schema_without_repair() -> None:
    snapshot = list(_snapshot())
    previous = CONTROL_RECORD_MANIFEST.initial_runtime_item
    # Runtime deserialization intentionally supports exactly previous -> current.
    snapshot[9] = {**previous, "schema_version": PREVIOUS_SCHEMA_VERSION}
    client = FakeClient(snapshots=[tuple(snapshot)])

    with pytest.raises(ControlRecordInitializationError, match="schema"):
        _initializer(client).initialize()

    assert client.transact_write_requests == []


def test_installed_marker_rejects_previous_fixed_record_without_repair() -> None:
    snapshot = list(_snapshot())
    previous = cast(DynamoItem, snapshot[1])
    snapshot[1] = {**previous, "schema_version": PREVIOUS_SCHEMA_VERSION}
    client = FakeClient(snapshots=[tuple(snapshot)])

    with pytest.raises(ControlRecordInitializationError, match="schema"):
        _initializer(client).initialize()

    assert client.scan_requests == []
    assert client.transact_write_requests == []


@pytest.mark.parametrize("index", [1, 8, 9, 10])
def test_installed_marker_never_repairs_a_missing_record(index: int) -> None:
    snapshot = list(_snapshot())
    snapshot[index] = None
    client = FakeClient(snapshots=[tuple(snapshot)])

    with pytest.raises(ControlRecordInitializationError, match="missing"):
        _initializer(client).initialize()

    assert client.scan_requests == []
    assert client.transact_write_requests == []


def test_installed_marker_never_repairs_corruption() -> None:
    snapshot = list(_snapshot())
    marker = cast(DynamoItem, snapshot[0])
    snapshot[0] = {**marker, "manifest_hash": "0" * 64}
    client = FakeClient(snapshots=[tuple(snapshot)])

    with pytest.raises(ControlRecordInitializationError, match=r"schema|marker"):
        _initializer(client).initialize()

    assert client.transact_write_requests == []


def test_installed_manifest_v1_is_not_silently_repaired_to_v2() -> None:
    snapshot = list(_snapshot())
    marker = cast(DynamoItem, snapshot[0])
    snapshot[0] = {
        **marker,
        "manifest_version": 1,
        "manifest_hash": "7c43ef2665d386482afb0655ccc5ac1e163d7f8fe14e7bc4fc3625be2daea320",
    }
    snapshot[10] = None
    client = FakeClient(snapshots=[tuple(snapshot)])

    with pytest.raises(ControlRecordInitializationError, match=r"schema|marker"):
        _initializer(client).initialize()

    assert client.scan_requests == []
    assert client.transact_write_requests == []


def test_first_install_rejects_active_work_on_a_later_scan_page() -> None:
    client = FakeClient(
        snapshots=[(None,) * 11],
        pages=[
            _scan_page(
                {
                    **_legacy_record("debate_meta", current_phase="completed"),
                    "schema_version": PREVIOUS_SCHEMA_VERSION,
                },
                last_key={"PK": "DEBATE#done", "SK": "META"},
            ),
            _scan_page(_legacy_record("attempt_meta", phase="discussing", recovery_state="none")),
        ],
    )

    with pytest.raises(ControlRecordInitializationError, match="active work"):
        _initializer(client).initialize()

    assert client.transact_write_requests == []


def test_first_install_requires_offline_migration_after_four_scan_pages() -> None:
    pages = [
        _scan_page(
            last_key={"PK": f"PAGE#{page}", "SK": "CURSOR"},
            scanned_count=100,
        )
        for page in range(4)
    ]
    client = FakeClient(snapshots=[(None,) * 11], pages=pages)

    with pytest.raises(ControlRecordMigrationRequired, match="bounded legacy scan"):
        _initializer(client).initialize()

    assert len(client.scan_requests) == 4
    assert all(request["Limit"] == 100 for request in client.scan_requests)
    assert "ExclusiveStartKey" not in client.scan_requests[0]
    assert all("ExclusiveStartKey" in request for request in client.scan_requests[1:])
    assert client.transact_write_requests == []


def test_concurrent_identical_install_converges_by_revalidating_all_records() -> None:
    client = FakeClient(snapshots=[(None,) * 11, _snapshot()])
    client.cancel_write = True

    result = _initializer(client).initialize()

    assert result.status is ControlRecordInitializationStatus.ALREADY_INITIALIZED
    assert len(client.transact_get_requests) == 2


def test_transaction_cancellation_fails_when_the_marker_did_not_converge() -> None:
    client = FakeClient(snapshots=[(None,) * 11, (None,) * 11])
    client.cancel_write = True

    with pytest.raises(ControlRecordInitializationError, match="missing"):
        _initializer(client).initialize()


def _legacy_record(record_type: str, **fields: object) -> DynamoItem:
    timestamp = LEGACY_CREATED_AT
    defaults: dict[str, object] = {
        "PK": "HISTORY#record",
        "SK": "ITEM#record",
        "record_type": record_type,
        "schema_version": CURRENT_SCHEMA_VERSION,
    }
    if record_type == "debate_meta":
        defaults.update(
            {
                "PK": "DEBATE#debate-id",
                "SK": "META",
                "debate_id": "debate-id",
                "current_attempt_id": "attempt-id",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
    elif record_type == "attempt_meta":
        defaults.update(
            {
                "PK": "DEBATE#debate-id",
                "SK": "ATTEMPT#attempt-id#META",
                "debate_id": "debate-id",
                "attempt_id": "attempt-id",
                "created_at": timestamp,
                "updated_at": timestamp,
                "attempt_created_at": timestamp,
            }
        )
    elif record_type == "outbox":
        defaults.update(
            {
                "PK": "DEBATE#debate-id",
                "SK": "ATTEMPT#attempt-id#OUTBOX#operation-id",
                "debate_id": "debate-id",
                "attempt_id": "attempt-id",
                "operation_id": "operation-id",
                "created_at": timestamp,
                "updated_at": timestamp,
                "delivery_attempt": 0,
            }
        )
    elif record_type in {"ingress_request", "ingress_operation_result"}:
        request_key = f"REQUEST#{timestamp}#interaction-id"
        defaults.update(
            {
                "PK": (
                    "CONTROL#INGRESS"
                    if record_type == "ingress_request"
                    else "INGRESS_OPERATION#interaction-id"
                ),
                "SK": request_key if record_type == "ingress_request" else "RESULT",
                "record_schema_version": 1,
                "interaction_id": "interaction-id",
                "operation_id": "operation-id",
                "request_sort_key": request_key,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        if record_type == "ingress_request":
            defaults.update(
                {
                    "status_message_state": "starting",
                    "startup_deadline_at": LEGACY_STARTUP_DEADLINE,
                    "terminal_deadline_at": LEGACY_TERMINAL_DEADLINE,
                    "delivery_attempt": 0,
                }
            )
    elif record_type == "ingress_active_pointer":
        request_key = f"REQUEST#{timestamp}#interaction-id"
        defaults.update(
            {
                "PK": "CONTROL#INGRESS#ACTIVE",
                "SK": request_key,
                "record_schema_version": 1,
                "interaction_id": "interaction-id",
                "request_sort_key": request_key,
                "created_at": timestamp,
            }
        )
    elif record_type == "ingress_status_publication":
        defaults.update(
            {
                "PK": "INGRESS_OPERATION#interaction-id",
                "SK": "STATUS_PUBLICATION",
                "record_schema_version": 3,
                "canonical_interaction_id": "interaction-id",
                "request_sort_key": f"REQUEST#{timestamp}#interaction-id",
                "created_at": timestamp,
                "updated_at": timestamp,
                "desired_state": "starting",
                "delivery_attempt": 0,
                "incarnation": 0,
                "history_reconciliation_required": False,
            }
        )
    elif record_type == "runtime_state":
        defaults.update(
            {
                "PK": "CONTROL#RUNTIME",
                "SK": "STATE",
                "record_schema_version": 1,
                "generation": 0,
                "desired_count": 0,
                "version": 0,
                "updated_at": timestamp,
            }
        )
    return cast(DynamoItem, {**defaults, **fields})


def _control_record(record_type: str) -> DynamoItem:
    return next(
        spec.install_item
        for spec in CONTROL_RECORD_MANIFEST.activity_records
        if spec.record_type == record_type
    )


def _without(item: DynamoItem, field: str) -> DynamoItem:
    return {key: value for key, value in item.items() if key != field}


def _sent_outbox(**fields: object) -> DynamoItem:
    values: dict[str, object] = {
        "status": "sent",
        "delivery_attempt": 1,
        "message_id": DISCORD_MESSAGE_ID,
        "sent_at": LEGACY_CREATED_AT,
    }
    values.update(fields)
    return _legacy_record("outbox", **values)


def _terminal_ingress_request(
    status: str = "completed",
    **fields: object,
) -> DynamoItem:
    status_fields_by_status: dict[str, dict[str, object]] = {
        "completed": {
            "status_message_state": "completed",
            "accepted_debate_id": "debate-id",
            "accepted_attempt_id": "attempt-id",
        },
        "rejected": {
            "status_message_state": "rejected",
            "error_code": "request_rejected",
        },
        "failed": {
            "status_message_state": "terminal_failed",
            "error_code": "request_failed",
        },
    }
    status_fields = status_fields_by_status[status]
    values: dict[str, object] = {
        "record_schema_version": 1,
        "status": status,
        "completed_at": LEGACY_CREATED_AT,
        **status_fields,
    }
    values.update(fields)
    return _legacy_record("ingress_request", **values)


def _terminal_ingress_result(status: str = "completed", **fields: object) -> DynamoItem:
    status_fields_by_status: dict[str, dict[str, object]] = {
        "completed": {
            "accepted_debate_id": "debate-id",
            "accepted_attempt_id": "attempt-id",
        },
        "rejected": {"error_code": "request_rejected"},
        "failed": {"error_code": "request_failed"},
    }
    status_fields = status_fields_by_status[status]
    values: dict[str, object] = {
        "record_schema_version": 1,
        "status": status,
        **status_fields,
    }
    values.update(fields)
    return _legacy_record("ingress_operation_result", **values)


def _settled_status_publication(
    state: str = "delivered",
    **fields: object,
) -> DynamoItem:
    state_fields_by_state: dict[str, dict[str, object]] = {
        "delivered": {
            "desired_state": "completed",
            "delivered_state": "completed",
            "status_message_id": DISCORD_MESSAGE_ID,
            "status_message_updated_at": LEGACY_CREATED_AT,
        },
        "failed": {
            "desired_state": "terminal_failed",
            "error_code": "status_delivery_failed",
        },
    }
    state_fields = state_fields_by_state[state]
    values: dict[str, object] = {
        "record_schema_version": 3,
        "publication_state": state,
        "delivery_attempt": 1,
        **state_fields,
    }
    values.update(fields)
    return _legacy_record("ingress_status_publication", **values)


@pytest.mark.parametrize(
    "item",
    [
        _legacy_record("attempt_meta", phase="discussing", recovery_state="none"),
        _legacy_record("attempt_meta", phase="completed", recovery_state="none"),
        {
            **_legacy_record("attempt_meta", phase="completed", recovery_state="checkpointed"),
            "schema_version": PREVIOUS_SCHEMA_VERSION,
        },
        _legacy_record("outbox", status="prepared"),
        _legacy_record(
            "ingress_request",
            record_schema_version=1,
            status="accepted",
        ),
        _legacy_record("ingress_active_pointer", record_schema_version=1),
        _legacy_record(
            "ingress_status_publication",
            record_schema_version=3,
            publication_state="retrying",
        ),
        _legacy_record("runtime_state", record_schema_version=1, state="starting"),
        {
            **_control_record("lease_slot"),
            "lease_owner": "owner",
            "lease_expiry": "2026-07-28T04:05:06.000000Z",
        },
        {**_control_record("active_attempt_counter"), "count": 1},
        {
            **_control_record("outbox_activity_counter"),
            "pending_count": 0,
            "claimed_count": 1,
        },
    ],
)
def test_legacy_active_work_classification_is_conservative(item: DynamoItem) -> None:
    assert control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "item",
    [
        {
            **_legacy_record("attempt_meta", phase="completed", recovery_state="none"),
            "schema_version": PREVIOUS_SCHEMA_VERSION,
        },
        _legacy_record(
            "attempt_meta",
            phase="completed",
            recovery_state="none",
            terminal_delivery_target="completed",
            terminal_delivery_operation_ids=["operation-id"],
            terminal_delivery_content_hashes=["a" * 64],
            terminal_delivery_staged_at="2026-07-28T04:04:06.000000Z",
            terminal_delivery_completed_at="2026-07-28T04:05:06.000000Z",
        ),
        _sent_outbox(),
        _terminal_ingress_request(),
        _terminal_ingress_request("rejected"),
        _terminal_ingress_result(),
        _terminal_ingress_result("failed"),
        _settled_status_publication(),
        _settled_status_publication("failed"),
        _legacy_record("runtime_state", record_schema_version=1, state="stopped"),
        _control_record("lease_slot"),
        _control_record("active_attempt_counter"),
        _control_record("outbox_activity_counter"),
    ],
)
def test_legacy_settled_work_classification_allows_install(item: DynamoItem) -> None:
    assert not control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "record_type",
    [
        "decision",
        "escalation_assessment",
        "evidence",
        "evidence_meta",
        "final_proposal",
        "initial_opinion",
        "vote",
    ],
)
def test_legacy_debate_artifacts_are_explicitly_inactive(record_type: str) -> None:
    item = _legacy_record(
        record_type,
        debate_id="debate-id",
        attempt_id="attempt-id",
        created_at="2026-07-28T04:00:00.000000Z",
        updated_at="2026-07-28T04:01:00.000000Z",
    )
    item["PK"] = "DEBATE#debate-id"
    expected_sort_key, discriminator = {
        "decision": ("ATTEMPT#attempt-id#DECISION", {}),
        "escalation_assessment": ("ATTEMPT#attempt-id#ESCALATION", {}),
        "evidence": ("ATTEMPT#attempt-id#EVIDENCE#0002", {"sequence": 2}),
        "evidence_meta": ("ATTEMPT#attempt-id#EVIDENCE#META", {}),
        "final_proposal": (
            "ATTEMPT#attempt-id#FINAL#participant-b",
            {"participant": "participant-b"},
        ),
        "initial_opinion": (
            "ATTEMPT#attempt-id#INITIAL#participant-a",
            {"participant": "participant-a"},
        ),
        "vote": ("ATTEMPT#attempt-id#VOTE#participant-c", {"voter": "participant-c"}),
    }[record_type]
    item.update(discriminator)
    item["SK"] = expected_sort_key

    assert not control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    ("record_type", "discriminator"),
    [
        ("decision", {}),
        ("escalation_assessment", {}),
        ("evidence", {"sequence": 0}),
        ("evidence_meta", {}),
        ("final_proposal", {"participant": "participant-a"}),
        ("initial_opinion", {"participant": "participant-b"}),
        ("vote", {"voter": "participant-c"}),
    ],
)
def test_legacy_debate_artifact_type_cannot_alias_attempt_metadata(
    record_type: str,
    discriminator: dict[str, object],
) -> None:
    item = _legacy_record(
        record_type,
        debate_id="debate-id",
        attempt_id="attempt-id",
        created_at=LEGACY_CREATED_AT,
        updated_at=LEGACY_CREATED_AT,
        **discriminator,
    )
    item["PK"] = "DEBATE#debate-id"
    item["SK"] = "ATTEMPT#attempt-id#META"

    with pytest.raises(ControlRecordInitializationError, match="artifact key"):
        control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "item",
    [
        _without(_sent_outbox(), "message_id"),
        _sent_outbox(delivery_attempt=0),
        _sent_outbox(
            claim_owner="stale-owner",
            claim_expiry="2026-07-28T04:02:00.000000Z",
        ),
        _without(_terminal_ingress_request(), "completed_at"),
        _terminal_ingress_request(error_code="unexpected_error"),
        _terminal_ingress_request(
            "failed",
            next_attempt_at="2026-07-28T04:01:00.000000Z",
        ),
        _without(_terminal_ingress_result(), "accepted_attempt_id"),
        _terminal_ingress_result("failed", error_code=""),
        _without(_settled_status_publication(), "status_message_id"),
        _settled_status_publication(delivered_state="rejected"),
        _settled_status_publication(
            claim_owner="stale-owner",
            claim_expiry="2026-07-28T04:02:00.000000Z",
        ),
        _without(_settled_status_publication("failed"), "error_code"),
    ],
)
def test_legacy_settled_records_require_durable_terminal_proof(item: DynamoItem) -> None:
    with pytest.raises(ControlRecordInitializationError):
        control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    ("partition_key", "sort_key"),
    [
        ("QUOTA#GUILD#", "DAY#2026-07-28"),
        ("QUOTA#GUILD#guild-id", "DAY#garbage"),
        ("QUOTA#GUILD#guild-id", "DAY#2026-07-28-extra"),
    ],
)
def test_legacy_daily_quota_requires_exact_guild_and_calendar_day(
    partition_key: str,
    sort_key: str,
) -> None:
    item = _legacy_record(
        "guild_daily_quota",
        count=4,
        created_at=LEGACY_CREATED_AT,
        updated_at=LEGACY_CREATED_AT,
    )
    item["PK"] = partition_key
    item["SK"] = sort_key

    with pytest.raises(ControlRecordInitializationError, match=r"quota (guild|date)"):
        control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "item",
    [
        {
            **_legacy_record(
                "panel_operation",
                operation_id="operation-id",
                kind="retry",
                created_at="2026-07-28T04:00:00.000000Z",
            ),
            "PK": "OPERATION#operation-id",
            "SK": "RESULT",
        },
        {
            **_legacy_record(
                "ingress_semantic_operation_binding",
                record_schema_version=1,
                operation_id="operation-id",
                canonical_interaction_id="interaction-id",
                request_sort_key="REQUEST#key",
                created_at="2026-07-28T04:00:00.000000Z",
            ),
            "PK": "INGRESS_SEMANTIC_OPERATION#operation-id",
            "SK": "BINDING",
        },
        {
            **_legacy_record(
                "runtime_wake_result",
                record_schema_version=1,
                interaction_id="interaction-id",
                generation=2,
                runtime_version=3,
                recorded_at="2026-07-28T04:00:00.000000Z",
            ),
            "PK": "INGRESS_OPERATION#interaction-id",
            "SK": "RUNTIME_WAKE",
        },
        {
            **_legacy_record(
                "guild_daily_quota",
                count=4,
                created_at="2026-07-28T04:00:00.000000Z",
                updated_at="2026-07-28T04:01:00.000000Z",
            ),
            "PK": "QUOTA#GUILD#guild-id",
            "SK": "DAY#2026-07-28",
        },
        {
            **_legacy_record(
                "panel_refresh_abandoned_counter",
                count=4,
                updated_at="2026-07-28T04:01:00.000000Z",
            ),
            "PK": "CONTROL#PANEL_REFRESH",
            "SK": "ABANDONED_COUNT",
        },
    ],
)
def test_legacy_statusless_records_are_explicitly_inactive(item: DynamoItem) -> None:
    assert not control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "item",
    [
        _legacy_record("unknown_future_record"),
        {**_legacy_record("outbox", status="sent"), "schema_version": 8},
        {**_legacy_record("outbox", status="sent"), "schema_version": 5},
        _legacy_record("outbox"),
        _without(_legacy_record("ingress_request", status="completed"), "record_schema_version"),
    ],
)
def test_legacy_unknown_schema_status_or_required_field_fails_closed(item: DynamoItem) -> None:
    with pytest.raises(ControlRecordInitializationError):
        control_records._legacy_item_is_active(item)


@pytest.mark.parametrize(
    "item",
    [
        _without(
            _legacy_record(
                "attempt_meta",
                phase="completed",
                recovery_state="none",
                terminal_delivery_target="completed",
                terminal_delivery_operation_ids=["operation-id"],
                terminal_delivery_content_hashes=["a" * 64],
                terminal_delivery_staged_at="2026-07-28T04:04:06.000000Z",
                terminal_delivery_completed_at="2026-07-28T04:05:06.000000Z",
            ),
            "attempt_id",
        ),
        _without(_legacy_record("outbox", status="sent"), "operation_id"),
        _without(
            _legacy_record("ingress_operation_result", status="completed"),
            "interaction_id",
        ),
        _without(
            _legacy_record(
                "ingress_status_publication",
                publication_state="delivered",
            ),
            "canonical_interaction_id",
        ),
        _without(
            _legacy_record("debate_meta", current_phase="completed"),
            "current_attempt_id",
        ),
    ],
)
def test_legacy_terminal_records_with_missing_identity_fail_closed(item: DynamoItem) -> None:
    with pytest.raises(ControlRecordInitializationError):
        control_records._legacy_item_is_active(item)
