"""Install and validate the fixed DynamoDB control-record manifest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum, unique
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        ScanInputTypeDef,
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )
else:
    ScanInputTypeDef = object
    TransactGetItemTypeDef = object
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import INGRESS_RECORD_SCHEMA_VERSION
from shittim_chest.adapters.dynamodb.outbox import (
    OUTBOX_ACTIVITY_LIMIT,
    OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.repository import (
    ACTIVE_ATTEMPT_COUNT_LIMIT,
    ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
    GLOBAL_LEASE_SLOTS,
    PANEL_REFRESH_COUNT_LIMIT,
)
from shittim_chest.adapters.dynamodb.runtime_state import (
    RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
    RUNTIME_ACTIVITY_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    PersistenceFormatError,
    deserialize_deployment_lock,
    deserialize_runtime_state,
    serialize_deployment_lock,
    serialize_runtime_state,
)
from shittim_chest.application.deployment_guard import DeploymentLock, DeploymentLockState
from shittim_chest.application.discord import OutboxStatus, PanelOperationKind
from shittim_chest.application.models import (
    DeliveryAbandonReason,
    GenerationCheckpoint,
    GenerationStatus,
    PhaseDeliveryStatus,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_QUEUE_LIMIT,
    STARTUP_TIMEOUT,
    TERMINAL_TIMEOUT,
    IngressStatus,
    RuntimeState,
    RuntimeStatus,
    StatusMessageState,
    StatusPublicationState,
)
from shittim_chest.domain import DebatePhase, ParticipantSlot

CONTROL_RECORD_MANIFEST_VERSION = 2
_INITIAL_RUNTIME_AT = datetime(1970, 1, 1, tzinfo=UTC)
_CLIENT_REQUEST_ID_PREFIX = "cr-"
_LEGACY_SCAN_PAGE_SIZE = 100
_LEGACY_SCAN_MAX_PAGES = 4
_LEGACY_SCAN_MAX_EVALUATED_ITEMS = _LEGACY_SCAN_PAGE_SIZE * _LEGACY_SCAN_MAX_PAGES


class ControlRecordInitializationError(RuntimeError):
    """Signal that the manifest cannot be safely installed or validated."""


class ControlRecordMigrationRequired(ControlRecordInitializationError):
    """Signal that bounded online adoption must be replaced by an offline migration."""


@unique
class ControlRecordInitializationStatus(StrEnum):
    """Stable result without table data or provider error detail."""

    INITIALIZED = "initialized"
    ALREADY_INITIALIZED = "already_initialized"
    UPGRADE_REQUIRED = "upgrade_required"


@dataclass(frozen=True, slots=True)
class ControlRecordInitializationResult:
    """Content-free result returned to audited deployment tooling."""

    status: ControlRecordInitializationStatus
    manifest_version: int
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class ControlRecordSpec:
    """Typed schema for one fixed activity record in the shared table."""

    partition_key: str
    sort_key: str
    record_type: str
    record_schema_version: int | None = None
    counter_fields: tuple[str, ...] = ()
    slot: int | None = None
    marker: bool = False

    @property
    def key(self) -> DynamoItem:
        return {"PK": self.partition_key, "SK": self.sort_key}

    def base_item(self) -> DynamoItem:
        item: DynamoItem = {
            **self.key,
            "record_type": self.record_type,
            "schema_version": CURRENT_SCHEMA_VERSION,
        }
        if self.record_schema_version is not None:
            item["record_schema_version"] = self.record_schema_version
        for field in self.counter_fields:
            item[field] = 0
        if self.slot is not None:
            item["slot"] = self.slot
            item["fencing_token"] = 0
        if self.marker:
            item["manifest_version"] = CONTROL_RECORD_MANIFEST_VERSION
        return item

    @property
    def allowed_fields(self) -> frozenset[str]:
        fields = set(self.base_item())
        if not self.marker:
            fields.update({"created_at", "updated_at"})
        else:
            fields.add("manifest_hash")
        if self.slot is not None:
            fields.update({"lease_owner", "lease_expiry"})
        return frozenset(fields)

    @property
    def install_item(self) -> DynamoItem:
        item = self.base_item()
        if self.marker:
            item["manifest_hash"] = CONTROL_RECORD_MANIFEST_HASH
        return item


@dataclass(frozen=True, slots=True)
class ControlRecordManifest:
    """Immutable, hash-addressed contract for all deployment control records."""

    version: int
    manifest_hash: str
    activity_records: tuple[ControlRecordSpec, ...]
    initial_runtime_at: datetime

    @property
    def initial_runtime_item(self) -> DynamoItem:
        return serialize_runtime_state(RuntimeState.stopped(at=self.initial_runtime_at))

    @property
    def initial_deployment_lock_item(self) -> DynamoItem:
        return serialize_deployment_lock(DeploymentLock.open(at=self.initial_runtime_at))


_ACTIVITY_SPECS = (
    ControlRecordSpec(
        partition_key="CONTROL#RUNTIME",
        sort_key="ACTIVITY_SCHEMA",
        record_type=RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE,
        record_schema_version=RUNTIME_ACTIVITY_SCHEMA_VERSION,
        marker=True,
    ),
    ControlRecordSpec(
        partition_key="CONTROL#INGRESS",
        sort_key="COUNTER",
        record_type="ingress_queue_counter",
        record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
        counter_fields=("count",),
    ),
    ControlRecordSpec(
        partition_key="CONTROL#INGRESS",
        sort_key="STATUS_PENDING_COUNTER",
        record_type="ingress_status_pending_counter",
        record_schema_version=INGRESS_RECORD_SCHEMA_VERSION,
        counter_fields=("count",),
    ),
    ControlRecordSpec(
        partition_key="CONTROL#PANEL_REFRESH",
        sort_key="PENDING_COUNT",
        record_type="panel_refresh_pending_counter",
        counter_fields=("count",),
    ),
    ControlRecordSpec(
        partition_key="CONTROL#OUTBOX",
        sort_key="ACTIVITY",
        record_type="outbox_activity_counter",
        record_schema_version=OUTBOX_ACTIVITY_RECORD_SCHEMA_VERSION,
        counter_fields=("pending_count", "claimed_count"),
    ),
    ControlRecordSpec(
        partition_key="CONTROL#DEBATE",
        sort_key="ACTIVE_ATTEMPT_COUNT",
        record_type="active_attempt_counter",
        record_schema_version=ACTIVE_ATTEMPT_COUNTER_RECORD_SCHEMA_VERSION,
        counter_fields=("count",),
    ),
    *(
        ControlRecordSpec(
            partition_key="CONTROL#GLOBAL",
            sort_key=f"SLOT#{slot}",
            record_type="lease_slot",
            slot=slot,
        )
        for slot in range(GLOBAL_LEASE_SLOTS)
    ),
)

if len(_ACTIVITY_SPECS) != 9:  # pragma: no cover - import-time invariant
    raise AssertionError("activity control manifest must contain exactly nine records")


def _initial_runtime_item() -> DynamoItem:
    return serialize_runtime_state(RuntimeState.stopped(at=_INITIAL_RUNTIME_AT))


def _initial_deployment_lock_item() -> DynamoItem:
    return serialize_deployment_lock(DeploymentLock.open(at=_INITIAL_RUNTIME_AT))


def _with_schema_version(item: DynamoItem, schema_version: int) -> DynamoItem:
    versioned = dict(item)
    versioned["schema_version"] = schema_version
    return versioned


def _manifest_payload(*, schema_version: int = CURRENT_SCHEMA_VERSION) -> dict[str, object]:
    # The marker's hash field is intentionally absent from this canonical
    # payload; otherwise the digest would be self-referential.
    return {
        "manifest_version": CONTROL_RECORD_MANIFEST_VERSION,
        "records": [
            *(_with_schema_version(spec.base_item(), schema_version) for spec in _ACTIVITY_SPECS),
            _with_schema_version(_initial_runtime_item(), schema_version),
            _with_schema_version(_initial_deployment_lock_item(), schema_version),
        ],
    }


def _manifest_hash(*, schema_version: int = CURRENT_SCHEMA_VERSION) -> str:
    encoded = json.dumps(
        _manifest_payload(schema_version=schema_version),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


CONTROL_RECORD_MANIFEST_HASH = _manifest_hash()
CONTROL_RECORD_PREVIOUS_MANIFEST_HASH = _manifest_hash(schema_version=PREVIOUS_SCHEMA_VERSION)
CONTROL_RECORD_MANIFEST = ControlRecordManifest(
    version=CONTROL_RECORD_MANIFEST_VERSION,
    manifest_hash=CONTROL_RECORD_MANIFEST_HASH,
    activity_records=_ACTIVITY_SPECS,
    initial_runtime_at=_INITIAL_RUNTIME_AT,
)


class DynamoDbControlRecordInitializer:
    """Atomically bootstrap fixed control records without repairing marked state."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    def initialize(self) -> ControlRecordInitializationResult:
        """Validate an installed manifest or perform the one safe first install."""

        try:
            snapshot = self._read_snapshot()
            marker = snapshot[0]
            if marker is not None:
                self._validate_complete(snapshot)
                return self._result(ControlRecordInitializationStatus.ALREADY_INITIALIZED)

            self._require_no_legacy_active_work()
            self._validate_first_install_snapshot(snapshot)
            actions = self._first_install_actions(snapshot)
            try:
                self._client.transact_write_items(
                    TransactItems=list(actions),
                    ClientRequestToken=_client_token(self._table_name, snapshot),
                    ReturnConsumedCapacity="NONE",
                )
            except self._client.exceptions.TransactionCanceledException:
                # A concurrent identical initializer may have committed first.
                converged = self._read_snapshot()
                self._validate_complete(converged)
                return self._result(ControlRecordInitializationStatus.ALREADY_INITIALIZED)
            return self._result(ControlRecordInitializationStatus.INITIALIZED)
        except ControlRecordInitializationError:
            raise
        except BotoCoreError, ClientError, PersistenceFormatError, ValueError:
            raise ControlRecordInitializationError("control record initialization failed") from None

    def validate(self) -> ControlRecordInitializationResult:
        """Read and validate the complete manifest without ever repairing or writing it."""

        try:
            self._validate_complete(self._read_snapshot())
            return self._result(ControlRecordInitializationStatus.ALREADY_INITIALIZED)
        except ControlRecordInitializationError:
            raise
        except BotoCoreError, ClientError, PersistenceFormatError, ValueError:
            raise ControlRecordInitializationError("control record validation failed") from None

    def validate_compatible(self) -> ControlRecordInitializationResult:
        """Validate one uniform current or immediately previous installed manifest."""

        try:
            schema_version = self._validate_compatible_complete(self._read_snapshot())
            status = (
                ControlRecordInitializationStatus.ALREADY_INITIALIZED
                if schema_version == CURRENT_SCHEMA_VERSION
                else ControlRecordInitializationStatus.UPGRADE_REQUIRED
            )
            return self._result(status)
        except ControlRecordInitializationError:
            raise
        except BotoCoreError, ClientError, PersistenceFormatError, ValueError:
            raise ControlRecordInitializationError("control record validation failed") from None

    def _result(
        self,
        status: ControlRecordInitializationStatus,
    ) -> ControlRecordInitializationResult:
        return ControlRecordInitializationResult(
            status=status,
            manifest_version=CONTROL_RECORD_MANIFEST.version,
            manifest_hash=CONTROL_RECORD_MANIFEST.manifest_hash,
        )

    def _read_snapshot(self) -> tuple[DynamoItem | None, ...]:
        keys = (*_ACTIVITY_SPECS, _RuntimeStateSpec(), _DeploymentLockSpec())
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
            for spec in keys
        ]
        response = self._client.transact_get_items(
            TransactItems=actions,
            ReturnConsumedCapacity="NONE",
        )
        raw_responses = response.get("Responses", [])
        if len(raw_responses) != len(keys):
            raise ControlRecordInitializationError("control record snapshot has an invalid shape")
        return tuple(
            None if raw.get("Item") is None else unmarshal_item(raw["Item"])
            for raw in raw_responses
        )

    def _validate_complete(self, snapshot: tuple[DynamoItem | None, ...]) -> None:
        if self._validate_compatible_complete(snapshot) != CURRENT_SCHEMA_VERSION:
            raise ControlRecordInitializationError("control record schema upgrade is required")

    def _validate_compatible_complete(
        self,
        snapshot: tuple[DynamoItem | None, ...],
    ) -> int:
        if len(snapshot) != 11:
            raise ControlRecordInitializationError("control record snapshot has an invalid shape")
        schema_versions: set[int] = set()
        for spec, item in zip(_ACTIVITY_SPECS, snapshot[:9], strict=True):
            if item is None:
                raise ControlRecordInitializationError("installed control record is missing")
            schema_versions.add(_schema_version(item))
            _validate_activity_item(
                spec,
                item,
                require_idle=False,
                allowed_schema_versions=frozenset(
                    {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
                ),
            )
        state_item = snapshot[9]
        if state_item is None:
            raise ControlRecordInitializationError("installed runtime state is missing")
        schema_versions.add(_schema_version(state_item))
        _validate_runtime_item(
            state_item,
            require_stopped=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        lock_item = snapshot[10]
        if lock_item is None:
            raise ControlRecordInitializationError("installed deployment lock is missing")
        schema_versions.add(_schema_version(lock_item))
        _validate_deployment_lock_item(
            lock_item,
            require_open=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        if len(schema_versions) != 1:
            raise ControlRecordInitializationError("control record schema versions are mixed")
        return schema_versions.pop()

    def _validate_first_install_snapshot(
        self,
        snapshot: tuple[DynamoItem | None, ...],
    ) -> None:
        for spec, item in zip(_ACTIVITY_SPECS[1:], snapshot[1:9], strict=True):
            if item is not None:
                _validate_activity_item(
                    spec,
                    item,
                    require_idle=True,
                    allowed_schema_versions=frozenset(
                        {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
                    ),
                )
        state_item = snapshot[9]
        if state_item is not None:
            _validate_runtime_item(
                state_item,
                require_stopped=True,
                allowed_schema_versions=frozenset(
                    {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
                ),
            )
        lock_item = snapshot[10]
        if lock_item is not None:
            _validate_deployment_lock_item(
                lock_item,
                require_open=True,
                allowed_schema_versions=frozenset(
                    {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}
                ),
            )

    def _first_install_actions(
        self,
        snapshot: tuple[DynamoItem | None, ...],
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        specs: tuple[ControlRecordSpec | _RuntimeStateSpec | _DeploymentLockSpec, ...] = (
            *_ACTIVITY_SPECS,
            _RuntimeStateSpec(),
            _DeploymentLockSpec(),
        )
        actions: list[TransactWriteItemTypeDef] = []
        for spec, existing in zip(specs, snapshot, strict=True):
            if existing is None:
                actions.append(_put_missing(self._table_name, spec.install_item))
            elif _schema_version(existing) == PREVIOUS_SCHEMA_VERSION:
                actions.append(
                    _put_migrated(
                        self._table_name,
                        previous=existing,
                        current=_migrate_fixed_record(spec, existing),
                        allowed_fields=spec.allowed_fields,
                    )
                )
            else:
                actions.append(
                    _condition_exact(
                        self._table_name,
                        existing,
                        allowed_fields=spec.allowed_fields,
                    )
                )
        return tuple(actions)

    def _require_no_legacy_active_work(self) -> None:
        request: ScanInputTypeDef = cast(
            ScanInputTypeDef,
            {
                "TableName": self._table_name,
                "ConsistentRead": True,
                "Limit": _LEGACY_SCAN_PAGE_SIZE,
                "ProjectionExpression": ",".join(
                    f"#f{index}" for index in range(len(_LEGACY_SCAN_FIELDS))
                ),
                "ExpressionAttributeNames": {
                    f"#f{index}": field for index, field in enumerate(_LEGACY_SCAN_FIELDS)
                },
                "ReturnConsumedCapacity": "NONE",
            },
        )
        evaluated_items = 0
        for page_number in range(_LEGACY_SCAN_MAX_PAGES):
            page = self._client.scan(**request)
            scanned_count = page.get("ScannedCount")
            if (
                isinstance(scanned_count, bool)
                or not isinstance(scanned_count, int)
                or not 0 <= scanned_count <= _LEGACY_SCAN_PAGE_SIZE
            ):
                raise ControlRecordInitializationError("legacy scan response is invalid")
            evaluated_items += scanned_count
            if evaluated_items > _LEGACY_SCAN_MAX_EVALUATED_ITEMS:
                raise ControlRecordMigrationRequired("bounded legacy scan was exceeded")
            raw_items = page.get("Items", [])
            if len(raw_items) > scanned_count:
                raise ControlRecordInitializationError("legacy scan response is invalid")
            for raw_item in raw_items:
                item = unmarshal_item(raw_item)
                if _legacy_item_is_active(item):
                    raise ControlRecordInitializationError(
                        "legacy active work prevents control record initialization"
                    )
            last_key = page.get("LastEvaluatedKey")
            if not last_key:
                return
            if page_number + 1 == _LEGACY_SCAN_MAX_PAGES:
                raise ControlRecordMigrationRequired("bounded legacy scan was exceeded")
            request = cast(ScanInputTypeDef, {**request, "ExclusiveStartKey": last_key})

        raise ControlRecordMigrationRequired(  # pragma: no cover - loop always returns or raises
            "bounded legacy scan was exceeded"
        )


@dataclass(frozen=True, slots=True)
class _RuntimeStateSpec:
    @property
    def key(self) -> DynamoItem:
        return {"PK": "CONTROL#RUNTIME", "SK": "STATE"}

    @property
    def install_item(self) -> DynamoItem:
        return _initial_runtime_item()

    @property
    def allowed_fields(self) -> frozenset[str]:
        return frozenset(
            {
                *self.install_item,
                "runtime_instance_id",
                "runtime_prompt_revision",
                "wake_started_at",
                "last_request_at",
                "started_at",
                "ready_at",
                "busy_since",
                "idle_since",
                "stop_eligible_at",
                "stopping_at",
                "last_error_code",
                "last_reconciled_at",
            }
        )


@dataclass(frozen=True, slots=True)
class _DeploymentLockSpec:
    @property
    def key(self) -> DynamoItem:
        return {"PK": "CONTROL#DEPLOYMENT", "SK": "LOCK"}

    @property
    def install_item(self) -> DynamoItem:
        return _initial_deployment_lock_item()

    @property
    def allowed_fields(self) -> frozenset[str]:
        return frozenset(
            {
                *self.install_item,
                "guard_id",
                "lock_owner",
                "locked_at",
                "lock_expires_at",
                "deployment_mode",
                "break_glass_reason",
            }
        )


def _validate_activity_item(
    spec: ControlRecordSpec,
    item: DynamoItem,
    *,
    require_idle: bool,
    allowed_schema_versions: frozenset[int],
) -> None:
    schema_version = _schema_version(item)
    if schema_version not in allowed_schema_versions:
        raise ControlRecordInitializationError("control record schema is unsupported")
    expected = spec.install_item
    if spec.marker and schema_version == PREVIOUS_SCHEMA_VERSION:
        expected = _with_schema_version(expected, PREVIOUS_SCHEMA_VERSION)
        expected["manifest_hash"] = CONTROL_RECORD_PREVIOUS_MANIFEST_HASH
    if not set(item) <= spec.allowed_fields:
        raise ControlRecordInitializationError("control record has unknown attributes")
    for field, value in expected.items():
        if field in spec.counter_fields or field in {"fencing_token", "schema_version"}:
            continue
        if item.get(field) != value:
            raise ControlRecordInitializationError("control record schema is invalid")
    _validate_optional_timestamps(item)
    if spec.marker:
        if item != expected:
            raise ControlRecordInitializationError("control record marker is invalid")
        return
    if spec.slot is not None:
        token = item.get("fencing_token")
        if isinstance(token, bool) or not isinstance(token, int) or token < 0:
            raise ControlRecordInitializationError("control record lease token is invalid")
        owner = item.get("lease_owner")
        expiry = item.get("lease_expiry")
        if (owner is None) is not (expiry is None):
            raise ControlRecordInitializationError("control record lease ownership is invalid")
        if owner is not None:
            if not isinstance(owner, str) or not owner.strip() or not isinstance(expiry, str):
                raise ControlRecordInitializationError("control record lease ownership is invalid")
            _parse_utc(expiry)
            if require_idle:
                raise ControlRecordInitializationError("control record lease is active")
        return
    limits = {
        "ingress_queue_counter": INGRESS_QUEUE_LIMIT,
        "ingress_status_pending_counter": 100_000,
        "panel_refresh_pending_counter": PANEL_REFRESH_COUNT_LIMIT,
        "outbox_activity_counter": OUTBOX_ACTIVITY_LIMIT,
        "active_attempt_counter": ACTIVE_ATTEMPT_COUNT_LIMIT,
    }
    for field in spec.counter_fields:
        value = item.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= limits[spec.record_type]
            or (require_idle and value != 0)
        ):
            raise ControlRecordInitializationError("control record counter is invalid")


def _validate_runtime_item(
    item: DynamoItem,
    *,
    require_stopped: bool,
    allowed_schema_versions: frozenset[int],
) -> DynamoItem:
    raw_schema = _schema_version(item)
    if raw_schema not in allowed_schema_versions:
        raise ControlRecordInitializationError("runtime state schema is unsupported")
    state = deserialize_runtime_state(item)
    canonical = serialize_runtime_state(state)
    if raw_schema == PREVIOUS_SCHEMA_VERSION:
        canonical["schema_version"] = raw_schema
    if item != canonical:
        raise ControlRecordInitializationError("runtime state has unknown attributes")
    if require_stopped and state.status is not RuntimeStatus.STOPPED:
        raise ControlRecordInitializationError("legacy runtime state is active")
    canonical["schema_version"] = CURRENT_SCHEMA_VERSION
    return canonical


def _validate_deployment_lock_item(
    item: DynamoItem,
    *,
    require_open: bool,
    allowed_schema_versions: frozenset[int],
) -> DynamoItem:
    raw_schema = _schema_version(item)
    if raw_schema not in allowed_schema_versions:
        raise ControlRecordInitializationError("deployment lock schema is unsupported")
    lock = deserialize_deployment_lock(item)
    canonical = serialize_deployment_lock(lock)
    if raw_schema == PREVIOUS_SCHEMA_VERSION:
        canonical["schema_version"] = raw_schema
    if item != canonical:
        raise ControlRecordInitializationError("deployment lock has unknown attributes")
    if require_open and lock.state is not DeploymentLockState.OPEN:
        raise ControlRecordInitializationError("legacy deployment lock is active")
    canonical["schema_version"] = CURRENT_SCHEMA_VERSION
    return canonical


def _migrate_fixed_record(
    spec: ControlRecordSpec | _RuntimeStateSpec | _DeploymentLockSpec,
    item: DynamoItem,
) -> DynamoItem:
    if isinstance(spec, _RuntimeStateSpec):
        return _validate_runtime_item(
            item,
            require_stopped=True,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION}),
        )
    if isinstance(spec, _DeploymentLockSpec):
        return _validate_deployment_lock_item(
            item,
            require_open=True,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION}),
        )
    _validate_activity_item(
        spec,
        item,
        require_idle=True,
        allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION}),
    )
    migrated = dict(item)
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    if spec.marker:
        migrated["manifest_hash"] = CONTROL_RECORD_MANIFEST_HASH
    return migrated


