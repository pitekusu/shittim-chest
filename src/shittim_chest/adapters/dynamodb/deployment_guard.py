"""Strong deployment admission snapshot and fenced deployment lock for DynamoDB."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )
else:
    TransactGetItemTypeDef = object
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.control_records import (
    CONTROL_RECORD_MANIFEST,
    ControlRecordInitializationError,
    ControlRecordSpec,
    _condition_exact,
    _convert_fixed_record_schema,
    _DeploymentLockSpec,
    _exact_condition,
    _put_migrated,
    _RuntimeStateSpec,
    _validate_activity_item,
    _validate_deployment_lock_item,
    _validate_runtime_item,
)
from shittim_chest.adapters.dynamodb.deployment_lock import deployment_lock_open_check
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNT_LIMIT,
    GLOBAL_LEASE_SLOTS,
    PANEL_REFRESH_COUNT_LIMIT,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    PersistenceFormatError,
    deserialize_deployment_lock,
    deserialize_runtime_state,
    serialize_deployment_lock,
)
from shittim_chest.application.deployment_guard import (
    DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
    BreakGlassReason,
    DeploymentGuardAssessment,
    DeploymentGuardCode,
    DeploymentGuardContext,
    DeploymentGuardSnapshot,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
    assess_deployment,
    validate_deployment_actor,
    validate_deployment_guard_id,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_QUEUE_LIMIT,
    RuntimeActivity,
    RuntimeStatus,
)

_SNAPSHOT_RECORD_COUNT = 11
_OUTBOX_ACTIVITY_LIMIT = 100_000
_STATUS_PENDING_LIMIT = 100_000


class DeploymentGuardUnavailable(RuntimeError):
    """Signal a content-free fail-closed read, validation, or transaction failure."""


class DeploymentGuardRejected(RuntimeError):
    """Signal a valid deployment snapshot that did not admit this deployment."""

    def __init__(self, assessment: DeploymentGuardAssessment) -> None:
        super().__init__(assessment.code.value)
        self.assessment = assessment


@dataclass(frozen=True, slots=True)
class DeploymentLockAcquisition:
    """Acquired lock and its immutable, content-free audit item."""

    assessment: DeploymentGuardAssessment
    lock: DeploymentLock
    audit_item: DynamoItem
    control_schema_before: int
    control_schema_after: int


class DynamoDbDeploymentGuard:
    """Read admission facts atomically and acquire/release a fenced deployment lock."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    def inspect(self, *, at: datetime) -> DeploymentGuardSnapshot:
        """Return one validated TransactGetItems snapshot without Scan or Query."""

        _require_utc(at)
        try:
            return self._read_snapshot(at=at).snapshot
        except DeploymentGuardUnavailable:
            raise
        except (
            BotoCoreError,
            ClientError,
            ControlRecordInitializationError,
            PersistenceFormatError,
            ValueError,
        ):
            raise DeploymentGuardUnavailable("deployment snapshot is unavailable") from None

    def guard(
        self,
        *,
        context: DeploymentGuardContext,
        evaluated_at: datetime,
    ) -> DeploymentGuardAssessment:
        """Evaluate the pure fail-closed admission policy against one atomic snapshot."""

        snapshot = self.inspect(at=evaluated_at)
        return assess_deployment(snapshot, context=context, evaluated_at=evaluated_at)

    def acquire(
        self,
        *,
        context: DeploymentGuardContext,
        guard_id: str,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> DeploymentLockAcquisition:
        """Atomically admit and lock; a caller-supplied guard ID makes retries replay-safe."""

        _require_utc(acquired_at)
        _require_utc(expires_at)
        if acquired_at >= expires_at:
            raise ValueError("deployment lock expiry must follow acquisition")
        validate_deployment_guard_id(guard_id)
        try:
            validated = self._read_snapshot(at=acquired_at)
            assessment = assess_deployment(
                validated.snapshot,
                context=context,
                evaluated_at=acquired_at,
            )
            if not assessment.allowed:
                replay = self._replay_acquisition(
                    context=context,
                    guard_id=guard_id,
                )
                if replay is not None:
                    return replay
                raise DeploymentGuardRejected(assessment)
            previous = validated.snapshot.deployment_lock
            locked = DeploymentLock(
                state=DeploymentLockState.LOCKED,
                fencing_token=previous.fencing_token + 1,
                version=previous.version + 1,
                updated_at=acquired_at,
                guard_id=guard_id,
                owner=context.actor,
                acquired_at=acquired_at,
                expires_at=expires_at,
                mode=context.mode,
                reason=context.reason,
            )
            audit = _acquire_audit_item(
                assessment=assessment,
                lock=locked,
                control_schema_before=validated.schema_version,
            )
            actions = self._acquire_actions(
                validated=validated,
                locked=locked,
                audit=audit,
            )
            try:
                self._client.transact_write_items(
                    TransactItems=actions,
                    ClientRequestToken=_transaction_token("acquire", actions),
                    ReturnConsumedCapacity="NONE",
                )
            except (
                self._client.exceptions.TransactionCanceledException,
                BotoCoreError,
                ClientError,
            ):
                replay = self._replay_acquisition(
                    context=context,
                    guard_id=guard_id,
                )
                if replay is not None:
                    return replay
                raise DeploymentGuardUnavailable("deployment lock acquisition failed") from None
            return DeploymentLockAcquisition(
                assessment=assessment,
                lock=locked,
                audit_item=audit,
                control_schema_before=validated.schema_version,
                control_schema_after=CURRENT_SCHEMA_VERSION,
            )
        except DeploymentGuardRejected, DeploymentGuardUnavailable:
            raise
        except (
            BotoCoreError,
            ClientError,
            ControlRecordInitializationError,
            PersistenceFormatError,
            ValueError,
        ):
            raise DeploymentGuardUnavailable("deployment lock acquisition failed") from None

    def release(
        self,
        *,
        guard_id: str,
        expected_fencing_token: int,
        actor: str,
        released_at: datetime,
        rollback_control_schema: bool = False,
    ) -> None:
        """Release only the exact owned fence; an immutable audit makes retries safe."""

        _require_utc(released_at)
        if isinstance(expected_fencing_token, bool) or expected_fencing_token <= 0:
            raise ValueError("expected fencing token must be positive")
        validate_deployment_guard_id(guard_id)
        validate_deployment_actor(actor)
        try:
            if self._release_audit_exists(
                guard_id=guard_id,
                expected_fencing_token=expected_fencing_token,
                actor=actor,
                rollback_control_schema=rollback_control_schema,
            ):
                return
            validated = self._read_snapshot(at=released_at) if rollback_control_schema else None
            current = (
                validated.snapshot.deployment_lock if validated is not None else self._read_lock()
            )
            if current.state is DeploymentLockState.OPEN:
                raise DeploymentGuardUnavailable("deployment lock release did not match")
            if (
                current.guard_id != guard_id
                or current.owner != actor
                or current.fencing_token != expected_fencing_token
            ):
                raise DeploymentGuardUnavailable("deployment lock release did not match")
            acquire_audit = self._get_audit(guard_id=guard_id, action="ACQUIRE")
            acquire_context = (
                None if acquire_audit is None else _acquire_audit_context(acquire_audit)
            )
            if (
                acquire_audit is None
                or acquire_context is None
                or acquire_context.actor != actor
                or not _acquire_audit_matches(
                    acquire_audit,
                    context=acquire_context,
                    lock=current,
                )
            ):
                raise DeploymentGuardUnavailable("deployment lock acquisition audit is invalid")
            control_schema_before = _integer(acquire_audit, "control_schema_before")
            control_schema_rolled_back = (
                rollback_control_schema and control_schema_before == PREVIOUS_SCHEMA_VERSION
            )
            control_schema_after = (
                PREVIOUS_SCHEMA_VERSION if control_schema_rolled_back else CURRENT_SCHEMA_VERSION
            )
            opened = DeploymentLock(
                state=DeploymentLockState.OPEN,
                fencing_token=current.fencing_token,
                version=current.version + 1,
                updated_at=released_at,
            )
            audit = _release_audit_item(
                guard_id=guard_id,
                actor=actor,
                fencing_token=expected_fencing_token,
                released_at=released_at,
                control_schema_before=control_schema_before,
                control_schema_after=control_schema_after,
                control_schema_rolled_back=control_schema_rolled_back,
            )
            actions = self._release_actions(
                current=current,
                opened=opened,
                audit=audit,
                validated=validated if control_schema_rolled_back else None,
            )
            try:
                self._client.transact_write_items(
                    TransactItems=actions,
                    ClientRequestToken=_transaction_token("release", actions),
                    ReturnConsumedCapacity="NONE",
                )
            except (
                self._client.exceptions.TransactionCanceledException,
                BotoCoreError,
                ClientError,
            ):
                if self._release_audit_exists(
                    guard_id=guard_id,
                    expected_fencing_token=expected_fencing_token,
                    actor=actor,
                    rollback_control_schema=rollback_control_schema,
                ):
                    return
                raise DeploymentGuardUnavailable("deployment lock release failed") from None
        except DeploymentGuardUnavailable:
            raise
        except BotoCoreError, ClientError, PersistenceFormatError, ValueError:
            raise DeploymentGuardUnavailable("deployment lock release failed") from None

    def _release_actions(
        self,
        *,
        current: DeploymentLock,
        opened: DeploymentLock,
        audit: DynamoItem,
        validated: _ValidatedSnapshot | None,
    ) -> list[TransactWriteItemTypeDef]:
        if validated is None:
            return [
                cast(
                    TransactWriteItemTypeDef,
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": marshal_item(serialize_deployment_lock(opened)),
                            **_exact_condition(
                                serialize_deployment_lock(current),
                                allowed_fields=_DeploymentLockSpec().allowed_fields,
                            ),
                        }
                    },
                ),
                _put_immutable(self._table_name, audit),
            ]
        if validated.schema_version != CURRENT_SCHEMA_VERSION:
            raise DeploymentGuardUnavailable("deployment rollback snapshot is not current")
        specs: tuple[ControlRecordSpec | _RuntimeStateSpec | _DeploymentLockSpec, ...] = (
            *CONTROL_RECORD_MANIFEST.activity_records,
            _RuntimeStateSpec(),
            _DeploymentLockSpec(),
        )
        targets = [
            _convert_fixed_record_schema(
                spec,
                item,
                schema_version=PREVIOUS_SCHEMA_VERSION,
            )
            for spec, item in zip(specs[:-1], validated.items[:-1], strict=True)
        ]
        targets.append(
            _convert_fixed_record_schema(
                specs[-1],
                serialize_deployment_lock(opened),
                schema_version=PREVIOUS_SCHEMA_VERSION,
            )
        )
        actions = [
            _put_migrated(
                self._table_name,
                previous=previous,
                current=target,
                allowed_fields=spec.allowed_fields,
            )
            for spec, previous, target in zip(
                specs,
                validated.items,
                targets,
                strict=True,
            )
        ]
        actions.append(_put_immutable(self._table_name, audit))
        return actions

    def _read_snapshot(self, *, at: datetime) -> _ValidatedSnapshot:
        specs: tuple[ControlRecordSpec | _RuntimeStateSpec | _DeploymentLockSpec, ...] = (
            *CONTROL_RECORD_MANIFEST.activity_records,
            _RuntimeStateSpec(),
            _DeploymentLockSpec(),
        )
        actions = [
            cast(
                TransactGetItemTypeDef,
                {
                    "Get": {
                        "TableName": self._table_name,
                        "Key": marshal_item(spec.key),
                    }
                },
            )
            for spec in specs
        ]
        response = self._client.transact_get_items(
            TransactItems=actions,
            ReturnConsumedCapacity="NONE",
        )
        raw_responses = response.get("Responses", [])
        if len(raw_responses) != _SNAPSHOT_RECORD_COUNT:
            raise DeploymentGuardUnavailable("deployment snapshot has an invalid shape")
        items = tuple(
            None if raw.get("Item") is None else unmarshal_item(raw["Item"])
            for raw in raw_responses
        )
        if any(item is None for item in items):
            raise DeploymentGuardUnavailable("deployment snapshot is incomplete")
        complete = cast(tuple[DynamoItem, ...], items)
        schema_versions: set[int] = set()
        for spec, item in zip(CONTROL_RECORD_MANIFEST.activity_records, complete[:9], strict=True):
            schema_versions.add(_control_schema_version(item))
            _validate_activity_item(
                spec,
                item,
                require_idle=False,
                allowed_schema_versions=frozenset(
                    {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
                ),
            )
        schema_versions.add(_control_schema_version(complete[9]))
        runtime_item = _validate_runtime_item(
            complete[9],
            require_stopped=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        schema_versions.add(_control_schema_version(complete[10]))
        lock_item = _validate_deployment_lock_item(
            complete[10],
            require_open=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        if len(schema_versions) != 1:
            raise DeploymentGuardUnavailable("deployment snapshot schema versions are mixed")
        schema_version = schema_versions.pop()
        runtime = deserialize_runtime_state(runtime_item)
        lock = deserialize_deployment_lock(lock_item)
        activity = _runtime_activity(complete[1:9], at=at)
        return _ValidatedSnapshot(
            items=complete,
            schema_version=schema_version,
            snapshot=DeploymentGuardSnapshot(
                runtime=runtime,
                activity=activity,
                deployment_lock=lock,
            ),
        )

    def _read_lock(self) -> DeploymentLock:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(_DeploymentLockSpec().key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            raise DeploymentGuardUnavailable("deployment lock is missing")
        return deserialize_deployment_lock(unmarshal_item(raw))

    def _acquire_actions(
        self,
        *,
        validated: _ValidatedSnapshot,
        locked: DeploymentLock,
        audit: DynamoItem,
    ) -> list[TransactWriteItemTypeDef]:
        specs: tuple[ControlRecordSpec | _RuntimeStateSpec, ...] = (
            *CONTROL_RECORD_MANIFEST.activity_records,
            _RuntimeStateSpec(),
        )
        if validated.schema_version == PREVIOUS_SCHEMA_VERSION:
            actions = [
                _put_migrated(
                    self._table_name,
                    previous=item,
                    current=_convert_fixed_record_schema(
                        spec,
                        item,
                        schema_version=CURRENT_SCHEMA_VERSION,
                    ),
                    allowed_fields=spec.allowed_fields,
                )
                for spec, item in zip(specs, validated.items[:10], strict=True)
            ]
        else:
            actions = [
                _condition_exact(
                    self._table_name,
                    item,
                    allowed_fields=spec.allowed_fields,
                )
                for spec, item in zip(specs, validated.items[:10], strict=True)
            ]
        previous_lock = validated.items[10]
        actions.append(
            cast(
                TransactWriteItemTypeDef,
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(serialize_deployment_lock(locked)),
                        **_exact_condition(
                            previous_lock,
                            allowed_fields=_DeploymentLockSpec().allowed_fields,
                        ),
                    }
                },
            )
        )
        actions.append(_put_immutable(self._table_name, audit))
        return actions

    def _replay_acquisition(
        self,
        *,
        context: DeploymentGuardContext,
        guard_id: str,
    ) -> DeploymentLockAcquisition | None:
        """Replay one immutable guard ID independently of the retry wall clock."""

        lock = self._read_lock()
        if (
            lock.state is not DeploymentLockState.LOCKED
            or lock.guard_id != guard_id
            or lock.owner != context.actor
            or lock.mode is not context.mode
            or lock.reason is not context.reason
        ):
            return None
        audit = self._get_audit(guard_id=guard_id, action="ACQUIRE")
        if audit is None or not _acquire_audit_matches(audit, context=context, lock=lock):
            return None
        control_schema_before = _integer(audit, "control_schema_before")
        assessment = DeploymentGuardAssessment(
            allowed=True,
            code=(
                # The audit stores the original decision before the lock was acquired.
                _assessment_code(audit)
            ),
            context=context,
            evaluated_at=_parse_timestamp(_text(audit, "evaluated_at")),
            runtime_status=_runtime_status(audit),
            runtime_generation=_integer(audit, "runtime_generation"),
            runtime_version=_integer(audit, "runtime_version"),
            activity_clear=_boolean(audit, "activity_clear"),
            deployment_lock_state=DeploymentLockState.OPEN,
            deployment_lock_fencing_token=lock.fencing_token - 1,
        )
        return DeploymentLockAcquisition(
            assessment=assessment,
            lock=lock,
            audit_item=audit,
            control_schema_before=control_schema_before,
            control_schema_after=CURRENT_SCHEMA_VERSION,
        )

    def _release_audit_exists(
        self,
        *,
        guard_id: str,
        expected_fencing_token: int,
        actor: str,
        rollback_control_schema: bool,
    ) -> bool:
        audit = self._get_audit(guard_id=guard_id, action="RELEASE")
        return audit is not None and _release_audit_matches(
            audit,
            guard_id=guard_id,
            actor=actor,
            fencing_token=expected_fencing_token,
            rollback_control_schema=rollback_control_schema,
        )

    def _get_audit(self, *, guard_id: str, action: str) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(_audit_key(guard_id=guard_id, action=action)),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)


@dataclass(frozen=True, slots=True)
class _ValidatedSnapshot:
    items: tuple[DynamoItem, ...]
    schema_version: int
    snapshot: DeploymentGuardSnapshot


def _runtime_activity(items: tuple[DynamoItem, ...], *, at: datetime) -> RuntimeActivity:
    ingress = _bounded_counter(items[0], "count", INGRESS_QUEUE_LIMIT)
    status = _bounded_counter(items[1], "count", _STATUS_PENDING_LIMIT)
    panel = _bounded_counter(items[2], "count", PANEL_REFRESH_COUNT_LIMIT)
    pending_outbox = _bounded_counter(items[3], "pending_count", _OUTBOX_ACTIVITY_LIMIT)
    claimed_outbox = _bounded_counter(items[3], "claimed_count", _OUTBOX_ACTIVITY_LIMIT)
    attempts = _bounded_counter(items[4], "count", ACTIVE_ATTEMPT_COUNT_LIMIT)
    active_leases = 0
    expired_leases = 0
    for item in items[5 : 5 + GLOBAL_LEASE_SLOTS]:
        owner = item.get("lease_owner")
        expiry = item.get("lease_expiry")
        if owner is None and expiry is None:
            continue
        if not isinstance(owner, str) or not owner.strip() or not isinstance(expiry, str):
            raise ValueError("deployment snapshot lease is malformed")
        parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        _require_utc(parsed)
        if parsed >= at:
            active_leases += 1
        else:
            expired_leases += 1
    if active_leases + expired_leases > attempts:
        raise ValueError("deployment snapshot leases exceed attempts")
    return RuntimeActivity(
        pending_ingress=ingress,
        active_attempts=attempts,
        application_tasks=active_leases,
        active_leases=active_leases,
        recovery_tasks=max(expired_leases, attempts - active_leases),
        pending_outbox=pending_outbox,
        claimed_outbox=claimed_outbox,
        pending_status_updates=status,
        pending_panel_refreshes=panel,
    )


def _bounded_counter(item: DynamoItem, field: str, limit: int) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        raise ValueError("deployment snapshot counter is malformed")
    return value


def _control_schema_version(item: DynamoItem) -> int:
    value = item.get("schema_version")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
    ):
        raise ValueError("deployment control schema version is unsupported")
    return value


