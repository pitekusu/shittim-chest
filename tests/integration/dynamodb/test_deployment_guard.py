"""DynamoDB Local deployment guard, ingress gate, and runtime-stop race tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any, cast

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    DynamoDbControlRecordInitializer,
    DynamoDbDeploymentGuard,
    DynamoDbIngressRepository,
    DynamoDbRuntimeStateRepository,
    serialize_runtime_state,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.deployment_guard import (
    DeploymentGuardUnavailable,
    DeploymentLockAcquisition,
)
from shittim_chest.adapters.dynamodb.serializer import (
    DynamoItem,
    deserialize_deployment_lock,
    deserialize_runtime_state,
)
from shittim_chest.application import IngressRequest, RuntimeState, RuntimeStatus
from shittim_chest.application.deployment_guard import DeploymentGuardContext
from shittim_chest.application.ports import RepositoryConflict, RepositoryUnavailable

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
GUARD_ID = "019d2c1f-0000-7000-8000-a00000000004"


class BarrierClient:
    """Release two competing TransactWriteItems calls at the same test barrier."""

    def __init__(self, client: DynamoDBClient) -> None:
        self._client = client
        self._barrier = Barrier(2)
        self.exceptions = client.exceptions

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self._barrier.wait(timeout=5)
        return cast(dict[str, Any], self._client.transact_write_items(**kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _context() -> DeploymentGuardContext:
    return DeploymentGuardContext(
        commit_sha="a" * 40,
        actor="pitekusu",
        run_id="123456",
        environment="production",
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


def _item(client: DynamoDBClient, table_name: str, *, pk: str, sk: str) -> DynamoItem:
    response = client.get_item(
        TableName=table_name,
        Key=marshal_item({"PK": pk, "SK": sk}),
        ConsistentRead=True,
    )
    return unmarshal_item(response["Item"])


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