def _convert_fixed_record_schema(
    spec: ControlRecordSpec | _RuntimeStateSpec | _DeploymentLockSpec,
    item: DynamoItem,
    *,
    schema_version: int,
) -> DynamoItem:
    """Convert only the shared schema and marker digest after typed validation."""

    if schema_version not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}:
        raise ValueError("control record target schema is unsupported")
    converted = dict(item)
    converted["schema_version"] = schema_version
    if isinstance(spec, ControlRecordSpec) and spec.marker:
        converted["manifest_hash"] = (
            CONTROL_RECORD_MANIFEST_HASH
            if schema_version == CURRENT_SCHEMA_VERSION
            else CONTROL_RECORD_PREVIOUS_MANIFEST_HASH
        )
    return converted


def _put_missing(table_name: str, item: DynamoItem) -> TransactWriteItemTypeDef:
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


def _condition_exact(
    table_name: str,
    item: DynamoItem,
    *,
    allowed_fields: frozenset[str],
) -> TransactWriteItemTypeDef:
    condition = _exact_condition(item, allowed_fields=allowed_fields)
    action: dict[str, object] = {
        "TableName": table_name,
        "Key": marshal_item({"PK": item["PK"], "SK": item["SK"]}),
        **condition,
    }
    return cast(TransactWriteItemTypeDef, {"ConditionCheck": action})


