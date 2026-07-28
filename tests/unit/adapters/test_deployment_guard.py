"""Atomic deployment snapshot and lock transaction tests without AWS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.control_records import CONTROL_RECORD_MANIFEST
from shittim_chest.adapters.dynamodb.deployment_guard import (
    DeploymentGuardUnavailable,
    DynamoDbDeploymentGuard,
)
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.application.deployment_guard import (
    BreakGlassReason,
    DeploymentGuardCode,
    DeploymentGuardContext,
    DeploymentLockState,
    DeploymentMode,
)

NOW = datetime(2026, 7, 28, 8, 30, tzinfo=UTC)
GUARD_ID = "019d2c1f-0000-7000-8000-a00000000002"
TABLE_NAME = "deployment-guard-test"


class TransactionCanceledException(Exception):
    pass


class FakeClient:
    def __init__(self, items: tuple[DynamoItem, ...]) -> None:
        self.items = items
        self.transact_get_requests: list[dict[str, object]] = []
        self.transact_write_requests: list[dict[str, object]] = []
        self.get_requests: list[dict[str, object]] = []
        self.persisted: dict[tuple[str, str], DynamoItem] = {}
        self.raise_after_persist = False
        self.audit_mutation: DynamoItem | None = None
        self.exceptions = SimpleNamespace(
            TransactionCanceledException=TransactionCanceledException,
        )

    def transact_get_items(self, **kwargs: object) -> dict[str, object]:
        self.transact_get_requests.append(kwargs)
        return {"Responses": [{"Item": marshal_item(item)} for item in self.items]}

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        self.transact_write_requests.append(kwargs)
        for action in cast(list[dict[str, Any]], kwargs["TransactItems"]):
            put = action.get("Put")
            if put is not None:
                item = unmarshal_item(put["Item"])
                if item.get("record_type") == "deployment_guard_audit":
                    item.update(self.audit_mutation or {})
                self.persisted[(cast(str, item["PK"]), cast(str, item["SK"]))] = item
        if self.raise_after_persist:
            raise TransactionCanceledException
        return {}

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.get_requests.append(kwargs)
        key = unmarshal_item(cast(dict[str, AttributeValueTypeDef], kwargs["Key"]))
        item = self.persisted.get((cast(str, key["PK"]), cast(str, key["SK"])))
        return {} if item is None else {"Item": marshal_item(item)}


def _items() -> tuple[DynamoItem, ...]:
    return (
        *(spec.install_item for spec in CONTROL_RECORD_MANIFEST.activity_records),
        CONTROL_RECORD_MANIFEST.initial_runtime_item,
        CONTROL_RECORD_MANIFEST.initial_deployment_lock_item,
    )


def _context() -> DeploymentGuardContext:
    return DeploymentGuardContext(
        commit_sha="a" * 40,
        actor="pitekusu",
        run_id="123456",
        environment="production",
    )


def _break_glass_context() -> DeploymentGuardContext:
    return DeploymentGuardContext(
        commit_sha="b" * 40,
        actor="release-operator",
        run_id="987654",
        environment="production",
        mode=DeploymentMode.BREAK_GLASS,
        reason=BreakGlassReason.SERVICE_RECOVERY,
    )


def _guard(client: FakeClient) -> DynamoDbDeploymentGuard:
    return DynamoDbDeploymentGuard(
        client=cast(DynamoDBClient, client),
        table_name=TABLE_NAME,
    )


def test_guard_reads_exactly_eleven_records_in_one_transaction() -> None:
    client = FakeClient(_items())

    assessment = _guard(client).guard(context=_context(), evaluated_at=NOW)

    assert assessment.allowed
    assert assessment.code is DeploymentGuardCode.SAFE
    assert len(client.transact_get_requests) == 1
    request = client.transact_get_requests[0]
    assert request["ReturnConsumedCapacity"] == "NONE"
    assert len(cast(list[object], request["TransactItems"])) == 11


def test_guard_fails_closed_on_missing_or_malformed_snapshot() -> None:
    missing = FakeClient(_items()[:-1])
    with pytest.raises(DeploymentGuardUnavailable, match="snapshot"):
        _guard(missing).guard(context=_context(), evaluated_at=NOW)

    malformed_items = list(_items())
    malformed_items[1] = {**malformed_items[1], "count": True}
    malformed = FakeClient(tuple(malformed_items))
    with pytest.raises(DeploymentGuardUnavailable, match="snapshot"):
        _guard(malformed).guard(context=_context(), evaluated_at=NOW)


def test_acquire_has_one_lock_action_exact_snapshot_checks_and_append_only_audit() -> None:
    client = FakeClient(_items())

    acquired = _guard(client).acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    assert acquired.lock.state is DeploymentLockState.LOCKED
    assert acquired.lock.fencing_token == 1
    request = client.transact_write_requests[0]
    actions = cast(list[dict[str, Any]], request["TransactItems"])
    assert len(actions) == 12
    assert len(cast(str, request["ClientRequestToken"])) <= 36
    touched_keys: list[tuple[str, str]] = []
    for action in actions:
        operation = action.get("ConditionCheck") or action.get("Put")
        assert operation is not None
        native = operation.get("Item") or operation.get("Key")
        key = unmarshal_item(native)
        touched_keys.append((cast(str, key["PK"]), cast(str, key["SK"])))
    assert touched_keys.count(("CONTROL#DEPLOYMENT", "LOCK")) == 1
    assert any(key[0].startswith("CONTROL#DEPLOYMENT#AUDIT#") for key in touched_keys)


def test_break_glass_acquire_persists_complete_immutable_context_and_prestate() -> None:
    client = FakeClient(_items())

    acquired = _guard(client).acquire(
        context=_break_glass_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    audit = acquired.audit_item
    assert audit == client.persisted[(f"CONTROL#DEPLOYMENT#AUDIT#{GUARD_ID}", "ACQUIRE")]
    assert audit["commit_sha"] == "b" * 40
    assert audit["actor"] == "release-operator"
    assert audit["run_id"] == "987654"
    assert audit["environment"] == "production"
    assert audit["deployment_mode"] == "break-glass"
    assert audit["break_glass_reason"] == "service-recovery"
    assert audit["decision_code"] == "break_glass_override"
    assert audit["runtime_status"] == "stopped"
    assert audit["runtime_generation"] == 0
    assert audit["runtime_version"] == 0
    assert audit["activity_clear"] is True
    assert audit["lock_fencing_token"] == 1
    assert audit["evaluated_at"] == "2026-07-28T08:30:00.000000Z"
    assert audit["lock_expires_at"] == "2026-07-28T08:45:00.000000Z"


def test_acquire_recognizes_same_guard_after_ambiguous_sdk_response() -> None:
    client = FakeClient(_items())
    client.raise_after_persist = True

    acquired = _guard(client).acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    assert acquired.lock.guard_id == GUARD_ID
    assert acquired.lock.owner == "pitekusu"
    assert acquired.assessment.code is DeploymentGuardCode.SAFE


def test_acquire_replay_uses_stored_timestamps_for_same_idempotency_key() -> None:
    client = FakeClient(_items())
    guard = _guard(client)
    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    client.items = (
        *client.items[:-1],
        client.persisted[("CONTROL#DEPLOYMENT", "LOCK")],
    )

    replayed = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=16),
    )

    assert replayed == acquired
    assert replayed.assessment.evaluated_at == NOW
    assert replayed.lock.acquired_at == NOW
    assert replayed.lock.expires_at == NOW + timedelta(minutes=15)
    assert len(client.transact_write_requests) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"decision_code": "break_glass_override"},
        {"runtime_status": "busy"},
        {"activity_clear": False},
        {"unexpected": "field"},
    ],
)
def test_acquire_replay_rejects_semantically_inconsistent_audit(
    mutation: DynamoItem,
) -> None:
    client = FakeClient(_items())
    client.raise_after_persist = True
    client.audit_mutation = mutation

    with pytest.raises(DeploymentGuardUnavailable, match="acquisition"):
        _guard(client).acquire(
            context=_context(),
            guard_id=GUARD_ID,
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )


def test_acquire_rejects_noncanonical_guard_id_before_any_sdk_read() -> None:
    client = FakeClient(_items())

    with pytest.raises(ValueError, match="canonical UUIDv7"):
        _guard(client).acquire(
            context=_context(),
            guard_id="not-a-uuid",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        )

    assert client.get_requests == []
    assert client.transact_get_requests == []
    assert client.transact_write_requests == []


def test_release_recognizes_response_loss_and_a_separate_later_replay() -> None:
    client = FakeClient(_items())
    acquired = _guard(client).acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    client.raise_after_persist = True
    released_at = NOW + timedelta(minutes=1)

    _guard(client).release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=released_at,
    )
    client.raise_after_persist = False
    _guard(client).release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=released_at + timedelta(seconds=30),
    )

    released = client.persisted[("CONTROL#DEPLOYMENT", "LOCK")]
    assert released["lock_state"] == "open"
    assert released["fencing_token"] == acquired.lock.fencing_token
    audit = client.persisted[(f"CONTROL#DEPLOYMENT#AUDIT#{GUARD_ID}", "RELEASE")]
    assert audit["released_at"] == "2026-07-28T08:31:00.000000Z"


@pytest.mark.parametrize(
    "mutation",
    [
        {"released_at": "not-a-timestamp"},
        {"actor": "different-actor"},
        {"lock_fencing_token": True},
        {"unexpected": "field"},
    ],
)
def test_release_replay_rejects_mutated_immutable_audit(mutation: DynamoItem) -> None:
    client = FakeClient(_items())
    guard = _guard(client)
    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    client.audit_mutation = mutation
    client.raise_after_persist = True

    with pytest.raises(DeploymentGuardUnavailable, match="release"):
        guard.release(
            guard_id=GUARD_ID,
            expected_fencing_token=acquired.lock.fencing_token,
            actor="pitekusu",
            released_at=NOW + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("guard_id", "actor"),
    [
        ("not-a-uuid", "pitekusu"),
        (GUARD_ID, "unsafe actor"),
    ],
)
def test_release_rejects_invalid_identity_before_any_sdk_read(
    guard_id: str,
    actor: str,
) -> None:
    client = FakeClient(_items())

    with pytest.raises(ValueError):
        _guard(client).release(
            guard_id=guard_id,
            expected_fencing_token=1,
            actor=actor,
            released_at=NOW,
        )

    assert client.get_requests == []
    assert client.transact_get_requests == []
    assert client.transact_write_requests == []


def test_expired_lock_remains_locked_and_is_not_reclaimed_by_guard() -> None:
    client = FakeClient(_items())
    _guard(client).acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    locked_items = list(_items())
    locked_items[-1] = client.persisted[("CONTROL#DEPLOYMENT", "LOCK")]

    assessment = _guard(FakeClient(tuple(locked_items))).guard(
        context=_context(),
        evaluated_at=NOW + timedelta(hours=1),
    )

    assert not assessment.allowed
    assert assessment.code is DeploymentGuardCode.DEPLOYMENT_LOCKED
