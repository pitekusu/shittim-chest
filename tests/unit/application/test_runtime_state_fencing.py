"""Runtime-state ownership, transition, and replacement fences."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    RuntimeState,
    RuntimeStatus,
    RuntimeWakeResult,
)

NOW = datetime(2026, 7, 26, 7, 0, tzinfo=UTC)


def starting_state() -> RuntimeState:
    return RuntimeState.stopped(at=NOW).request_wake(at=NOW + timedelta(seconds=1))


def runtime_state(status: RuntimeStatus) -> RuntimeState:
    """Build one valid representative for every runtime state."""

    stopped = RuntimeState.stopped(at=NOW)
    if status is RuntimeStatus.STOPPED:
        return stopped
    starting = stopped.request_wake(at=NOW + timedelta(seconds=1)).mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    if status is RuntimeStatus.STARTING:
        return starting
    ready = starting.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )
    if status is RuntimeStatus.READY:
        return ready
    busy = ready.transition(RuntimeStatus.BUSY, at=NOW + timedelta(seconds=4))
    if status is RuntimeStatus.BUSY:
        return busy
    if status is RuntimeStatus.IDLE:
        return ready.begin_idle(at=NOW + timedelta(seconds=4))
    if status is RuntimeStatus.STOPPING:
        idle = ready.begin_idle(at=NOW + timedelta(seconds=4))
        return idle.transition(RuntimeStatus.STOPPING, at=idle.stop_eligible_at or NOW)
    return busy.transition(
        RuntimeStatus.DEGRADED,
        at=NOW + timedelta(seconds=5),
        error_code="runtime_not_ready",
    )


def apply_allowed_transition(state: RuntimeState, target: RuntimeStatus) -> RuntimeState:
    """Exercise the public operation responsible for one allowed state edge."""

    at = state.updated_at + timedelta(seconds=1)
    if target is RuntimeStatus.STARTING:
        return state.request_wake(at=at)
    if target is RuntimeStatus.IDLE:
        return state.begin_idle(at=at)
    if target is RuntimeStatus.DEGRADED:
        return state.transition(target, at=at, error_code="runtime_not_ready")
    return state.transition(target, at=at)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeStatus.STOPPED, RuntimeStatus.STARTING),
        (RuntimeStatus.STARTING, RuntimeStatus.READY),
        (RuntimeStatus.STARTING, RuntimeStatus.BUSY),
        (RuntimeStatus.STARTING, RuntimeStatus.DEGRADED),
        (RuntimeStatus.READY, RuntimeStatus.BUSY),
        (RuntimeStatus.READY, RuntimeStatus.IDLE),
        (RuntimeStatus.BUSY, RuntimeStatus.IDLE),
        (RuntimeStatus.BUSY, RuntimeStatus.DEGRADED),
        (RuntimeStatus.IDLE, RuntimeStatus.STARTING),
        (RuntimeStatus.IDLE, RuntimeStatus.STOPPING),
        (RuntimeStatus.STOPPING, RuntimeStatus.STOPPED),
        (RuntimeStatus.STOPPING, RuntimeStatus.STARTING),
        (RuntimeStatus.DEGRADED, RuntimeStatus.STARTING),
        (RuntimeStatus.DEGRADED, RuntimeStatus.READY),
        (RuntimeStatus.DEGRADED, RuntimeStatus.BUSY),
        (RuntimeStatus.DEGRADED, RuntimeStatus.IDLE),
    ],
)
def test_every_allowed_runtime_transition(source: RuntimeStatus, target: RuntimeStatus) -> None:
    transitioned = apply_allowed_transition(runtime_state(source), target)

    assert transitioned.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RuntimeStatus.STOPPED, RuntimeStatus.READY),
        (RuntimeStatus.READY, RuntimeStatus.STOPPED),
        (RuntimeStatus.BUSY, RuntimeStatus.STARTING),
        (RuntimeStatus.IDLE, RuntimeStatus.BUSY),
        (RuntimeStatus.STOPPING, RuntimeStatus.BUSY),
        (RuntimeStatus.DEGRADED, RuntimeStatus.STOPPED),
    ],
)
def test_representative_invalid_runtime_transitions_fail_closed(
    source: RuntimeStatus,
    target: RuntimeStatus,
) -> None:
    state = runtime_state(source)

    with pytest.raises(ValueError, match="invalid runtime transition"):
        state.transition(target, at=state.updated_at + timedelta(seconds=1))


def test_started_and_ready_states_are_bound_to_one_runtime_instance() -> None:
    starting = starting_state()
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=3),
        runtime_instance_id="runtime-alpha",
    )

    assert started.started_at == NOW + timedelta(seconds=2)
    assert ready.runtime_instance_id == "runtime-alpha"
    with pytest.raises(ValueError, match="another instance"):
        started.transition(
            RuntimeStatus.READY,
            at=NOW + timedelta(seconds=3),
            runtime_instance_id="runtime-beta",
        )


def test_start_binding_and_degraded_transition_fail_closed() -> None:
    starting = starting_state()
    with pytest.raises(ValueError, match="runtime instance"):
        starting.transition(RuntimeStatus.READY, at=NOW + timedelta(seconds=2))
    with pytest.raises(ValueError, match="requires an error"):
        starting.transition(RuntimeStatus.DEGRADED, at=NOW + timedelta(seconds=2))
    with pytest.raises(ValueError, match="only STARTING"):
        RuntimeState.stopped(at=NOW).mark_started(
            at=NOW + timedelta(seconds=1),
            runtime_instance_id="runtime-alpha",
        )


def test_degraded_recovery_clears_stale_runtime_binding() -> None:
    started = starting_state().mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    degraded = started.transition(
        RuntimeStatus.DEGRADED,
        at=NOW + timedelta(seconds=3),
        error_code="runtime_not_ready",
    )
    restarted = degraded.request_wake(at=NOW + timedelta(seconds=4))

    assert restarted.status is RuntimeStatus.STARTING
    assert restarted.runtime_instance_id is None
    assert restarted.started_at is None
    assert restarted.last_error_code is None


def test_only_additional_starting_wake_preserves_runtime_binding() -> None:
    starting = starting_state().mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    additional = starting.request_wake(at=NOW + timedelta(seconds=3))
    ready = additional.transition(
        RuntimeStatus.READY,
        at=NOW + timedelta(seconds=4),
        runtime_instance_id="runtime-alpha",
    )
    idle = ready.begin_idle(at=NOW + timedelta(seconds=5))
    idle_wake = idle.request_wake(at=NOW + timedelta(seconds=6))
    stopping = idle.transition(
        RuntimeStatus.STOPPING,
        at=idle.stop_eligible_at or NOW,
    )
    stopping_wake = stopping.request_wake(at=NOW + timedelta(minutes=31))

    assert additional.runtime_instance_id == "runtime-alpha"
    assert additional.started_at == starting.started_at
    assert additional.wake_started_at == starting.wake_started_at
    assert idle_wake.runtime_instance_id is None
    assert idle_wake.started_at is None
    assert stopping_wake.runtime_instance_id is None
    assert stopping_wake.started_at is None


def test_stopped_lifecycle_rewake_clears_previous_runtime_binding() -> None:
    ready = runtime_state(RuntimeStatus.READY)
    idle = ready.begin_idle(at=ready.updated_at + timedelta(seconds=1))
    stopping = idle.transition(RuntimeStatus.STOPPING, at=idle.stop_eligible_at or NOW)
    stopped = stopping.transition(
        RuntimeStatus.STOPPED,
        at=stopping.updated_at + timedelta(seconds=1),
    )
    rewoken = stopped.request_wake(at=stopped.updated_at + timedelta(seconds=1))

    assert stopped.started_at is not None
    assert stopped.ready_at is not None
    assert rewoken.status is RuntimeStatus.STARTING
    assert rewoken.runtime_instance_id is None
    assert rewoken.started_at is None
    assert rewoken.ready_at is None
    assert rewoken.busy_since is None


def test_non_wake_replacement_requires_one_version_and_stable_generation() -> None:
    starting = starting_state()
    started = starting.mark_started(
        at=NOW + timedelta(seconds=2),
        runtime_instance_id="runtime-alpha",
    )
    starting.validate_replacement(started)
    starting.validate_replacement(starting)

    with pytest.raises(ValueError, match="only request_wake"):
        starting.validate_replacement(replace(started, generation=2))
    with pytest.raises(ValueError, match="version exactly once"):
        starting.validate_replacement(replace(started, version=3))


def test_reconciliation_updates_version_without_generation_or_state() -> None:
    starting = starting_state()
    reconciled = starting.record_reconciled(at=NOW + timedelta(seconds=2))

    assert reconciled.status is starting.status
    assert reconciled.generation == starting.generation
    assert reconciled.version == starting.version + 1
    assert reconciled.last_reconciled_at == NOW + timedelta(seconds=2)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("generation", 0, "runtime generation"),
        ("runtime_version", 0, "runtime version"),
        ("interaction_id", " ", "interaction ID"),
        ("schema_version", 2, "unsupported runtime wake"),
    ],
)
def test_runtime_wake_result_validation(
    field: str,
    value: str | int,
    message: str,
) -> None:
    base = {
        "interaction_id": "interaction-alpha",
        "generation": 1,
        "runtime_version": 1,
        "recorded_at": NOW,
        "schema_version": 1,
    }
    base[field] = value

    with pytest.raises(ValueError, match=message):
        RuntimeWakeResult(**base)
