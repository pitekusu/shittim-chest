"""DynamoDB Local deployment guard, ingress gate, and runtime-stop race tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from typing import Any, cast

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    CONTROL_RECORD_MANIFEST,
    CURRENT_SCHEMA_VERSION,
    DynamoDbControlRecordInitializer,
    DynamoDbDeploymentGuard,
    DynamoDbIngressRepository,
    DynamoDbRuntimeStateRepository,
    serialize_deployment_lock,
    serialize_runtime_state,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.control_records import (
    CONTROL_RECORD_MANIFEST_HASH,
    CONTROL_RECORD_PREVIOUS_MANIFEST_HASH,
    ControlRecordInitializationError,
    ControlRecordInitializationStatus,
)
from shittim_chest.adapters.dynamodb.deployment_guard import (
    DeploymentGuardUnavailable,
    DeploymentLockAcquisition,
)
from shittim_chest.adapters.dynamodb.serializer import (
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    deserialize_deployment_lock,
    deserialize_runtime_state,
)
from shittim_chest.application import IngressRequest, RuntimeState, RuntimeStatus
from shittim_chest.application.deployment_guard import (
    BreakGlassReason,
    DeploymentGuardContext,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
)
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
GUARD_ID = "019d2c1f-0000-7000-8000-a00000000004"


class BarrierClient:
    """Release the first two competing transactions at one shared barrier."""

    def __init__(self, client: DynamoDBClient) -> None:
        self._client = client
        self._barrier = Barrier(2)
        self._call_lock = Lock()
        self._call_count = 0
        self.exceptions = client.exceptions

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        with self._call_lock:
            wait_at_barrier = self._call_count < 2
            self._call_count += 1
        if wait_at_barrier:
            self._barrier.wait(timeout=5)
        return cast(dict[str, Any], self._client.transact_write_items(**kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _context(*, break_glass: bool = False) -> DeploymentGuardContext:
    return DeploymentGuardContext(
        commit_sha="a" * 40,
        actor="pitekusu",
        run_id="123456",
        environment="production",
        mode=DeploymentMode.BREAK_GLASS if break_glass else DeploymentMode.NORMAL,
        reason=BreakGlassReason.SERVICE_RECOVERY if break_glass else None,
    )


def _request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="deployment-guard-interaction",
        operation_id="deployment-guard-operation",
        application_id="application-id",
        question="deployment guard integration",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW + timedelta(minutes=31),
    )


def _idle_state() -> RuntimeState:
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW)
    bound = starting.mark_started(at=NOW, runtime_instance_id="task-1")
    ready = bound.transition(RuntimeStatus.READY, at=NOW, runtime_instance_id="task-1")
    return ready.begin_idle(at=NOW)


def _force_stopped_runtime(
    client: DynamoDBClient,
    table_name: str,
    *,
    previous: RuntimeState,
    at: datetime,
) -> RuntimeState:
    stopped = RuntimeState(
        status=RuntimeStatus.STOPPED,
        generation=previous.generation,
        desired_count=0,
        version=previous.version + 1,
        updated_at=at,
        stopped_at=at,
    )
    client.put_item(
        TableName=table_name,
        Item=marshal_item(serialize_runtime_state(stopped)),
    )
    return stopped


def _item(client: DynamoDBClient, table_name: str, *, pk: str, sk: str) -> DynamoItem:
    response = client.get_item(
        TableName=table_name,
        Key=marshal_item({"PK": pk, "SK": sk}),
        ConsistentRead=True,
    )
    return unmarshal_item(response["Item"])


def _fixed_control_items(client: DynamoDBClient, table_name: str) -> tuple[DynamoItem, ...]:
    keys = (
        *(spec.key for spec in CONTROL_RECORD_MANIFEST.activity_records),
        {"PK": "CONTROL#RUNTIME", "SK": "STATE"},
        {"PK": "CONTROL#DEPLOYMENT", "SK": "LOCK"},
    )
    return tuple(
        _item(
            client,
            table_name,
            pk=cast(str, key["PK"]),
            sk=cast(str, key["SK"]),
        )
        for key in keys
    )


def _seed_previous_controls(client: DynamoDBClient, table_name: str) -> None:
    controls = [
        *(spec.install_item for spec in CONTROL_RECORD_MANIFEST.activity_records),
        CONTROL_RECORD_MANIFEST.initial_runtime_item,
        CONTROL_RECORD_MANIFEST.initial_deployment_lock_item,
    ]
    for index, item in enumerate(controls):
        previous = {**item, "schema_version": PREVIOUS_SCHEMA_VERSION}
        if index == 0:
            previous["manifest_hash"] = CONTROL_RECORD_PREVIOUS_MANIFEST_HASH
        client.put_item(TableName=table_name, Item=marshal_item(previous))


def test_previous_controls_are_migrated_with_lock_and_kept_after_runtime_activation(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    _seed_previous_controls(dynamodb_client, empty_dynamodb_table)
    initializer = DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    assert (
        initializer.validate_compatible().status
        is ControlRecordInitializationStatus.UPGRADE_REQUIRED
    )
    guard = DynamoDbDeploymentGuard(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )

    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    locked = _fixed_control_items(dynamodb_client, empty_dynamodb_table)
    assert acquired.control_schema_before == PREVIOUS_SCHEMA_VERSION
    assert acquired.control_schema_after == CURRENT_SCHEMA_VERSION
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in locked)
    assert locked[0]["manifest_hash"] == CONTROL_RECORD_MANIFEST_HASH
    assert locked[-1]["lock_state"] == "locked"

    guard.release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=NOW + timedelta(minutes=1),
    )

    released = _fixed_control_items(dynamodb_client, empty_dynamodb_table)
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in released)
    assert released[-1]["lock_state"] == "open"
    assert initializer.validate().status is ControlRecordInitializationStatus.ALREADY_INITIALIZED


def test_previous_controls_are_atomically_restored_when_runtime_candidate_is_not_active(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    _seed_previous_controls(dynamodb_client, empty_dynamodb_table)
    guard = DynamoDbDeploymentGuard(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )

    guard.release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=NOW + timedelta(minutes=1),
        rollback_control_schema=True,
    )

    restored = _fixed_control_items(dynamodb_client, empty_dynamodb_table)
    assert all(item["schema_version"] == PREVIOUS_SCHEMA_VERSION for item in restored)
    assert restored[0]["manifest_hash"] == CONTROL_RECORD_PREVIOUS_MANIFEST_HASH
    assert restored[-1]["lock_state"] == "open"
    initializer = DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    assert (
        initializer.validate_compatible().status
        is ControlRecordInitializationStatus.UPGRADE_REQUIRED
    )
    with pytest.raises(ControlRecordInitializationError, match="upgrade"):
        initializer.validate()


def test_previous_control_rollback_rejects_changes_after_acquisition(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    _seed_previous_controls(dynamodb_client, empty_dynamodb_table)
    guard = DynamoDbDeploymentGuard(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    changed = {**_fixed_control_items(dynamodb_client, empty_dynamodb_table)[1], "count": 1}
    dynamodb_client.put_item(
        TableName=empty_dynamodb_table,
        Item=marshal_item(changed),
    )

    with pytest.raises(DeploymentGuardUnavailable, match="does not match acquisition"):
        guard.release(
            guard_id=GUARD_ID,
            expected_fencing_token=acquired.lock.fencing_token,
            actor="pitekusu",
            released_at=NOW + timedelta(minutes=1),
            rollback_control_schema=True,
        )

    retained = _fixed_control_items(dynamodb_client, empty_dynamodb_table)
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in retained)
    assert retained[1]["count"] == 1
    assert retained[-1]["lock_state"] == "locked"
    release_audit = dynamodb_client.get_item(
        TableName=empty_dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"CONTROL#DEPLOYMENT#AUDIT#{GUARD_ID}",
                "SK": "RELEASE",
            }
        ),
        ConsistentRead=True,
    )
    assert "Item" not in release_audit


@pytest.mark.asyncio
async def test_lock_blocks_ingress_and_idle_stop_until_exact_idempotent_release(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    idle = _idle_state()
    dynamodb_client.put_item(
        TableName=empty_dynamodb_table,
        Item=marshal_item(serialize_runtime_state(idle)),
    )
    guard = DynamoDbDeploymentGuard(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    acquired = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW + timedelta(minutes=30),
        expires_at=NOW + timedelta(minutes=30, seconds=1),
    )
    replayed = guard.acquire(
        context=_context(),
        guard_id=GUARD_ID,
        acquired_at=NOW + timedelta(minutes=30, milliseconds=500),
        expires_at=NOW + timedelta(minutes=45),
    )
    assert replayed == acquired
    assert replayed.assessment.evaluated_at == NOW + timedelta(minutes=30)
    assert replayed.lock.expires_at == NOW + timedelta(minutes=30, seconds=1)
    ingress = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    runtime = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )

    with pytest.raises(RepositoryUnavailable):
        await ingress.enqueue(_request())
    with pytest.raises(RepositoryConflict, match="activity fence"):
        await runtime.begin_idle_stop(
            expected=idle,
            at=NOW + timedelta(minutes=30),
        )

    expired_assessment = guard.guard(
        context=_context(),
        evaluated_at=NOW + timedelta(minutes=30, seconds=2),
    )
    assert not expired_assessment.allowed
    assert expired_assessment.code.value == "deployment_locked"

    released_at = NOW + timedelta(minutes=30, seconds=3)
    guard.release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=released_at,
    )
    # A separate process can safely replay the same idempotency key at a later wall clock.
    guard.release(
        guard_id=GUARD_ID,
        expected_fencing_token=acquired.lock.fencing_token,
        actor="pitekusu",
        released_at=released_at + timedelta(seconds=30),
    )
    release_audit = _item(
        dynamodb_client,
        empty_dynamodb_table,
        pk=f"CONTROL#DEPLOYMENT#AUDIT#{GUARD_ID}",
        sk="RELEASE",
    )
    assert release_audit["released_at"] == "2026-07-28T10:30:03.000000Z"

    stopped = await runtime.begin_idle_stop(
        expected=idle,
        at=NOW + timedelta(minutes=30, seconds=2),
    )
    assert stopped.status is RuntimeStatus.STOPPING
    assert (await ingress.enqueue(_request())).created


@pytest.mark.asyncio
async def test_acquire_and_enqueue_race_has_exactly_one_winner(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    racing = cast(DynamoDBClient, BarrierClient(dynamodb_client))
    guard = DynamoDbDeploymentGuard(client=racing, table_name=empty_dynamodb_table)
    ingress = DynamoDbIngressRepository(client=racing, table_name=empty_dynamodb_table)

    guard_result, ingress_result = await asyncio.gather(
        asyncio.to_thread(
            guard.acquire,
            context=_context(),
            guard_id="019d2c1f-0000-7000-8000-a00000000005",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
        ),
        ingress.enqueue(_request()),
        return_exceptions=True,
    )

    lock = deserialize_deployment_lock(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#DEPLOYMENT",
            sk="LOCK",
        )
    )
    counter = _item(
        dynamodb_client,
        empty_dynamodb_table,
        pk="CONTROL#INGRESS",
        sk="COUNTER",
    )
    if isinstance(guard_result, DeploymentLockAcquisition):
        assert isinstance(ingress_result, RepositoryUnavailable)
        assert lock.state.value == "locked"
        assert counter["count"] == 0
    else:
        assert isinstance(guard_result, DeploymentGuardUnavailable)
        assert not isinstance(ingress_result, BaseException)
        assert ingress_result.created
        assert lock.state.value == "open"
        assert counter["count"] == 1


@pytest.mark.asyncio
async def test_acquire_and_idle_stop_race_has_exactly_one_winner(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    idle = _idle_state()
    dynamodb_client.put_item(
        TableName=empty_dynamodb_table,
        Item=marshal_item(serialize_runtime_state(idle)),
    )
    racing = cast(DynamoDBClient, BarrierClient(dynamodb_client))
    guard = DynamoDbDeploymentGuard(client=racing, table_name=empty_dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=racing, table_name=empty_dynamodb_table)

    guard_result, stop_result = await asyncio.gather(
        asyncio.to_thread(
            guard.acquire,
            context=_context(),
            guard_id="019d2c1f-0000-7000-8000-a00000000006",
            acquired_at=NOW + timedelta(minutes=30),
            expires_at=NOW + timedelta(minutes=45),
        ),
        runtime.begin_idle_stop(
            expected=idle,
            at=NOW + timedelta(minutes=30),
        ),
        return_exceptions=True,
    )

    lock = deserialize_deployment_lock(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#DEPLOYMENT",
            sk="LOCK",
        )
    )
    state = deserialize_runtime_state(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#RUNTIME",
            sk="STATE",
        )
    )
    if isinstance(guard_result, DeploymentLockAcquisition):
        assert isinstance(stop_result, RepositoryConflict)
        assert lock.state.value == "locked"
        assert state == idle
    else:
        assert isinstance(guard_result, DeploymentGuardUnavailable)
        assert isinstance(stop_result, RuntimeState)
        assert lock.state.value == "open"
        assert state.status is RuntimeStatus.STOPPING


@pytest.mark.asyncio
async def test_break_glass_acquire_and_initial_wake_race_has_exactly_one_winner(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    request = _request()
    ingress = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    assert (await ingress.enqueue(request)).created
    racing = cast(DynamoDBClient, BarrierClient(dynamodb_client))
    guard = DynamoDbDeploymentGuard(client=racing, table_name=empty_dynamodb_table)
    runtime = DynamoDbRuntimeStateRepository(client=racing, table_name=empty_dynamodb_table)
    raced_at = request.created_at + timedelta(seconds=1)

    guard_result, wake_result = await asyncio.gather(
        asyncio.to_thread(
            guard.acquire,
            context=_context(break_glass=True),
            guard_id="019d2c1f-0000-7000-8000-a00000000007",
            acquired_at=raced_at,
            expires_at=raced_at + timedelta(minutes=15),
        ),
        runtime.request_wake(
            interaction_id=request.interaction_id,
            at=raced_at,
        ),
        return_exceptions=True,
    )

    lock = deserialize_deployment_lock(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#DEPLOYMENT",
            sk="LOCK",
        )
    )
    state = deserialize_runtime_state(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#RUNTIME",
            sk="STATE",
        )
    )
    wake_marker = dynamodb_client.get_item(
        TableName=empty_dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"INGRESS_OPERATION#{request.interaction_id}",
                "SK": "RUNTIME_WAKE",
            }
        ),
        ConsistentRead=True,
    )
    if isinstance(guard_result, DeploymentLockAcquisition):
        assert guard_result.assessment.code.value == "break_glass_override"
        assert isinstance(wake_result, RepositoryConflict)
        assert lock == guard_result.lock
        assert state.status is RuntimeStatus.STOPPED
        assert "Item" not in wake_marker
    else:
        assert isinstance(guard_result, DeploymentGuardUnavailable)
        assert isinstance(wake_result, RuntimeState)
        assert lock.state is DeploymentLockState.OPEN
        assert state == wake_result
        assert state.status is RuntimeStatus.STARTING
        assert "Item" in wake_marker


@pytest.mark.asyncio
async def test_break_glass_acquire_and_rewake_race_has_exactly_one_winner(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    request = _request()
    ingress = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    runtime = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    assert (await ingress.enqueue(request)).created
    first = await runtime.request_wake(
        interaction_id=request.interaction_id,
        at=request.created_at + timedelta(seconds=1),
    )
    stopped = _force_stopped_runtime(
        dynamodb_client,
        empty_dynamodb_table,
        previous=first,
        at=request.created_at + timedelta(seconds=2),
    )
    racing = cast(DynamoDBClient, BarrierClient(dynamodb_client))
    guard = DynamoDbDeploymentGuard(client=racing, table_name=empty_dynamodb_table)
    racing_runtime = DynamoDbRuntimeStateRepository(
        client=racing,
        table_name=empty_dynamodb_table,
    )
    raced_at = request.created_at + timedelta(seconds=3)

    guard_result, wake_result = await asyncio.gather(
        asyncio.to_thread(
            guard.acquire,
            context=_context(break_glass=True),
            guard_id="019d2c1f-0000-7000-8000-a00000000008",
            acquired_at=raced_at,
            expires_at=raced_at + timedelta(minutes=15),
        ),
        racing_runtime.ensure_wake(
            interaction_id=request.interaction_id,
            at=raced_at,
        ),
        return_exceptions=True,
    )

    lock = deserialize_deployment_lock(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#DEPLOYMENT",
            sk="LOCK",
        )
    )
    state = deserialize_runtime_state(
        _item(
            dynamodb_client,
            empty_dynamodb_table,
            pk="CONTROL#RUNTIME",
            sk="STATE",
        )
    )
    if isinstance(guard_result, DeploymentLockAcquisition):
        assert guard_result.assessment.code.value == "break_glass_override"
        assert isinstance(wake_result, RepositoryConflict)
        assert lock == guard_result.lock
        assert state == stopped
    else:
        assert isinstance(guard_result, DeploymentGuardUnavailable)
        assert isinstance(wake_result, RuntimeState)
        assert lock.state is DeploymentLockState.OPEN
        assert state == wake_result
        assert state.status is RuntimeStatus.STARTING
        assert state.generation == first.generation + 1


def test_stale_release_cannot_open_a_successor_fence(
    dynamodb_client: DynamoDBClient,
    empty_dynamodb_table: str,
) -> None:
    DynamoDbControlRecordInitializer(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    ).initialize()
    guard = DynamoDbDeploymentGuard(
        client=dynamodb_client,
        table_name=empty_dynamodb_table,
    )
    first = guard.acquire(
        context=_context(),
        guard_id="019d2c1f-0000-7000-8000-a00000000009",
        acquired_at=NOW + timedelta(minutes=40),
        expires_at=NOW + timedelta(minutes=55),
    )
    # Model the state left by privileged lock recovery before a successor owns the next fence.
    recovered_open = DeploymentLock(
        state=DeploymentLockState.OPEN,
        fencing_token=first.lock.fencing_token,
        version=first.lock.version + 1,
        updated_at=NOW + timedelta(minutes=41),
    )
    dynamodb_client.put_item(
        TableName=empty_dynamodb_table,
        Item=marshal_item(serialize_deployment_lock(recovered_open)),
    )
    successor = guard.acquire(
        context=_context(),
        guard_id="019d2c1f-0000-7000-8000-a0000000000a",
        acquired_at=NOW + timedelta(minutes=42),
        expires_at=NOW + timedelta(minutes=57),
    )
    stale_guard_id = first.lock.guard_id
    assert stale_guard_id is not None

    with pytest.raises(DeploymentGuardUnavailable, match="release did not match"):
        guard.release(
            guard_id=stale_guard_id,
            expected_fencing_token=first.lock.fencing_token,
            actor="pitekusu",
            released_at=NOW + timedelta(minutes=43),
        )

    assert (
        deserialize_deployment_lock(
            _item(
                dynamodb_client,
                empty_dynamodb_table,
                pk="CONTROL#DEPLOYMENT",
                sk="LOCK",
            )
        )
        == successor.lock
    )
    stale_release_audit = dynamodb_client.get_item(
        TableName=empty_dynamodb_table,
        Key=marshal_item(
            {
                "PK": f"CONTROL#DEPLOYMENT#AUDIT#{stale_guard_id}",
                "SK": "RELEASE",
            }
        ),
        ConsistentRead=True,
    )
    assert "Item" not in stale_release_audit