def _acquire_audit_item(
    *,
    assessment: DeploymentGuardAssessment,
    lock: DeploymentLock,
    control_schema_before: int,
) -> DynamoItem:
    item: DynamoItem = {
        **_audit_key(guard_id=lock.guard_id or "", action="ACQUIRE"),
        "record_type": "deployment_guard_audit",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
        "action": "acquire",
        "guard_id": lock.guard_id or "",
        "commit_sha": assessment.context.commit_sha,
        "actor": assessment.context.actor,
        "run_id": assessment.context.run_id,
        "environment": assessment.context.environment,
        "deployment_mode": assessment.context.mode.value,
        "decision_code": assessment.code.value,
        "runtime_status": assessment.runtime_status.value,
        "runtime_generation": assessment.runtime_generation,
        "runtime_version": assessment.runtime_version,
        "activity_clear": assessment.activity_clear,
        "lock_fencing_token": lock.fencing_token,
        "control_schema_before": control_schema_before,
        "control_schema_after": CURRENT_SCHEMA_VERSION,
        "control_schema_migrated": control_schema_before != CURRENT_SCHEMA_VERSION,
        "evaluated_at": _timestamp(assessment.evaluated_at),
        "lock_expires_at": _timestamp(lock.expires_at or assessment.evaluated_at),
    }
    if assessment.context.reason is not None:
        item["break_glass_reason"] = assessment.context.reason.value
    return item


