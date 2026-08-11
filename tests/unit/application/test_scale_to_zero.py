from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    IDLE_TIMEOUT,
    EcsRuntimeSnapshot,
    IngressClaimFence,
    IngressKind,
    IngressRequest,
    IngressStatus,
    IngressWakeCandidate,
    OutboxActivity,
    RuntimeActivity,
    RuntimeState,
    RuntimeStatus,
    StatusMessageState,
)

NOW = datetime(2026, 7, 26, 1, 2, 3, tzinfo=UTC)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-id",
        operation_id="operation-id",
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


def test_new_request_has_per_request_deadlines_and_counts_toward_queue() -> None:
    value = request()

    assert value.status is IngressStatus.PENDING
    assert value.status.counts_toward_queue_limit
    assert value.startup_deadline_at == NOW + timedelta(minutes=3)
    assert value.terminal_deadline_at == NOW + timedelta(minutes=15)
    assert value.status_message_state is StatusMessageState.STARTING


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


def test_processing_start_marker_is_predeadline_and_survives_retry() -> None:
    value = request()
    started_at = value.created_at + timedelta(seconds=2)
    with pytest.raises(ValueError, match="pending ingress"):
        replace(value, processing_started_at=started_at)

    claimed = replace(
        value,
        status=IngressStatus.CLAIMED,
        claim_owner="runtime-1",
        claim_expires_at=value.created_at + timedelta(minutes=1),
        processing_started_at=started_at,
    )
    retrying = replace(
        claimed,
        status=IngressStatus.RETRYING,
        claim_owner=None,
        claim_expires_at=None,
        next_attempt_at=value.created_at + timedelta(minutes=2),
    )

    assert retrying.processing_started_at == started_at
    assert IngressWakeCandidate.from_request(retrying).processing_started_at == started_at
    with pytest.raises(ValueError, match="before the terminal deadline"):
        replace(claimed, processing_started_at=value.terminal_deadline_at)


def test_claim_fence_write_time_is_bounded_by_creation_and_live_claim() -> None:
    value = request()
    claimed = replace(
        value,
        status=IngressStatus.CLAIMED,
        updated_at=NOW,
        claim_owner="runtime-1",
        claim_expires_at=NOW + timedelta(minutes=1),
        delivery_attempt=1,
    )

    with pytest.raises(ValueError, match="cannot precede creation"):
        IngressClaimFence.from_claimed_request(
            claimed,
            claim_owner="runtime-1",
            write_at=NOW - timedelta(microseconds=1),
        )
    exact = IngressClaimFence.from_claimed_request(
        claimed,
        claim_owner="runtime-1",
        write_at=NOW,
    )
    assert exact.write_at == NOW

    started = replace(
        claimed,
        processing_started_at=NOW + timedelta(seconds=1),
        claim_expires_at=value.terminal_deadline_at + timedelta(minutes=1),
    )
    replay = IngressClaimFence.from_claimed_request(
        started,
        claim_owner="runtime-1",
        write_at=value.terminal_deadline_at + timedelta(seconds=1),
    )
    assert replay.write_at > replay.terminal_deadline_at


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
        .transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=2),
            runtime_instance_id="runtime-1",
        )
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


def test_ecs_snapshot_rejects_running_and_pending_tasks_together() -> None:
    with pytest.raises(ValueError, match="more than one active task"):
        EcsRuntimeSnapshot(desired_count=1, running_count=1, pending_count=1)


def test_runtime_activity_requires_every_counter_to_be_zero() -> None:
    assert RuntimeActivity().is_complete
    assert not RuntimeActivity(pending_status_updates=1).requires_runtime
    assert not RuntimeActivity(pending_status_updates=1).is_complete
    assert RuntimeActivity(active_attempts=1).requires_runtime
    assert not RuntimeActivity(claimed_ingress=1).is_complete
    assert not RuntimeActivity(pending_panel_refreshes=1).is_complete
    with pytest.raises(ValueError, match="non-negative integers"):
        RuntimeActivity(active_leases=-1)


def test_outbox_activity_requires_non_negative_complete_counts() -> None:
    assert OutboxActivity().is_complete
    assert not OutboxActivity(pending=1).is_complete
    assert not OutboxActivity(claimed=1).is_complete
    with pytest.raises(ValueError, match="non-negative"):
        OutboxActivity(pending=-1)


def test_durable_work_resume_and_stop_operations_preserve_typed_fences() -> None:
    stopped = RuntimeState.stopped(at=NOW)
    first = stopped.request_wake(at=NOW + timedelta(seconds=1))
    ready = first.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-1",
    )
    idle = ready.begin_idle(at=NOW + timedelta(minutes=1))
    resumed = idle.resume_for_work(at=NOW + timedelta(minutes=2))

    assert resumed.status is RuntimeStatus.STARTING
    assert resumed.generation == idle.generation + 1
    assert resumed.last_request_at == idle.last_request_at
    assert resumed.runtime_instance_id is None
    assert resumed.idle_since is None
    assert idle.stop_eligible_at is not None
    with pytest.raises(ValueError, match="not yet eligible"):
        idle.begin_idle_stop(at=idle.stop_eligible_at - timedelta(microseconds=1))
    stopping = idle.begin_idle_stop(at=idle.stop_eligible_at)
    assert stopping.status is RuntimeStatus.STOPPING
    assert stopping.desired_count == 0
    assert stopping.idle_since == idle.idle_since
    assert stopping.stop_eligible_at == idle.stop_eligible_at

    cancelled_start = resumed.begin_unneeded_start_stop(
        at=resumed.updated_at + timedelta(seconds=1)
    )
    assert cancelled_start.status is RuntimeStatus.STOPPING
    assert cancelled_start.idle_since is None
    assert cancelled_start.stop_eligible_at is None
