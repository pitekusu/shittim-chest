from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    EcsRuntimeSnapshot,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    RuntimeActivity,
    RuntimeState,
    RuntimeStatus,
)
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 3, 0, tzinfo=UTC)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-id",
        operation_id="operation-id",
        question="Choose a sweet breakfast",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW,
    )


@pytest.mark.parametrize(
    "field",
    [
        "interaction_id",
        "operation_id",
        "requester_id",
        "requester_username",
        "requester_display_name",
        "guild_id",
        "channel_id",
        "status_channel_id",
    ],
)
def test_request_rejects_empty_required_text(field: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        replace(request(), **{field: " "})


@pytest.mark.parametrize(
    "field",
    [
        "status_message_updated_at",
        "next_attempt_at",
        "claim_expires_at",
        "completed_at",
    ],
)
def test_request_rejects_naive_optional_timestamps(field: str) -> None:
    updates: dict[str, object] = {field: NOW.replace(tzinfo=None)}
    if field == "claim_expires_at":
        updates["claim_owner"] = "worker"
        updates["status"] = IngressStatus.CLAIMED
    if field == "completed_at":
        updates["status"] = IngressStatus.FAILED
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        replace(request(), **updates)


def test_request_rejects_invalid_shape_variants() -> None:
    source = request()
    with pytest.raises(ValueError, match="update cannot precede"):
        replace(source, updated_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="fifteen minutes"):
        replace(source, terminal_deadline_at=NOW + timedelta(minutes=16))
    with pytest.raises(ValueError, match="1-1000"):
        replace(source, question="x" * 1001)
    with pytest.raises(ValueError, match="must not be blank"):
        replace(source, question=" ")
    with pytest.raises(ValueError, match="command name"):
        replace(source, command_name=None)
    with pytest.raises(ValueError, match="only a claimed"):
        replace(source, claim_owner="worker", claim_expires_at=NOW + timedelta(minutes=1))
    with pytest.raises(ValueError, match="set together"):
        replace(source, claim_owner="worker")
    with pytest.raises(ValueError, match="non-negative integer"):
        replace(source, delivery_attempt=True)
    with pytest.raises(ValueError, match="set together"):
        replace(source, accepted_debate_id=DebateId.new())
    with pytest.raises(ValueError, match="requires debate"):
        replace(source, status=IngressStatus.ACCEPTED)
    with pytest.raises(ValueError, match="non-terminal"):
        replace(source, completed_at=NOW)
    with pytest.raises(ValueError, match="non-negative Unix"):
        replace(source, ttl=-1)
    with pytest.raises(ValueError, match="unsupported ingress"):
        replace(source, schema_version=2)


def test_request_accepts_claimed_accepted_and_terminal_shapes() -> None:
    source = request()
    claimed = replace(
        source,
        status=IngressStatus.CLAIMED,
        claim_owner="worker",
        claim_expires_at=NOW + timedelta(minutes=2),
        delivery_attempt=1,
    )
    accepted = replace(
        source,
        status=IngressStatus.ACCEPTED,
        accepted_debate_id=DebateId.new(),
        accepted_attempt_id=AttemptId.new(),
    )
    failed = replace(
        source,
        status=IngressStatus.FAILED,
        completed_at=NOW + timedelta(minutes=15),
        error_code="STARTUP_TERMINAL_TIMEOUT",
    )

    assert claimed.status.counts_toward_queue_limit
    assert not accepted.status.counts_toward_queue_limit
    assert failed.status.is_terminal


def operation() -> IngressOperationResult:
    return IngressOperationResult(
        operation_id="operation-id",
        interaction_id="interaction-id",
        request_sort_key="REQUEST#time#interaction-id",
        status=IngressStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize("field", ["operation_id", "interaction_id", "request_sort_key"])
def test_operation_rejects_empty_identifiers(field: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        replace(operation(), **{field: ""})


def test_operation_rejects_invalid_timestamps_ids_and_schema() -> None:
    source = operation()
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        replace(source, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="cannot precede"):
        replace(source, updated_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="set together"):
        replace(source, accepted_attempt_id=AttemptId.new())
    with pytest.raises(ValueError, match="unsupported ingress operation"):
        replace(source, schema_version=2)


def test_runtime_state_validation_is_fail_closed() -> None:
    stopped = RuntimeState.stopped(at=NOW)
    with pytest.raises(ValueError, match="generation"):
        replace(stopped, generation=-1)
    with pytest.raises(ValueError, match="desired count"):
        replace(stopped, desired_count=2)
    with pytest.raises(ValueError, match="version"):
        replace(stopped, version=True)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        replace(stopped, updated_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="set together"):
        replace(stopped, idle_since=NOW)
    with pytest.raises(ValueError, match="exactly thirty"):
        replace(
            stopped,
            status=RuntimeStatus.IDLE,
            idle_since=NOW,
            stop_eligible_at=NOW + timedelta(minutes=31),
        )
    with pytest.raises(ValueError, match="IDLE state requires"):
        replace(stopped, status=RuntimeStatus.IDLE)
    with pytest.raises(ValueError, match="only IDLE"):
        replace(
            stopped,
            idle_since=NOW,
            stop_eligible_at=NOW + timedelta(minutes=30),
        )
    with pytest.raises(ValueError, match="STOPPING state requires"):
        replace(stopped, status=RuntimeStatus.STOPPING)
    with pytest.raises(ValueError, match="unsupported runtime"):
        replace(stopped, schema_version=2)


def test_runtime_transitions_set_typed_state_fields() -> None:
    starting = RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))
    ready = starting.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-1",
    )
    busy = ready.transition(RuntimeStatus.BUSY, at=NOW + timedelta(seconds=3))
    degraded = busy.transition(
        RuntimeStatus.DEGRADED,
        at=NOW + timedelta(seconds=4),
        error_code="DISCORD_NOT_READY",
    )
    restarted = degraded.transition(RuntimeStatus.STARTING, at=NOW + timedelta(seconds=5))
    ready_again = restarted.transition(RuntimeStatus.READY, at=NOW + timedelta(seconds=6))
    idle = ready_again.begin_idle(at=NOW + timedelta(seconds=7))
    stopping = idle.transition(RuntimeStatus.STOPPING, at=NOW + timedelta(minutes=31))
    stopped = stopping.transition(RuntimeStatus.STOPPED, at=NOW + timedelta(minutes=32))

    assert ready.ready_at == NOW + timedelta(seconds=2)
    assert busy.busy_since == NOW + timedelta(seconds=3)
    assert degraded.last_error_code == "DISCORD_NOT_READY"
    assert restarted.wake_started_at == NOW + timedelta(seconds=5)
    assert stopping.desired_count == 0
    assert stopped.runtime_instance_id is None
    assert stopped.stopped_at == NOW + timedelta(minutes=32)


