"""Application persistence-model invariants."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    DebateSnapshot,
    DeliveryAbandonReason,
    GenerationCheckpoint,
    GenerationStatus,
    LeaseGrant,
    PanelRefreshState,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
)
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    ParticipantSlot,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def snapshot() -> DebateSnapshot:
    return DebateSnapshot(
        state=DebateState.accepted(DebateId.new(), AttemptId.new(), at=NOW),
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="channel",
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="starter",
        thread_id="thread",
        control_panel_message_id="panel",
    )


def test_panel_refresh_state_is_derived_from_durable_delivery_fields() -> None:
    not_required = snapshot()
    pending = replace(not_required, panel_refresh_required_at=NOW)
    delivered = replace(pending, panel_refreshed_at=NOW)
    abandoned = replace(
        pending,
        panel_refresh_delivery_attempt=1,
        panel_refresh_failed_at=NOW + timedelta(seconds=1),
        panel_refresh_error_code="discord_permission_denied",
    )

    assert not_required.panel_refresh_state is PanelRefreshState.NOT_REQUIRED
    assert pending.panel_refresh_state is PanelRefreshState.PENDING
    assert pending.panel_refresh_pending is True
    assert delivered.panel_refresh_state is PanelRefreshState.DELIVERED
    assert abandoned.panel_refresh_state is PanelRefreshState.ABANDONED
    assert abandoned.panel_refresh_pending is False


def test_panel_refresh_failure_requires_timestamp_code_and_current_requirement() -> None:
    source = snapshot()

    with pytest.raises(ValueError, match="timestamp and error code"):
        replace(source, panel_refresh_failed_at=NOW)
    with pytest.raises(ValueError, match="timestamp and error code"):
        replace(source, panel_refresh_error_code="discord_forbidden")
    with pytest.raises(ValueError, match="requires a requirement"):
        replace(
            source,
            panel_refresh_failed_at=NOW,
            panel_refresh_error_code="discord_forbidden",
        )


def test_panel_refresh_failure_cannot_precede_or_conflict_with_delivery() -> None:
    required_at = NOW + timedelta(seconds=2)
    pending = replace(snapshot(), panel_refresh_required_at=required_at)

    with pytest.raises(ValueError, match="cannot precede"):
        replace(
            pending,
            panel_refresh_failed_at=NOW + timedelta(seconds=1),
            panel_refresh_error_code="discord_forbidden",
        )
    with pytest.raises(ValueError, match="cannot also be abandoned"):
        replace(
            pending,
            panel_refreshed_at=required_at,
            panel_refresh_failed_at=required_at + timedelta(seconds=1),
            panel_refresh_error_code="discord_forbidden",
        )


def test_abandoned_panel_refresh_cannot_retain_claim_or_retry_state() -> None:
    failed_at = NOW + timedelta(seconds=1)
    abandoned = replace(
        snapshot(),
        panel_refresh_required_at=NOW,
        panel_refresh_delivery_attempt=1,
        panel_refresh_failed_at=failed_at,
        panel_refresh_error_code="discord_forbidden",
    )

    with pytest.raises(ValueError, match="cannot retain retry or claim"):
        replace(
            abandoned,
            panel_refresh_claim_owner="worker",
            panel_refresh_claim_expires_at=failed_at + timedelta(seconds=60),
        )
    with pytest.raises(ValueError, match="cannot retain retry or claim"):
        replace(
            abandoned,
            panel_refresh_next_attempt_at=failed_at + timedelta(seconds=30),
        )


def test_generation_checkpoint_allows_only_two_successor_fenced_calls() -> None:
    first_lease = LeaseGrant("worker-1", 0, 1, NOW + timedelta(minutes=1))
    second_lease = LeaseGrant("worker-2", 1, 2, NOW + timedelta(minutes=2))
    third_lease = LeaseGrant("worker-3", 2, 3, NOW + timedelta(minutes=3))
    planned = GenerationCheckpoint.planned(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_B,
        at=NOW,
    )

    first = planned.claim(lease=first_lease, at=NOW + timedelta(seconds=1))
    assert first.status is GenerationStatus.IN_FLIGHT
    assert first.logical_attempt == 1
    with pytest.raises(ValueError, match="same lease"):
        first.claim(lease=first_lease, at=NOW + timedelta(seconds=2))

    second = first.claim(lease=second_lease, at=NOW + timedelta(seconds=2))
    assert second.logical_attempt == 2
    with pytest.raises(ValueError, match="not claimable"):
        second.claim(lease=third_lease, at=NOW + timedelta(seconds=3))
    with pytest.raises(ValueError, match="lost its lease fence"):
        second.complete(lease=first_lease, at=NOW + timedelta(seconds=3))

    completed = second.complete(lease=second_lease, at=NOW + timedelta(seconds=3))
    assert completed.status is GenerationStatus.COMPLETED
    assert completed.logical_attempt == 2


def test_generation_checkpoint_exhausts_without_a_third_provider_call() -> None:
    first_lease = LeaseGrant("worker-1", 0, 1, NOW + timedelta(minutes=1))
    second_lease = LeaseGrant("worker-2", 1, 2, NOW + timedelta(minutes=2))
    successor = LeaseGrant("worker-3", 2, 3, NOW + timedelta(minutes=3))
    second = (
        GenerationCheckpoint.planned(
            phase=DebatePhase.GENERATING_DECISION,
            participant=ParticipantSlot.PARTICIPANT_A,
            at=NOW,
        )
        .claim(lease=first_lease, at=NOW + timedelta(seconds=1))
        .claim(
            lease=second_lease,
            at=NOW + timedelta(seconds=2),
        )
    )

    with pytest.raises(ValueError, match="successor lease"):
        second.exhaust_after_recovery(
            lease=second_lease,
            at=NOW + timedelta(seconds=3),
            error_code="generation_attempts_exhausted",
        )
    failed = second.exhaust_after_recovery(
        lease=successor,
        at=NOW + timedelta(seconds=3),
        error_code="generation_attempts_exhausted",
    )

    assert failed.status is GenerationStatus.FAILED
    assert failed.logical_attempt == 2
    assert failed.error_code == "generation_attempts_exhausted"
    assert failed.claim_owner == second.claim_owner
    assert failed.claim_slot == second.claim_slot
    assert failed.claim_fencing_token == second.claim_fencing_token
    assert failed.claimed_at == second.claimed_at


def test_generation_checkpoint_can_fail_recovery_without_counting_an_unmade_call() -> None:
    first_lease = LeaseGrant("worker-1", 0, 1, NOW + timedelta(minutes=1))
    successor = LeaseGrant("worker-2", 1, 1, NOW + timedelta(minutes=2))
    first = GenerationCheckpoint.planned(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_C,
        at=NOW,
    ).claim(lease=first_lease, at=NOW + timedelta(seconds=1))

    with pytest.raises(ValueError, match="successor lease"):
        first.fail_before_recovery_call(
            lease=first_lease,
            at=NOW + timedelta(seconds=2),
            error_code="discord_delivery_preflight_failed",
        )
    failed = first.fail_before_recovery_call(
        lease=successor,
        at=NOW + timedelta(seconds=2),
        error_code="discord_delivery_preflight_failed",
    )

    assert failed.status is GenerationStatus.FAILED
    assert failed.logical_attempt == 1
    assert failed.claim_owner == "worker-1"


def test_phase_delivery_plan_has_one_bounded_settlement_path() -> None:
    plan = PhaseDeliveryPlan(
        plan_id="terminal-completed",
        source_phase=DebatePhase.GENERATING_DECISION,
        target_phase=DebatePhase.COMPLETED,
        operation_ids=("terminal-completed-0000",),
        content_hashes=("a" * 64,),
        delivery_sequences=(300,),
        staged_at=NOW,
        deadline_at=NOW + timedelta(minutes=15),
    )

    delivered = plan.complete(at=NOW + timedelta(seconds=1))
    terminating = plan.terminate(reason=DeliveryAbandonReason.CANCELLED)
    reconciled = terminating.complete(at=NOW + timedelta(seconds=2))
    abandoned = terminating.abandon(
        at=NOW + timedelta(seconds=2),
        reason=DeliveryAbandonReason.CANCELLED,
    )

    assert delivered.status is PhaseDeliveryStatus.DELIVERED
    assert delivered.completed_at == NOW + timedelta(seconds=1)
    assert terminating.status is PhaseDeliveryStatus.TERMINATING
    assert reconciled.status is PhaseDeliveryStatus.DELIVERED
    assert abandoned.status is PhaseDeliveryStatus.ABANDONED
    assert abandoned.abandon_reason is DeliveryAbandonReason.CANCELLED
    with pytest.raises(ValueError, match="exactly 15 minutes"):
        replace(plan, deadline_at=NOW + timedelta(minutes=14))
    with pytest.raises(ValueError, match="only an unsettled"):
        abandoned.complete(at=NOW + timedelta(seconds=3))
