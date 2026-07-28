"""Deterministic deployment admission policy tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application.deployment_guard import (
    BreakGlassReason,
    DeploymentGuardCode,
    DeploymentGuardContext,
    DeploymentGuardSnapshot,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
    assess_deployment,
)
from shittim_chest.application.scale_to_zero import (
    RuntimeActivity,
    RuntimeState,
    RuntimeStatus,
)

NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
COMMIT_SHA = "a" * 40
GUARD_ID = "019d2c1f-0000-7000-8000-a00000000001"


def _context(*, break_glass: bool = False) -> DeploymentGuardContext:
    return DeploymentGuardContext(
        commit_sha=COMMIT_SHA,
        actor="pitekusu",
        run_id="123456",
        environment="production",
        mode=DeploymentMode.BREAK_GLASS if break_glass else DeploymentMode.NORMAL,
        reason=BreakGlassReason.SERVICE_RECOVERY if break_glass else None,
    )


def _states() -> dict[RuntimeStatus, RuntimeState]:
    stopped = RuntimeState.stopped(at=NOW)
    starting = stopped.request_wake(at=NOW)
    bound = starting.mark_started(at=NOW, runtime_instance_id="task-1")
    ready = bound.transition(RuntimeStatus.READY, at=NOW, runtime_instance_id="task-1")
    busy = ready.transition(RuntimeStatus.BUSY, at=NOW, runtime_instance_id="task-1")
    idle = ready.begin_idle(at=NOW)
    stopping = idle.begin_idle_stop(at=NOW + timedelta(minutes=30))
    degraded = busy.transition(RuntimeStatus.DEGRADED, at=NOW, error_code="runtime_failed")
    return {
        RuntimeStatus.STOPPED: stopped,
        RuntimeStatus.STARTING: starting,
        RuntimeStatus.READY: ready,
        RuntimeStatus.BUSY: busy,
        RuntimeStatus.IDLE: idle,
        RuntimeStatus.STOPPING: stopping,
        RuntimeStatus.DEGRADED: degraded,
    }


def _snapshot(
    *,
    runtime: RuntimeState | None = None,
    activity: RuntimeActivity | None = None,
    lock: DeploymentLock | None = None,
) -> DeploymentGuardSnapshot:
    return DeploymentGuardSnapshot(
        runtime=runtime or RuntimeState.stopped(at=NOW),
        activity=activity or RuntimeActivity(),
        deployment_lock=lock or DeploymentLock.open(at=NOW),
    )


@pytest.mark.parametrize("status", [RuntimeStatus.STOPPED, RuntimeStatus.IDLE])
def test_normal_deployment_allows_only_quiescent_stopped_or_idle(status: RuntimeStatus) -> None:
    result = assess_deployment(
        _snapshot(runtime=_states()[status]),
        context=_context(),
        evaluated_at=NOW,
    )

    assert result.allowed
    assert result.code is DeploymentGuardCode.SAFE
    assert result.activity_clear


@pytest.mark.parametrize(
    "status",
    [
        RuntimeStatus.STARTING,
        RuntimeStatus.READY,
        RuntimeStatus.BUSY,
        RuntimeStatus.STOPPING,
    ],
)
def test_normal_deployment_rejects_active_runtime_states(status: RuntimeStatus) -> None:
    result = assess_deployment(
        _snapshot(runtime=_states()[status]),
        context=_context(),
        evaluated_at=NOW,
    )

    assert not result.allowed
    assert result.code is DeploymentGuardCode.RUNTIME_NOT_QUIESCENT


def test_normal_deployment_rejects_degraded_before_activity() -> None:
    result = assess_deployment(
        _snapshot(
            runtime=_states()[RuntimeStatus.DEGRADED],
            activity=RuntimeActivity(pending_ingress=1),
        ),
        context=_context(),
        evaluated_at=NOW,
    )

    assert not result.allowed
    assert result.code is DeploymentGuardCode.RUNTIME_DEGRADED


@pytest.mark.parametrize(
    "field",
    [
        "pending_ingress",
        "claimed_ingress",
        "retrying_ingress",
        "active_attempts",
        "application_tasks",
        "active_leases",
        "recovery_tasks",
        "pending_outbox",
        "claimed_outbox",
        "pending_status_updates",
        "pending_panel_refreshes",
        "checkpoint_tasks",
    ],
)
def test_normal_deployment_rejects_every_activity_dimension(field: str) -> None:
    result = assess_deployment(
        _snapshot(activity=RuntimeActivity(**{field: 1})),
        context=_context(),
        evaluated_at=NOW,
    )

    assert not result.allowed
    assert result.code is DeploymentGuardCode.DURABLE_ACTIVITY_PRESENT
    assert not result.activity_clear


def test_explicit_break_glass_overrides_runtime_and_activity_but_not_lock() -> None:
    context = _context(break_glass=True)
    active = _snapshot(
        runtime=_states()[RuntimeStatus.DEGRADED],
        activity=RuntimeActivity(recovery_tasks=1),
    )

    allowed = assess_deployment(active, context=context, evaluated_at=NOW)

    assert allowed.allowed
    assert allowed.code is DeploymentGuardCode.BREAK_GLASS_OVERRIDE

    held = DeploymentLock(
        state=DeploymentLockState.LOCKED,
        fencing_token=1,
        version=1,
        updated_at=NOW,
        guard_id=GUARD_ID,
        owner="pitekusu",
        acquired_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        mode=DeploymentMode.BREAK_GLASS,
        reason=BreakGlassReason.SERVICE_RECOVERY,
    )
    denied = assess_deployment(
        replace(active, deployment_lock=held),
        context=context,
        evaluated_at=NOW + timedelta(hours=1),
    )
    assert not denied.allowed
    assert denied.code is DeploymentGuardCode.DEPLOYMENT_LOCKED


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: DeploymentGuardContext("A" * 40, "pitekusu", "123", "production"),
            "commit SHA",
        ),
        (
            lambda: DeploymentGuardContext(COMMIT_SHA, "bad_actor", "123", "production"),
            "actor",
        ),
        (lambda: DeploymentGuardContext(COMMIT_SHA, "pitekusu", "0", "production"), "run ID"),
        (
            lambda: DeploymentGuardContext(COMMIT_SHA, "pitekusu", "123", "staging"),
            "environment",
        ),
        (
            lambda: DeploymentGuardContext(
                COMMIT_SHA,
                "pitekusu",
                "123",
                "production",
                mode=DeploymentMode.BREAK_GLASS,
            ),
            "supplied together",
        ),
        (
            lambda: DeploymentGuardContext(
                COMMIT_SHA,
                "pitekusu",
                "123",
                "production",
                reason=BreakGlassReason.INCIDENT_RESPONSE,
            ),
            "supplied together",
        ),
    ],
)
def test_context_rejects_malformed_or_ambiguous_metadata(
    factory: Callable[[], DeploymentGuardContext],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_lock_rejects_partial_or_noncanonical_ownership() -> None:
    with pytest.raises(ValueError, match="cannot retain ownership"):
        DeploymentLock(
            state=DeploymentLockState.OPEN,
            fencing_token=0,
            version=0,
            updated_at=NOW,
            owner="pitekusu",
        )
    with pytest.raises(ValueError, match="canonical UUIDv7"):
        DeploymentLock(
            state=DeploymentLockState.LOCKED,
            fencing_token=1,
            version=1,
            updated_at=NOW,
            guard_id="not-a-uuid",
            owner="pitekusu",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            mode=DeploymentMode.NORMAL,
        )