def _put_migrated(
    table_name: str,
    *,
    previous: DynamoItem,
    current: DynamoItem,
    allowed_fields: frozenset[str],
) -> TransactWriteItemTypeDef:
    return cast(
        TransactWriteItemTypeDef,
        {
            "Put": {
                "TableName": table_name,
                "Item": marshal_item(current),
                **_exact_condition(previous, allowed_fields=allowed_fields),
            }
        },
    )


def _exact_condition(
    item: DynamoItem,
    *,
    allowed_fields: frozenset[str],
) -> dict[str, object]:
    names = {f"#f{index}": field for index, field in enumerate(sorted(allowed_fields))}
    values: DynamoItem = {}
    conditions: list[str] = []
    for placeholder, field in names.items():
        if field in item:
            value_placeholder = f":v{len(values)}"
            values[value_placeholder] = item[field]
            conditions.append(f"{placeholder}={value_placeholder}")
        else:
            conditions.append(f"attribute_not_exists({placeholder})")
    condition: dict[str, object] = {
        "ConditionExpression": " AND ".join(conditions),
        "ExpressionAttributeNames": names,
    }
    if values:
        condition["ExpressionAttributeValues"] = marshal_item(values)
    return condition


def _client_token(
    table_name: str,
    snapshot: tuple[DynamoItem | None, ...],
) -> str:
    encoded = json.dumps(
        {
            "manifest_hash": CONTROL_RECORD_MANIFEST_HASH,
            "snapshot": snapshot,
            "table_name": table_name,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{_CLIENT_REQUEST_ID_PREFIX}{digest[:33]}"


_LEGACY_SCAN_FIELDS = (
    "PK",
    "SK",
    "record_type",
    "schema_version",
    "record_schema_version",
    "phase",
    "current_phase",
    "recovery_state",
    "status",
    "state",
    "publication_state",
    "lease_owner",
    "lease_expiry",
    "claim_owner",
    "claim_expiry",
    "count",
    "pending_count",
    "claimed_count",
    "panel_refresh_required_at",
    "panel_refreshed_at",
    "panel_refresh_failed_at",
    "terminal_delivery_target",
    "terminal_delivery_operation_ids",
    "terminal_delivery_content_hashes",
    "terminal_delivery_staged_at",
    "terminal_delivery_completed_at",
    "terminal_delivery_plan_id",
    "terminal_delivery_source",
    "terminal_delivery_sequences",
    "terminal_delivery_deadline_at",
    "terminal_delivery_plan_status",
    "terminal_delivery_abandon_reason",
    "generation_checkpoints",
    "plan_id",
    "source_phase",
    "target_phase",
    "operation_ids",
    "content_hashes",
    "delivery_sequences",
    "deadline_at",
    "settled_at",
    "abandoned_at",
    "abandon_reason",
    "delivery_sequence",
    "manifest_version",
    "manifest_hash",
    "slot",
    "fencing_token",
    "created_at",
    "updated_at",
    "attempt_id",
    "debate_id",
    "current_attempt_id",
    "attempt_created_at",
    "operation_id",
    "bot_slot",
    "thread_id",
    "content_hash",
    "nonce",
    "chunk_sequence",
    "interaction_id",
    "canonical_interaction_id",
    "request_sort_key",
    "accepted_debate_id",
    "accepted_attempt_id",
    "completed_at",
    "startup_deadline_at",
    "terminal_deadline_at",
    "processing_started_at",
    "next_retry_at",
    "next_attempt_at",
    "delivery_attempt",
    "message_id",
    "sent_at",
    "status_message_state",
    "desired_state",
    "delivered_state",
    "status_message_id",
    "status_message_updated_at",
    "history_reconciliation_required",
    "incarnation",
    "error_code",
    "sequence",
    "participant",
    "voter",
    "kind",
    "generation",
    "desired_count",
    "version",
    "runtime_version",
    "recorded_at",
    "lock_state",
    "guard_id",
    "lock_owner",
    "locked_at",
    "lock_expires_at",
    "deployment_mode",
    "break_glass_reason",
)

_INACTIVE_IMMUTABLE_RECORD_TYPES = frozenset(
    {
        "decision",
        "escalation_assessment",
        "evidence",
        "evidence_meta",
        "final_proposal",
        "ingress_semantic_operation_binding",
        "initial_opinion",
        "panel_operation",
        "runtime_wake_result",
        "vote",
    }
)

_CONTROL_RECORD_TYPES = frozenset(spec.record_type for spec in _ACTIVITY_SPECS)


def _legacy_item_is_active(item: DynamoItem) -> bool:
    _required_text(item, "PK")
    _required_text(item, "SK")
    record_type = _required_text(item, "record_type")
    schema_version = _schema_version(item)
    if schema_version not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}:
        raise ControlRecordInitializationError("legacy schema version is unsupported")
    if record_type == RUNTIME_ACTIVITY_SCHEMA_RECORD_TYPE:
        # The strongly consistent manifest snapshot was marker-free. Seeing the
        # marker in the later bounded scan means initialization raced or the
        # table is inconsistent; neither case is safe to classify as legacy.
        raise ControlRecordInitializationError("control record marker changed during adoption")
    if record_type in _CONTROL_RECORD_TYPES:
        spec = _legacy_control_spec(item, record_type=record_type)
        _validate_activity_item(
            spec,
            item,
            require_idle=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        if spec.slot is not None:
            return item.get("lease_owner") is not None or item.get("lease_expiry") is not None
        return any(
            _required_non_negative_integer(item, field) != 0 for field in spec.counter_fields
        )
    if record_type == "deployment_lock":
        canonical = _validate_deployment_lock_item(
            item,
            require_open=False,
            allowed_schema_versions=frozenset({PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}),
        )
        return canonical.get("lock_state") == DeploymentLockState.LOCKED.value
    if record_type in {"attempt_meta", "debate_meta"}:
        _validate_debate_activity_identity(item, record_type=record_type)
        phase_field = "phase" if record_type == "attempt_meta" else "current_phase"
        phase = _enum_value(DebatePhase, item, phase_field)
        generation_is_active = False
        if record_type == "attempt_meta":
            generation_is_active = _validate_generation_checkpoint_collection(item)
            recovery_state = _required_text(item, "recovery_state")
            if recovery_state == "checkpointed":
                return True
            if recovery_state != "none":
                raise ControlRecordInitializationError("legacy recovery state is invalid")
        if not phase.is_terminal:
            return True
        if record_type == "attempt_meta":
            if generation_is_active:
                return True
            if _panel_refresh_is_pending(item):
                return True
            if schema_version == CURRENT_SCHEMA_VERSION:
                target = item.get("terminal_delivery_target")
                completed = item.get("terminal_delivery_completed_at")
                if target != phase.value or not isinstance(completed, str):
                    return True
                _validate_terminal_delivery(item)
                _parse_utc(completed)
        return False
    if record_type == "outbox":
        _validate_outbox_identity(item)
        status = _enum_value(OutboxStatus, item, "status")
        if status in {OutboxStatus.PREPARED, OutboxStatus.CLAIMED}:
            return True
        if status is OutboxStatus.SENT:
            _validate_sent_outbox(item)
        else:
            _validate_abandoned_outbox(item)
        return False
    if record_type == "phase_delivery_plan":
        status = _validate_phase_delivery_plan(item)
        return status in {PhaseDeliveryStatus.STAGED, PhaseDeliveryStatus.TERMINATING}
    if record_type in {"ingress_request", "ingress_operation_result"}:
        _require_record_schema_version(item, expected=1)
        _validate_ingress_identity(item, record_type=record_type)
        status = _enum_value(IngressStatus, item, "status")
        if status not in {
            IngressStatus.COMPLETED,
            IngressStatus.REJECTED,
            IngressStatus.FAILED,
        }:
            return True
        _validate_terminal_ingress(item, record_type=record_type, status=status)
        return False
    if record_type == "ingress_active_pointer":
        _require_record_schema_version(item, expected=1)
        _validate_ingress_active_pointer_identity(item)
        return True
    if record_type == "ingress_status_publication":
        _require_record_schema_version(item, expected=3)
        _validate_status_publication_identity(item)
        state = _enum_value(StatusPublicationState, item, "publication_state")
        if state in {
            StatusPublicationState.PREPARED,
            StatusPublicationState.CLAIMED,
            StatusPublicationState.RETRYING,
        }:
            return True
        _validate_settled_status_publication(item, state=state)
        return False
    if record_type == "runtime_state":
        _require_record_schema_version(item, expected=1)
        _validate_runtime_state_shape(item)
        return _enum_value(RuntimeStatus, item, "state") is not RuntimeStatus.STOPPED
    if record_type in {"guild_daily_quota", "panel_refresh_abandoned_counter"}:
        _validate_inactive_counter(item, record_type=record_type)
        return False
    if record_type in _INACTIVE_IMMUTABLE_RECORD_TYPES:
        _validate_inactive_immutable(item, record_type=record_type)
        return False
    raise ControlRecordInitializationError("legacy record type is unsupported")


def _legacy_control_spec(item: DynamoItem, *, record_type: str) -> ControlRecordSpec:
    partition_key = _required_text(item, "PK")
    sort_key = _required_text(item, "SK")
    for spec in _ACTIVITY_SPECS:
        if (
            spec.record_type == record_type
            and spec.partition_key == partition_key
            and spec.sort_key == sort_key
        ):
            return spec
    raise ControlRecordInitializationError("legacy control record key is invalid")


def _validate_debate_activity_identity(item: DynamoItem, *, record_type: str) -> None:
    debate_id = _required_text(item, "debate_id")
    partition_key = _required_text(item, "PK")
    sort_key = _required_text(item, "SK")
    if partition_key != f"DEBATE#{debate_id}":
        raise ControlRecordInitializationError("debate activity partition key is invalid")
    _required_utc(item, "created_at")
    _required_utc(item, "updated_at")
    if record_type == "debate_meta":
        _required_text(item, "current_attempt_id")
        if sort_key != "META":
            raise ControlRecordInitializationError("debate metadata sort key is invalid")
        return
    attempt_id = _required_text(item, "attempt_id")
    _required_utc(item, "attempt_created_at")
    if sort_key != f"ATTEMPT#{attempt_id}#META":
        raise ControlRecordInitializationError("attempt metadata sort key is invalid")


def _validate_terminal_delivery(item: DynamoItem) -> None:
    operation_ids = _required_text_list(item, "terminal_delivery_operation_ids")
    content_hashes = _required_text_list(item, "terminal_delivery_content_hashes")
    if not operation_ids or len(operation_ids) != len(content_hashes):
        raise ControlRecordInitializationError("terminal delivery plan is invalid")
    _required_utc(item, "terminal_delivery_staged_at")
    _required_utc(item, "terminal_delivery_completed_at")
    pointer_fields = (
        "terminal_delivery_plan_id",
        "terminal_delivery_source",
        "terminal_delivery_sequences",
        "terminal_delivery_deadline_at",
        "terminal_delivery_plan_status",
    )
    present = tuple(field in item for field in pointer_fields)
    if any(present) and not all(present):
        raise ControlRecordInitializationError("phase delivery pointer is incomplete")
    if all(present):
        _required_text(item, "terminal_delivery_plan_id")
        _enum_value(DebatePhase, item, "terminal_delivery_source")
        _required_non_negative_integer_list(item, "terminal_delivery_sequences")
        _required_utc(item, "terminal_delivery_deadline_at")
        status = _enum_value(
            PhaseDeliveryStatus,
            item,
            "terminal_delivery_plan_status",
        )
        if status is PhaseDeliveryStatus.ABANDONED:
            _enum_value(
                DeliveryAbandonReason,
                item,
                "terminal_delivery_abandon_reason",
            )
        elif "terminal_delivery_abandon_reason" in item:
            raise ControlRecordInitializationError(
                "settled phase delivery pointer retains an abandonment reason"
            )
    elif "terminal_delivery_abandon_reason" in item:
        raise ControlRecordInitializationError("phase delivery reason has no pointer")


def _validate_generation_checkpoint_collection(item: DynamoItem) -> bool:
    value = item.get("generation_checkpoints")
    if value is None:
        return False
    if not isinstance(value, list):
        raise ControlRecordInitializationError("generation checkpoints are invalid")
    required = {
        "record_schema_version",
        "phase",
        "participant",
        "status",
        "logical_attempt",
        "planned_at",
    }
    optional = {
        "claim_owner",
        "claim_slot",
        "claim_fencing_token",
        "claimed_at",
        "settled_at",
        "error_code",
    }
    identities: set[tuple[DebatePhase, ParticipantSlot]] = set()
    has_unsettled = False
    for raw in value:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ControlRecordInitializationError("generation checkpoint is invalid")
        checkpoint = raw
        fields = set(checkpoint)
        if not required.issubset(fields) or not fields.issubset(required | optional):
            raise ControlRecordInitializationError("generation checkpoint schema is invalid")
        try:
            model = GenerationCheckpoint(
                phase=_enum_value(DebatePhase, checkpoint, "phase"),
                participant=_enum_value(ParticipantSlot, checkpoint, "participant"),
                status=_enum_value(GenerationStatus, checkpoint, "status"),
                logical_attempt=_required_non_negative_integer(
                    checkpoint,
                    "logical_attempt",
                ),
                planned_at=_required_utc(checkpoint, "planned_at"),
                claim_owner=(
                    _required_text(checkpoint, "claim_owner")
                    if "claim_owner" in checkpoint
                    else None
                ),
                claim_slot=(
                    _required_non_negative_integer(checkpoint, "claim_slot")
                    if "claim_slot" in checkpoint
                    else None
                ),
                claim_fencing_token=(
                    _required_non_negative_integer(checkpoint, "claim_fencing_token")
                    if "claim_fencing_token" in checkpoint
                    else None
                ),
                claimed_at=(
                    _required_utc(checkpoint, "claimed_at") if "claimed_at" in checkpoint else None
                ),
                settled_at=(
                    _required_utc(checkpoint, "settled_at") if "settled_at" in checkpoint else None
                ),
                error_code=(
                    _required_text(checkpoint, "error_code") if "error_code" in checkpoint else None
                ),
                record_schema_version=_required_non_negative_integer(
                    checkpoint,
                    "record_schema_version",
                ),
            )
        except (TypeError, ValueError) as error:
            raise ControlRecordInitializationError("generation checkpoint is invalid") from error
        identity = (model.phase, model.participant)
        if identity in identities:
            raise ControlRecordInitializationError("generation checkpoint is duplicated")
        identities.add(identity)
        has_unsettled = has_unsettled or model.status in {
            GenerationStatus.PLANNED,
            GenerationStatus.IN_FLIGHT,
        }
    return has_unsettled


def _validate_outbox_identity(item: DynamoItem) -> None:
    debate_id = _required_text(item, "debate_id")
    attempt_id = _required_text(item, "attempt_id")
    operation_id = _required_text(item, "operation_id")
    if (
        _required_text(item, "PK") != f"DEBATE#{debate_id}"
        or _required_text(item, "SK") != f"ATTEMPT#{attempt_id}#OUTBOX#{operation_id}"
    ):
        raise ControlRecordInitializationError("outbox key is invalid")
    _required_utc(item, "created_at")
    _required_utc(item, "updated_at")
    record_schema_version = item.get("record_schema_version", 1)
    if (
        isinstance(record_schema_version, bool)
        or not isinstance(record_schema_version, int)
        or record_schema_version not in {1, 2}
    ):
        raise ControlRecordInitializationError("outbox record schema is unsupported")
    v2_fields = ("phase", "plan_id", "delivery_sequence", "deadline_at")
    if record_schema_version == 1:
        if any(field in item for field in (*v2_fields, "abandoned_at", "abandon_reason")):
            raise ControlRecordInitializationError("outbox v1 contains v2 fields")
        return
    _enum_value(DebatePhase, item, "phase")
    _required_text(item, "plan_id")
    _required_non_negative_integer(item, "delivery_sequence")
    deadline = _required_utc(item, "deadline_at")
    if deadline != _required_utc(item, "created_at") + timedelta(minutes=15):
        raise ControlRecordInitializationError("outbox deadline is invalid")
    content_hash = _required_text(item, "content_hash")
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ControlRecordInitializationError("outbox content hash is invalid")


def _validate_sent_outbox(item: DynamoItem) -> None:
    _required_text(item, "message_id")
    sent_at = _required_utc(item, "sent_at")
    created_at = _required_utc(item, "created_at")
    updated_at = _required_utc(item, "updated_at")
    if sent_at < created_at or updated_at != sent_at:
        raise ControlRecordInitializationError("sent outbox timestamps are invalid")
    if _required_non_negative_integer(item, "delivery_attempt") < 1:
        raise ControlRecordInitializationError("sent outbox delivery attempt is invalid")
    _require_absent(
        item,
        "claim_owner",
        "claim_expiry",
        "next_retry_at",
        "abandoned_at",
        "abandon_reason",
    )


def _validate_abandoned_outbox(item: DynamoItem) -> None:
    _require_record_schema_version(item, expected=2)
    abandoned_at = _required_utc(item, "abandoned_at")
    created_at = _required_utc(item, "created_at")
    updated_at = _required_utc(item, "updated_at")
    if abandoned_at < created_at or updated_at != abandoned_at:
        raise ControlRecordInitializationError("abandoned outbox timestamps are invalid")
    _enum_value(DeliveryAbandonReason, item, "abandon_reason")
    _require_absent(
        item,
        "claim_owner",
        "claim_expiry",
        "next_retry_at",
        "message_id",
        "sent_at",
    )


def _validate_phase_delivery_plan(item: DynamoItem) -> PhaseDeliveryStatus:
    _require_record_schema_version(item, expected=2)
    debate_id = _required_text(item, "debate_id")
    attempt_id = _required_text(item, "attempt_id")
    plan_id = _required_text(item, "plan_id")
    if (
        _required_text(item, "PK") != f"DEBATE#{debate_id}"
        or _required_text(item, "SK") != f"ATTEMPT#{attempt_id}#DELIVERY#{plan_id}"
    ):
        raise ControlRecordInitializationError("phase delivery plan key is invalid")
    source = _enum_value(DebatePhase, item, "source_phase")
    target = _enum_value(DebatePhase, item, "target_phase")
    if source.is_terminal or source is target:
        raise ControlRecordInitializationError("phase delivery transition is invalid")
    operation_ids = _required_text_list(item, "operation_ids")
    content_hashes = _required_text_list(item, "content_hashes")
    sequences = _required_non_negative_integer_list(item, "delivery_sequences")
    if (
        not operation_ids
        or len(operation_ids) != len(set(operation_ids))
        or len(content_hashes) != len(operation_ids)
        or len(sequences) != len(operation_ids)
        or sequences != tuple(sorted(sequences))
        or len(sequences) != len(set(sequences))
    ):
        raise ControlRecordInitializationError("phase delivery operation mapping is invalid")
    for content_hash in content_hashes:
        if len(content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in content_hash
        ):
            raise ControlRecordInitializationError("phase delivery content hash is invalid")
    staged_at = _required_utc(item, "staged_at")
    deadline_at = _required_utc(item, "deadline_at")
    updated_at = _required_utc(item, "updated_at")
    if deadline_at != staged_at + timedelta(minutes=15) or updated_at < staged_at:
        raise ControlRecordInitializationError("phase delivery timestamps are invalid")
    status = _enum_value(PhaseDeliveryStatus, item, "status")
    if status is PhaseDeliveryStatus.STAGED:
        _require_absent(item, "settled_at", "abandon_reason")
    elif status is PhaseDeliveryStatus.TERMINATING:
        _require_absent(item, "settled_at")
        _enum_value(DeliveryAbandonReason, item, "abandon_reason")
    elif status is PhaseDeliveryStatus.DELIVERED:
        settled_at = _required_utc(item, "settled_at")
        if settled_at < staged_at or updated_at != settled_at:
            raise ControlRecordInitializationError("delivered phase timestamps are invalid")
        _require_absent(item, "abandon_reason")
    else:
        settled_at = _required_utc(item, "settled_at")
        if settled_at < staged_at or updated_at != settled_at:
            raise ControlRecordInitializationError("abandoned phase timestamps are invalid")
        _enum_value(DeliveryAbandonReason, item, "abandon_reason")
    return status


def _validate_ingress_identity(item: DynamoItem, *, record_type: str) -> None:
    interaction_id = _required_text(item, "interaction_id")
    _required_text(item, "operation_id")
    created_at = _required_utc(item, "created_at")
    _required_utc(item, "updated_at")
    partition_key = _required_text(item, "PK")
    sort_key = _required_text(item, "SK")
    if record_type == "ingress_request":
        expected_sort_key = _ingress_request_sort_key(created_at, interaction_id)
        if partition_key != "CONTROL#INGRESS" or sort_key != expected_sort_key:
            raise ControlRecordInitializationError("ingress request key is invalid")
        return
    request_sort_key = _required_text(item, "request_sort_key")
    if (
        partition_key != f"INGRESS_OPERATION#{interaction_id}"
        or sort_key != "RESULT"
        or request_sort_key != _ingress_request_sort_key(created_at, interaction_id)
    ):
        raise ControlRecordInitializationError("ingress operation result key is invalid")


def _validate_terminal_ingress(
    item: DynamoItem,
    *,
    record_type: str,
    status: IngressStatus,
) -> None:
    created_at = _required_utc(item, "created_at")
    updated_at = _required_utc(item, "updated_at")
    if updated_at < created_at:
        raise ControlRecordInitializationError("terminal ingress timestamps are invalid")
    accepted_debate_id, _ = _optional_text_pair(
        item,
        "accepted_debate_id",
        "accepted_attempt_id",
    )
    if status is IngressStatus.COMPLETED:
        if accepted_debate_id is None or "error_code" in item:
            raise ControlRecordInitializationError("completed ingress result is invalid")
    else:
        _required_text(item, "error_code")

    if record_type == "ingress_operation_result":
        return

    completed_at = _required_utc(item, "completed_at")
    if completed_at != updated_at:
        raise ControlRecordInitializationError("terminal ingress completion is invalid")
    startup_deadline = _required_utc(item, "startup_deadline_at")
    terminal_deadline = _required_utc(item, "terminal_deadline_at")
    if startup_deadline != created_at + STARTUP_TIMEOUT:
        raise ControlRecordInitializationError("ingress startup deadline is invalid")
    if terminal_deadline != created_at + TERMINAL_TIMEOUT:
        raise ControlRecordInitializationError("ingress terminal deadline is invalid")
    if "processing_started_at" in item:
        processing_started_at = item["processing_started_at"]
        if not isinstance(processing_started_at, str):
            raise ControlRecordInitializationError("legacy processing_started_at is invalid")
        parsed_processing_started_at = _parse_utc(processing_started_at)
        if not created_at <= parsed_processing_started_at < terminal_deadline:
            raise ControlRecordInitializationError("ingress processing timestamp is invalid")
    _required_non_negative_integer(item, "delivery_attempt")
    _optional_text_pair(item, "status_message_id", "status_message_updated_at", second_utc=True)
    expected_message_state = {
        IngressStatus.COMPLETED: StatusMessageState.COMPLETED,
        IngressStatus.REJECTED: StatusMessageState.REJECTED,
        IngressStatus.FAILED: StatusMessageState.TERMINAL_FAILED,
    }[status]
    if _enum_value(StatusMessageState, item, "status_message_state") is not expected_message_state:
        raise ControlRecordInitializationError("terminal ingress message state is invalid")
    _require_absent(item, "claim_owner", "claim_expiry", "next_attempt_at")


def _validate_ingress_active_pointer_identity(item: DynamoItem) -> None:
    interaction_id = _required_text(item, "interaction_id")
    request_sort_key = _required_text(item, "request_sort_key")
    created_at = _required_utc(item, "created_at")
    if (
        _required_text(item, "PK") != "CONTROL#INGRESS#ACTIVE"
        or _required_text(item, "SK") != request_sort_key
        or request_sort_key != _ingress_request_sort_key(created_at, interaction_id)
    ):
        raise ControlRecordInitializationError("ingress active pointer key is invalid")


def _validate_status_publication_identity(item: DynamoItem) -> None:
    interaction_id = _required_text(item, "canonical_interaction_id")
    request_sort_key = _required_text(item, "request_sort_key")
    if (
        _required_text(item, "PK") != f"INGRESS_OPERATION#{interaction_id}"
        or _required_text(item, "SK") != "STATUS_PUBLICATION"
    ):
        raise ControlRecordInitializationError("status publication key is invalid")
    created_at = _required_utc(item, "created_at")
    updated_at = _required_utc(item, "updated_at")
    if request_sort_key != _ingress_request_sort_key(created_at, interaction_id):
        raise ControlRecordInitializationError("status publication request key is invalid")
    if updated_at < created_at:
        raise ControlRecordInitializationError("status publication timestamps are invalid")


def _validate_settled_status_publication(
    item: DynamoItem,
    *,
    state: StatusPublicationState,
) -> None:
    if state not in {StatusPublicationState.DELIVERED, StatusPublicationState.FAILED}:
        raise ControlRecordInitializationError("status publication state is invalid")
    desired_state = _enum_value(StatusMessageState, item, "desired_state")
    message_id, _ = _optional_text_pair(
        item,
        "status_message_id",
        "status_message_updated_at",
        second_utc=True,
    )
    if _required_non_negative_integer(item, "delivery_attempt") < 1:
        raise ControlRecordInitializationError("settled status delivery attempt is invalid")
    _required_non_negative_integer(item, "incarnation")
    history_reconciliation_required = _required_boolean(
        item,
        "history_reconciliation_required",
    )
    _require_absent(item, "claim_owner", "claim_expiry", "next_attempt_at")

    delivered_state = (
        _enum_value(StatusMessageState, item, "delivered_state")
        if "delivered_state" in item
        else None
    )
    if state is StatusPublicationState.DELIVERED:
        if delivered_state is not desired_state or message_id is None:
            raise ControlRecordInitializationError("delivered status publication is invalid")
        if history_reconciliation_required or "error_code" in item:
            raise ControlRecordInitializationError("delivered status settlement is invalid")
        return
    _required_text(item, "error_code")


def _validate_runtime_state_shape(item: DynamoItem) -> None:
    if _required_text(item, "PK") != "CONTROL#RUNTIME" or _required_text(item, "SK") != "STATE":
        raise ControlRecordInitializationError("runtime state key is invalid")
    _required_non_negative_integer(item, "generation")
    desired_count = _required_non_negative_integer(item, "desired_count")
    if desired_count not in {0, 1}:
        raise ControlRecordInitializationError("runtime desired count is invalid")
    _required_non_negative_integer(item, "version")
    _required_utc(item, "updated_at")


def _ingress_request_sort_key(created_at: datetime, interaction_id: str) -> str:
    timestamp = created_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"REQUEST#{timestamp}#{interaction_id}"


def _validate_inactive_counter(item: DynamoItem, *, record_type: str) -> None:
    partition_key = _required_text(item, "PK")
    sort_key = _required_text(item, "SK")
    _required_non_negative_integer(item, "count")
    if record_type == "panel_refresh_abandoned_counter":
        if partition_key != "CONTROL#PANEL_REFRESH" or sort_key != "ABANDONED_COUNT":
            raise ControlRecordInitializationError("inactive counter key is invalid")
        _required_utc(item, "updated_at")
        return
    quota_prefix = "QUOTA#GUILD#"
    day_prefix = "DAY#"
    if not partition_key.startswith(quota_prefix) or not sort_key.startswith(day_prefix):
        raise ControlRecordInitializationError("daily quota key is invalid")
    if not partition_key.removeprefix(quota_prefix).strip():
        raise ControlRecordInitializationError("daily quota guild is invalid")
    day_text = sort_key.removeprefix(day_prefix)
    try:
        parsed_day = date.fromisoformat(day_text)
    except ValueError:
        raise ControlRecordInitializationError("daily quota date is invalid") from None
    if parsed_day.isoformat() != day_text:
        raise ControlRecordInitializationError("daily quota date is invalid")
    _required_utc(item, "created_at")
    _required_utc(item, "updated_at")


def _validate_inactive_immutable(item: DynamoItem, *, record_type: str) -> None:
    partition_key = _required_text(item, "PK")
    sort_key = _required_text(item, "SK")
    if record_type in {
        "decision",
        "escalation_assessment",
        "evidence",
        "evidence_meta",
        "final_proposal",
        "initial_opinion",
        "vote",
    }:
        debate_id = _required_text(item, "debate_id")
        attempt_id = _required_text(item, "attempt_id")
        expected_sort_key = _artifact_sort_key(item, record_type=record_type, attempt_id=attempt_id)
        if partition_key != f"DEBATE#{debate_id}" or sort_key != expected_sort_key:
            raise ControlRecordInitializationError("immutable debate artifact key is invalid")
        _required_utc(item, "created_at")
        _required_utc(item, "updated_at")
        return
    if record_type == "panel_operation":
        operation_id = _required_text(item, "operation_id")
        _enum_value(PanelOperationKind, item, "kind")
        if partition_key != f"OPERATION#{operation_id}" or sort_key != "RESULT":
            raise ControlRecordInitializationError("panel operation key is invalid")
        _required_utc(item, "created_at")
        return
    if record_type == "ingress_semantic_operation_binding":
        _require_record_schema_version(item, expected=1)
        operation_id = _required_text(item, "operation_id")
        _required_text(item, "canonical_interaction_id")
        _required_text(item, "request_sort_key")
        if partition_key != f"INGRESS_SEMANTIC_OPERATION#{operation_id}" or sort_key != "BINDING":
            raise ControlRecordInitializationError("semantic operation binding key is invalid")
        _required_utc(item, "created_at")
        return
    if record_type == "runtime_wake_result":
        _require_record_schema_version(item, expected=1)
        interaction_id = _required_text(item, "interaction_id")
        if partition_key != f"INGRESS_OPERATION#{interaction_id}" or sort_key != "RUNTIME_WAKE":
            raise ControlRecordInitializationError("runtime wake result key is invalid")
        for field in ("generation", "runtime_version"):
            if _required_non_negative_integer(item, field) < 1:
                raise ControlRecordInitializationError("runtime wake result version is invalid")
        _required_utc(item, "recorded_at")
        return
    raise ControlRecordInitializationError("inactive record type is unsupported")


def _artifact_sort_key(item: DynamoItem, *, record_type: str, attempt_id: str) -> str:
    fixed_suffixes = {
        "decision": "DECISION",
        "escalation_assessment": "ESCALATION",
        "evidence_meta": "EVIDENCE#META",
    }
    suffix = fixed_suffixes.get(record_type)
    if suffix is not None:
        return f"ATTEMPT#{attempt_id}#{suffix}"
    if record_type == "evidence":
        sequence = _required_non_negative_integer(item, "sequence")
        return f"ATTEMPT#{attempt_id}#EVIDENCE#{sequence:04d}"
    if record_type in {"initial_opinion", "final_proposal"}:
        participant = _enum_value(ParticipantSlot, item, "participant")
        kind = "INITIAL" if record_type == "initial_opinion" else "FINAL"
        return f"ATTEMPT#{attempt_id}#{kind}#{participant.value}"
    if record_type == "vote":
        voter = _enum_value(ParticipantSlot, item, "voter")
        return f"ATTEMPT#{attempt_id}#VOTE#{voter.value}"
    raise ControlRecordInitializationError("immutable debate artifact type is invalid")


def _panel_refresh_is_pending(item: Mapping[str, DynamoValue]) -> bool:
    parsed: dict[str, datetime] = {}
    for field in (
        "panel_refresh_required_at",
        "panel_refreshed_at",
        "panel_refresh_failed_at",
    ):
        value = item.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ControlRecordInitializationError("legacy panel refresh timestamp is invalid")
            parsed[field] = _parse_utc(value)
    required_at = parsed.get("panel_refresh_required_at")
    refreshed_at = parsed.get("panel_refreshed_at")
    failed_at = parsed.get("panel_refresh_failed_at")
    if required_at is None:
        if refreshed_at is not None or failed_at is not None:
            raise ControlRecordInitializationError("legacy panel refresh state is invalid")
        return False
    if failed_at is not None:
        return False
    if refreshed_at is None:
        return True
    return refreshed_at < required_at


def _validate_optional_timestamps(item: Mapping[str, DynamoValue]) -> None:
    for field in ("created_at", "updated_at"):
        value = item.get(field)
        if value is not None:
            if not isinstance(value, str):
                raise ControlRecordInitializationError("control record timestamp is invalid")
            _parse_utc(value)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ControlRecordInitializationError("control record timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ControlRecordInitializationError("control record timestamp is not UTC")
    return parsed


def _required_text(item: Mapping[str, DynamoValue], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ControlRecordInitializationError(f"legacy {field} is invalid")
    return value


def _required_non_negative_integer(item: Mapping[str, DynamoValue], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ControlRecordInitializationError(f"legacy {field} is invalid")
    return value


def _required_text_list(item: Mapping[str, DynamoValue], field: str) -> tuple[str, ...]:
    value = item.get(field)
    if not isinstance(value, list) or any(
        not isinstance(element, str) or not element for element in value
    ):
        raise ControlRecordInitializationError(f"legacy {field} is invalid")
    return cast(tuple[str, ...], tuple(value))


def _required_non_negative_integer_list(
    item: Mapping[str, DynamoValue],
    field: str,
) -> tuple[int, ...]:
    value = item.get(field)
    if not isinstance(value, list) or any(
        isinstance(element, bool) or not isinstance(element, int) or element < 0
        for element in value
    ):
        raise ControlRecordInitializationError(f"legacy {field} is invalid")
    return cast(tuple[int, ...], tuple(value))


def _required_boolean(item: Mapping[str, DynamoValue], field: str) -> bool:
    value = item.get(field)
    if not isinstance(value, bool):
        raise ControlRecordInitializationError(f"legacy {field} is invalid")
    return value


def _optional_text_pair(
    item: Mapping[str, DynamoValue],
    first: str,
    second: str,
    *,
    second_utc: bool = False,
) -> tuple[str | None, str | None]:
    first_present = first in item
    second_present = second in item
    if first_present is not second_present:
        raise ControlRecordInitializationError(f"legacy {first} and {second} are inconsistent")
    if not first_present:
        return None, None
    first_value = _required_text(item, first)
    second_value = _required_text(item, second)
    if second_utc:
        _parse_utc(second_value)
    return first_value, second_value


def _require_absent(item: Mapping[str, DynamoValue], *fields: str) -> None:
    if any(field in item for field in fields):
        raise ControlRecordInitializationError("settled legacy record retains active fields")


def _schema_version(item: Mapping[str, DynamoValue]) -> int:
    value = _required_non_negative_integer(item, "schema_version")
    if value not in {PREVIOUS_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION}:
        raise ControlRecordInitializationError("top-level schema version is unsupported")
    return value


def _require_record_schema_version(
    item: Mapping[str, DynamoValue],
    *,
    expected: int,
) -> None:
    if _required_non_negative_integer(item, "record_schema_version") != expected:
        raise ControlRecordInitializationError("record schema version is unsupported")


def _required_utc(item: Mapping[str, DynamoValue], field: str) -> datetime:
    value = _required_text(item, field)
    return _parse_utc(value)


def _enum_value[EnumT: StrEnum](
    enum_type: type[EnumT],
    item: Mapping[str, DynamoValue],
    field: str,
) -> EnumT:
    try:
        return enum_type(_required_text(item, field))
    except ValueError:
        raise ControlRecordInitializationError(f"legacy {field} is invalid") from None


__all__ = (
    "CONTROL_RECORD_MANIFEST",
    "CONTROL_RECORD_MANIFEST_HASH",
    "CONTROL_RECORD_MANIFEST_VERSION",
    "CONTROL_RECORD_PREVIOUS_MANIFEST_HASH",
    "ControlRecordInitializationError",
    "ControlRecordInitializationResult",
    "ControlRecordInitializationStatus",
    "ControlRecordManifest",
    "ControlRecordMigrationRequired",
    "ControlRecordSpec",
    "DynamoDbControlRecordInitializer",
)
