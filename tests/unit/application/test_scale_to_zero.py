from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    IDLE_TIMEOUT,
    EcsRuntimeSnapshot,
    IngressKind,
    IngressRequest,
    IngressStatus,
    RuntimeActivity,
    RuntimeState,
    RuntimeStatus,
    StatusMessageState,
)

NOW = datetime(2026, 7, 26, 1, 2, 3, tzinfo=UTC)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="100000000000000001",
        operation_id="100000000000000001",
        question="Which sweet breakfast should I choose?",
        requester_id="100000000000000002",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="100000000000000003",
        channel_id="100000000000000004",
        command_name="shittim",
        created_at=NOW,
    )


def test_new_request_has_per_request_deadlines_and_counts_toward_queue() -> None:
    value = request()

    assert value.status is IngressStatus.PENDING
    assert value.status.counts_toward_queue_limit
    assert value.startup_deadline_at == NOW + timedelta(minutes=3)
    assert value.terminal_deadline_at == NOW + timedelta(minutes=15)
    assert value.status_message_state is StatusMessageState.PENDING


def test_request_rejects_non_utc_and_incorrect_deadlines() -> None:
    value = request()

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        replace(value, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="exactly three minutes"):
        replace(value, startup_deadline_at=NOW + timedelta(minutes=4))


def test_control_request_requires_custom_id_and_never_question() -> None:
    value = request()

    with pytest.raises(ValueError, match="control operation requires"):
        replace(
            value,
            kind=IngressKind.CANCEL,
            command_name=None,
            question=None,
        )
    with pytest.raises(ValueError, match="only a new debate"):
        replace(value, kind=IngressKind.RETRY, custom_id="shittim:v1:retry")


def test_claim_and_terminal_shape_are_fail_closed() -> None:
    value = request()

    with pytest.raises(ValueError, match="claimed request requires"):
        replace(value, status=IngressStatus.CLAIMED)
    with pytest.raises(ValueError, match="terminal request requires"):
        replace(value, status=IngressStatus.FAILED)


def test_runtime_wake_increments_generation_and_preserves_start_time() -> None:
    stopped = RuntimeState.stopped(at=NOW)
    first = stopped.request_wake(at=NOW + timedelta(seconds=1))
    second = first.request_wake(at=NOW + timedelta(seconds=2))

    assert first.status is RuntimeStatus.STARTING
    assert first.desired_count == 1
    assert first.generation == 1
    assert second.generation == 2
    assert second.wake_started_at == first.wake_started_at


def test_runtime_transition_graph_rejects_invalid_edge() -> None:
    stopped = RuntimeState.stopped(at=NOW)

    with pytest.raises(ValueError, match="invalid runtime transition"):
        stopped.transition(RuntimeStatus.BUSY, at=NOW + timedelta(seconds=1))


def test_idle_timestamp_is_fixed_and_stop_requires_complete_activity() -> None:
    ready = (
        RuntimeState.stopped(at=NOW)
        .request_wake(at=NOW + timedelta(seconds=1))
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=2),
            runtime_instance_id="runtime-1",
        )
    )
    idle = ready.begin_idle(at=NOW + timedelta(minutes=1))
    idle_since = idle.idle_since
    stop_eligible_at = idle.stop_eligible_at

    assert idle.begin_idle(at=NOW + timedelta(minutes=2)) is idle
    assert idle_since is not None
    assert stop_eligible_at is not None
    assert stop_eligible_at == idle_since + IDLE_TIMEOUT
    assert not idle.may_stop(
        at=stop_eligible_at,
        expected_generation=idle.generation,
        activity=RuntimeActivity(pending_outbox=1),
    )
    assert idle.may_stop(
        at=stop_eligible_at,
        expected_generation=idle.generation,
        activity=RuntimeActivity(),
    )


def test_runtime_wake_from_idle_clears_stop_window() -> None:
    idle = (
        RuntimeState.stopped(at=NOW)
        .request_wake(at=NOW + timedelta(seconds=1))
        .transition(RuntimeStatus.READY, at=NOW + timedelta(seconds=2))
        .begin_idle(at=NOW + timedelta(minutes=1))
    )

    started = idle.request_wake(at=NOW + timedelta(minutes=2))

    assert started.status is RuntimeStatus.STARTING
    assert started.idle_since is None
    assert started.stop_eligible_at is None
    assert started.desired_count == 1


@pytest.mark.parametrize("value", [-1, 2, True])
def test_ecs_snapshot_rejects_non_singleton_counts(value: int) -> None:
    with pytest.raises(ValueError, match="singleton ECS counts"):
        EcsRuntimeSnapshot(desired_count=value, running_count=0, pending_count=0)


def test_runtime_activity_requires_every_counter_to_be_zero() -> None:
    assert RuntimeActivity().is_complete
    assert not RuntimeActivity(claimed_ingress=1).is_complete
    with pytest.raises(ValueError, match="non-negative integers"):
        RuntimeActivity(active_leases=-1)