def _release_audit_item(
    *,
    guard_id: str,
    actor: str,
    fencing_token: int,
    released_at: datetime,
    control_schema_before: int,
    control_schema_after: int,
    control_schema_rolled_back: bool,
) -> DynamoItem:
    return {
        **_audit_key(guard_id=guard_id, action="RELEASE"),
        "record_type": "deployment_guard_audit",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
        "action": "release",
        "guard_id": guard_id,
        "actor": actor,
        "lock_fencing_token": fencing_token,
        "control_schema_before": control_schema_before,
        "control_schema_after": control_schema_after,
        "control_schema_rolled_back": control_schema_rolled_back,
        "released_at": _timestamp(released_at),
    }


def _release_audit_matches(
    audit: DynamoItem,
    *,
    guard_id: str,
    actor: str,
    fencing_token: int,
    rollback_control_schema: bool,
) -> bool:
    """Validate an immutable release receipt without binding it to retry time."""

    allowed_fields = {
        "PK",
        "SK",
        "record_type",
        "schema_version",
        "record_schema_version",
        "action",
        "guard_id",
        "actor",
        "lock_fencing_token",
        "control_schema_before",
        "control_schema_after",
        "control_schema_rolled_back",
        "released_at",
    }
    if set(audit) != allowed_fields:
        return False
    expected = {
        **_audit_key(guard_id=guard_id, action="RELEASE"),
        "record_type": "deployment_guard_audit",
        "action": "release",
        "guard_id": guard_id,
        "actor": actor,
    }
    if not all(audit.get(field) == value for field, value in expected.items()):
        return False
    try:
        schema_version = _integer(audit, "schema_version")
        record_schema_version = _integer(audit, "record_schema_version")
        stored_fencing_token = _integer(audit, "lock_fencing_token")
        control_schema_before = _integer(audit, "control_schema_before")
        control_schema_after = _integer(audit, "control_schema_after")
        control_schema_rolled_back = _boolean(audit, "control_schema_rolled_back")
        released_at = _text(audit, "released_at")
        parsed_released_at = _parse_timestamp(released_at)
    except ValueError:
        return False
    expected_rollback = rollback_control_schema and control_schema_before == PREVIOUS_SCHEMA_VERSION
    expected_after = PREVIOUS_SCHEMA_VERSION if expected_rollback else CURRENT_SCHEMA_VERSION
    return (
        schema_version == CURRENT_SCHEMA_VERSION
        and record_schema_version == DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION
        and stored_fencing_token == fencing_token
        and stored_fencing_token > 0
        and control_schema_before in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
        and control_schema_after == expected_after
        and control_schema_rolled_back is expected_rollback
        and _timestamp(parsed_released_at) == released_at
    )