def test_runtime_rejects_backdated_or_empty_instance_transition() -> None:
    stopped = RuntimeState.stopped(at=NOW)
    with pytest.raises(ValueError, match="cannot precede"):
        stopped.request_wake(at=NOW - timedelta(seconds=1))
    starting = stopped.request_wake(at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot precede"):
        starting.transition(RuntimeStatus.READY, at=NOW)
    with pytest.raises(ValueError, match="must not be empty"):
        starting.transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=2),
            runtime_instance_id=" ",
        )


def test_runtime_stop_fence_checks_every_dimension() -> None:
    ready = (
        RuntimeState.stopped(at=NOW)
        .request_wake(at=NOW + timedelta(seconds=1))
        .transition(RuntimeStatus.READY, at=NOW + timedelta(seconds=2))
    )
    idle = ready.begin_idle(at=NOW + timedelta(minutes=1))
    eligible = NOW + timedelta(minutes=31)

    assert not ready.may_stop(
        at=eligible,
        expected_generation=ready.generation,
        activity=RuntimeActivity(),
    )
    assert not idle.may_stop(
        at=eligible - timedelta(microseconds=1),
        expected_generation=idle.generation,
        activity=RuntimeActivity(),
    )
    assert not idle.may_stop(
        at=eligible,
        expected_generation=idle.generation + 1,
        activity=RuntimeActivity(),
    )


@pytest.mark.parametrize("field", RuntimeActivity.__dataclass_fields__)
def test_runtime_activity_rejects_invalid_count_for_every_field(field: str) -> None:
    with pytest.raises(ValueError, match="non-negative integers"):
        RuntimeActivity(**{field: True})


@pytest.mark.parametrize("field", ["desired_count", "running_count", "pending_count"])
def test_ecs_snapshot_checks_each_count(field: str) -> None:
    values = {"desired_count": 0, "running_count": 0, "pending_count": 0}
    values[field] = -1
    with pytest.raises(ValueError, match="singleton ECS counts"):
        EcsRuntimeSnapshot(**values)