def _audit_key(*, guard_id: str, action: str) -> DynamoItem:
    return {"PK": f"CONTROL#DEPLOYMENT#AUDIT#{guard_id}", "SK": action}


def _put_immutable(table_name: str, item: DynamoItem) -> TransactWriteItemTypeDef:
    return cast(
        TransactWriteItemTypeDef,
        {
            "Put": {
                "TableName": table_name,
                "Item": marshal_item(item),
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        },
    )


def _acquire_audit_context(audit: DynamoItem) -> DeploymentGuardContext:
    mode = DeploymentMode(_text(audit, "deployment_mode"))
    raw_reason = audit.get("break_glass_reason")
    if raw_reason is not None and (not isinstance(raw_reason, str) or not raw_reason):
        raise ValueError("deployment audit reason is malformed")
    return DeploymentGuardContext(
        commit_sha=_text(audit, "commit_sha"),
        actor=_text(audit, "actor"),
        run_id=_text(audit, "run_id"),
        environment=_text(audit, "environment"),
        mode=mode,
        reason=BreakGlassReason(raw_reason) if raw_reason is not None else None,
    )


def _acquire_audit_matches(
    audit: DynamoItem,
    *,
    context: DeploymentGuardContext,
    lock: DeploymentLock,
) -> bool:
    allowed_fields = {
        "PK",
        "SK",
        "record_type",
        "schema_version",
        "record_schema_version",
        "action",
        "guard_id",
        "commit_sha",
        "actor",
        "run_id",
        "environment",
        "deployment_mode",
        "decision_code",
        "runtime_status",
        "runtime_generation",
        "runtime_version",
        "activity_clear",
        "lock_fencing_token",
        "control_schema_before",
        "control_schema_after",
        "control_schema_migrated",
        "evaluated_at",
        "lock_expires_at",
    }
    if context.reason is not None:
        allowed_fields.add("break_glass_reason")
    if set(audit) != allowed_fields:
        return False
    expected = {
        **_audit_key(guard_id=lock.guard_id or "", action="ACQUIRE"),
        "record_type": "deployment_guard_audit",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": DEPLOYMENT_GUARD_AUDIT_SCHEMA_VERSION,
        "action": "acquire",
        "guard_id": lock.guard_id,
        "commit_sha": context.commit_sha,
        "actor": context.actor,
        "run_id": context.run_id,
        "environment": context.environment,
        "deployment_mode": context.mode.value,
        "lock_fencing_token": lock.fencing_token,
        "control_schema_after": CURRENT_SCHEMA_VERSION,
        "evaluated_at": _timestamp(lock.acquired_at or lock.updated_at),
        "lock_expires_at": _timestamp(lock.expires_at or lock.updated_at),
    }
    if context.reason is not None:
        expected["break_glass_reason"] = context.reason.value
    if not all(audit.get(field) == value for field, value in expected.items()):
        return False
    try:
        code = _assessment_code(audit)
        status = _runtime_status(audit)
        generation = _integer(audit, "runtime_generation")
        version = _integer(audit, "runtime_version")
        control_schema_before = _integer(audit, "control_schema_before")
        control_schema_after = _integer(audit, "control_schema_after")
        control_schema_migrated = _boolean(audit, "control_schema_migrated")
        activity_clear = _boolean(audit, "activity_clear")
        _parse_timestamp(_text(audit, "evaluated_at"))
        _parse_timestamp(_text(audit, "lock_expires_at"))
    except ValueError:
        return False
    if (
        generation < 0
        or version < 0
        or status.value != audit["runtime_status"]
        or control_schema_before not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
        or control_schema_after != CURRENT_SCHEMA_VERSION
        or control_schema_migrated != (control_schema_before != CURRENT_SCHEMA_VERSION)
    ):
        return False
    expected_code = (
        DeploymentGuardCode.BREAK_GLASS_OVERRIDE
        if context.mode.value == "break-glass"
        else DeploymentGuardCode.SAFE
    )
    if code is not expected_code:
        return False
    if context.mode.value == "normal":
        return activity_clear and status in {RuntimeStatus.STOPPED, RuntimeStatus.IDLE}
    return True


def _assessment_code(audit: DynamoItem) -> DeploymentGuardCode:
    return DeploymentGuardCode(_text(audit, "decision_code"))


def _runtime_status(audit: DynamoItem) -> RuntimeStatus:
    return RuntimeStatus(_text(audit, "runtime_status"))


def _integer(item: DynamoItem, field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("deployment audit integer is malformed")
    return value


def _boolean(item: DynamoItem, field: str) -> bool:
    value = item.get(field)
    if not isinstance(value, bool):
        raise ValueError("deployment audit boolean is malformed")
    return value


def _text(item: DynamoItem, field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError("deployment audit text is malformed")
    return value


def _transaction_token(label: str, actions: list[TransactWriteItemTypeDef]) -> str:
    canonical = json.dumps(actions, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(f"{label}:{canonical}".encode()).hexdigest()
    return f"dg-{digest[:33]}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_utc(parsed)
    return parsed


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")


__all__ = (
    "DeploymentGuardRejected",
    "DeploymentGuardUnavailable",
    "DeploymentLockAcquisition",
    "DynamoDbDeploymentGuard",
    "deployment_lock_open_check",
)
