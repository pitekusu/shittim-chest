"""Application persistence-model invariants."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

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
    FinalProposal,
    InitialOpinion,
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


def test_initial_generation_checkpoint_and_output_must_settle_together() -> None:
    source = snapshot()
    collecting_state = source.state.transition_to(
        DebatePhase.PREPARING_EVIDENCE,
        at=NOW + timedelta(seconds=1),
    ).transition_to(
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        at=NOW + timedelta(seconds=2),
    )
    lease = LeaseGrant(
        owner_id="worker",
        slot=0,
        fencing_token=1,
        expires_at=NOW + timedelta(minutes=1),
    )
    planned = GenerationCheckpoint.planned(
        phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
        participant=ParticipantSlot.PARTICIPANT_A,
        at=NOW + timedelta(seconds=2),
    )
    completed = planned.claim(lease=lease, at=NOW + timedelta(seconds=3)).complete(
        lease=lease,
        at=NOW + timedelta(seconds=4),
    )
    with pytest.raises(ValueError, match="requires its durable output"):
        replace(
            source,
            state=collecting_state,
            lease=lease,
            generation_checkpoints=(completed,),
        )
    with pytest.raises(ValueError, match="requires its completed generation checkpoint"):
        replace(
            source,
            state=collecting_state,
            lease=lease,
            initial_opinions=(
                InitialOpinion(ParticipantSlot.PARTICIPANT_A, "summary", "proposal"),
            ),
            generation_checkpoints=(planned,),
        )


def test_final_proposal_checkpoint_and_output_must_settle_together() -> None:
    source = snapshot()
    collecting_state = (
        source.state.transition_to(
            DebatePhase.PREPARING_EVIDENCE,
            at=NOW + timedelta(seconds=1),
        )
        .transition_to(
            DebatePhase.COLLECTING_INITIAL_OPINIONS,
            at=NOW + timedelta(seconds=2),
        )
        .transition_to(DebatePhase.DISCUSSING, at=NOW + timedelta(seconds=3))
        .transition_to(
            DebatePhase.COLLECTING_FINAL_PROPOSALS,
            at=NOW + timedelta(seconds=4),
        )
    )
    lease = LeaseGrant(
        owner_id="worker",
        slot=0,
        fencing_token=1,
        expires_at=NOW + timedelta(minutes=1),
    )
    planned = GenerationCheckpoint.planned(
        phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
        participant=ParticipantSlot.PARTICIPANT_A,
        at=NOW + timedelta(seconds=4),
    )
    completed = planned.claim(lease=lease, at=NOW + timedelta(seconds=5)).complete(
        lease=lease,
        at=NOW + timedelta(seconds=6),
    )
    with pytest.raises(ValueError, match="requires its durable output"):
        replace(
            source,
            state=collecting_state,
            lease=lease,
            generation_checkpoints=(completed,),
        )
    with pytest.raises(ValueError, match="requires its completed generation checkpoint"):
        replace(
            source,
            state=collecting_state,
            lease=lease,
            final_proposals=(FinalProposal(ParticipantSlot.PARTICIPANT_A, "title", "proposal"),),
            generation_checkpoints=(planned,),
        )


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


def test_snapshot_rejects_malformed_retry_and_identity_state() -> None:
    source = snapshot()

    invalid = (
        ({"question": " "}, "question must contain"),
        ({"attempt_created_at": NOW - timedelta(seconds=1)}, "cannot precede debate"),
        ({"attempt_created_at": NOW + timedelta(seconds=1)}, "state timestamp cannot precede"),
        ({"panel_refresh_claim_owner": "worker"}, "owner and expiry"),
        ({"panel_refresh_delivery_attempt": True}, "must be an integer"),
        ({"panel_refresh_delivery_attempt": -1}, "must be non-negative"),
        ({"panel_refreshed_at": NOW}, "completion requires"),
        (
            {
                "panel_refresh_required_at": NOW,
                "panel_refresh_error_code": " ",
                "panel_refresh_failed_at": NOW,
            },
            "must be non-empty",
        ),
        (
            {
                "panel_refresh_required_at": NOW,
                "panel_refresh_error_code": "x" * 101,
                "panel_refresh_failed_at": NOW,
            },
            "at most 100",
        ),
        (
            {"panel_refresh_required_at": NOW, "thread_id": None},
            "requires a complete panel binding",
        ),
        ({"error_code": " "}, "error code must be non-empty"),
    )

    for updates, message in invalid:
        error_type = TypeError if "integer" in message else ValueError
        with pytest.raises(error_type, match=message):
            replace(source, **updates)


def test_snapshot_rejects_duplicate_stale_or_wrong_phase_generation_fences() -> None:
    source = snapshot()
    current = GenerationCheckpoint.planned(
        phase=DebatePhase.ACCEPTED,
        participant=ParticipantSlot.PARTICIPANT_A,
        at=NOW,
    )
    stale = GenerationCheckpoint.planned(
        phase=DebatePhase.ACCEPTED,
        participant=ParticipantSlot.PARTICIPANT_B,
        at=NOW - timedelta(seconds=1),
    )
    wrong_phase = GenerationCheckpoint.planned(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_C,
        at=NOW,
    )

    with pytest.raises(ValueError, match="unique by phase and participant"):
        replace(source, generation_checkpoints=(current, current))
    with pytest.raises(ValueError, match="cannot precede its attempt"):
        replace(source, generation_checkpoints=(stale,))
    with pytest.raises(ValueError, match="remain at its active phase"):
        replace(source, generation_checkpoints=(wrong_phase,))


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


def test_generation_checkpoint_rejects_malformed_persisted_states() -> None:
    base = {
        "phase": DebatePhase.GENERATING_DECISION,
        "participant": ParticipantSlot.PARTICIPANT_A,
        "status": GenerationStatus.PLANNED,
        "logical_attempt": 0,
        "planned_at": NOW,
    }
    claimed = {
        "claim_owner": "worker",
        "claim_slot": 0,
        "claim_fencing_token": 1,
        "claimed_at": NOW + timedelta(seconds=1),
    }

    invalid = (
        ({"phase": DebatePhase.COMPLETED}, "phase must be active"),
        ({"record_schema_version": 2}, "unsupported generation"),
        ({"logical_attempt": True}, "between zero and two"),
        ({"claim_owner": "worker"}, "complete or absent"),
        ({**claimed, "claim_slot": 3}, "slot must be between"),
        ({**claimed, "claim_fencing_token": 0}, "token must be positive"),
        ({**claimed, "claimed_at": NOW - timedelta(seconds=1)}, "precede planning"),
        ({"status": GenerationStatus.PLANNED, "settled_at": NOW}, "cannot be settled"),
        ({"status": GenerationStatus.IN_FLIGHT, "logical_attempt": 1}, "exact claim"),
        (
            {
                **claimed,
                "status": GenerationStatus.IN_FLIGHT,
                "logical_attempt": 1,
                "settled_at": NOW + timedelta(seconds=2),
            },
            "cannot be settled",
        ),
        ({"status": GenerationStatus.COMPLETED, "logical_attempt": 1}, "settled claim"),
        (
            {
                **claimed,
                "status": GenerationStatus.COMPLETED,
                "logical_attempt": 1,
                "settled_at": NOW + timedelta(seconds=2),
                "error_code": "provider_failed",
            },
            "cannot contain an error",
        ),
        ({"status": GenerationStatus.FAILED}, "requires a settlement"),
        (
            {
                **claimed,
                "status": GenerationStatus.FAILED,
                "logical_attempt": 0,
                "settled_at": NOW + timedelta(seconds=2),
                "error_code": "provider_failed",
            },
            "uncalled failed",
        ),
        (
            {
                "status": GenerationStatus.FAILED,
                "logical_attempt": 1,
                "settled_at": NOW + timedelta(seconds=2),
                "error_code": "provider_failed",
            },
            "called failed",
        ),
        (
            {
                "status": GenerationStatus.FAILED,
                "logical_attempt": 0,
                "settled_at": NOW + timedelta(seconds=2),
            },
            "requires an error code",
        ),
    )

    for updates, message in invalid:
        with pytest.raises(ValueError, match=message):
            GenerationCheckpoint(**cast(Any, base | updates))


def test_generation_checkpoint_settlement_and_cancellation_boundaries() -> None:
    lease = LeaseGrant("worker", 0, 1, NOW + timedelta(minutes=1))
    planned = GenerationCheckpoint.planned(
        phase=DebatePhase.GENERATING_DECISION,
        participant=ParticipantSlot.PARTICIPANT_A,
        at=NOW,
    )
    in_flight = planned.claim(lease=lease, at=NOW + timedelta(seconds=1))

    assert planned.cancel(at=NOW + timedelta(seconds=1)).logical_attempt == 0
    assert in_flight.cancel(at=NOW + timedelta(seconds=2)).logical_attempt == 1
    failed = in_flight.fail(
        lease=lease,
        at=NOW + timedelta(seconds=2),
        error_code="provider_failed",
    )
    assert failed.cancel(at=NOW + timedelta(seconds=3)) is failed
    with pytest.raises(ValueError, match="only planned"):
        in_flight.fail_before_call(at=NOW + timedelta(seconds=2), error_code="provider_failed")
    with pytest.raises(ValueError, match="only a second"):
        in_flight.exhaust_after_recovery(
            lease=LeaseGrant("successor", 1, 2, NOW + timedelta(minutes=2)),
            at=NOW + timedelta(seconds=2),
            error_code="generation_attempts_exhausted",
        )
    with pytest.raises(ValueError, match="only in-flight"):
        planned.fail_before_recovery_call(
            lease=lease,
            at=NOW + timedelta(seconds=1),
            error_code="provider_failed",
        )
    with pytest.raises(ValueError, match="only an in-flight"):
        planned.complete(lease=lease, at=NOW + timedelta(seconds=1))


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


def test_phase_delivery_plan_rejects_malformed_persisted_states() -> None:
    base = {
        "plan_id": "terminal-completed",
        "source_phase": DebatePhase.GENERATING_DECISION,
        "target_phase": DebatePhase.COMPLETED,
        "operation_ids": ("terminal-completed-0000",),
        "content_hashes": ("a" * 64,),
        "delivery_sequences": (300,),
        "staged_at": NOW,
        "deadline_at": NOW + timedelta(minutes=15),
    }
    invalid = (
        ({"source_phase": DebatePhase.FAILED}, "active source"),
        ({"target_phase": DebatePhase.GENERATING_DECISION}, "active source"),
        ({"record_schema_version": 1}, "unsupported phase delivery"),
        ({"operation_ids": ()}, "non-empty and unique"),
        (
            {
                "operation_ids": ("one", "one"),
                "content_hashes": ("a" * 64, "b" * 64),
                "delivery_sequences": (300, 301),
            },
            "non-empty and unique",
        ),
        ({"content_hashes": ()}, "hashes must match"),
        ({"delivery_sequences": ()}, "sequences must match"),
        (
            {
                "operation_ids": ("one", "two"),
                "content_hashes": ("a" * 64, "b" * 64),
                "delivery_sequences": (301, 300),
            },
            "unique and increasing",
        ),
        ({"delivery_sequences": (True,)}, "non-negative integer"),
        ({"content_hashes": ("invalid",)}, "lowercase SHA-256"),
        (
            {"status": PhaseDeliveryStatus.STAGED, "settled_at": NOW},
            "cannot contain a result",
        ),
        ({"status": PhaseDeliveryStatus.TERMINATING}, "requires only its stop reason"),
        (
            {"status": PhaseDeliveryStatus.DELIVERED, "settled_at": NOW - timedelta(seconds=1)},
            "cannot settle before staging",
        ),
        ({"status": PhaseDeliveryStatus.DELIVERED}, "requires only a settlement"),
        ({"status": PhaseDeliveryStatus.ABANDONED}, "requires a timestamp and reason"),
    )

    for updates, message in invalid:
        with pytest.raises(ValueError, match=message):
            PhaseDeliveryPlan(**cast(Any, base | updates))


def test_phase_delivery_plan_idempotency_and_reason_guards() -> None:
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
    terminating = plan.terminate(reason=DeliveryAbandonReason.CANCELLED)
    delivered = plan.complete(at=NOW + timedelta(seconds=1))
    abandoned = terminating.abandon(
        at=NOW + timedelta(seconds=2),
        reason=DeliveryAbandonReason.CANCELLED,
    )

    assert terminating.terminate(reason=DeliveryAbandonReason.CANCELLED) is terminating
    assert delivered.complete(at=NOW + timedelta(seconds=2)) is delivered
    assert (
        abandoned.abandon(
            at=NOW + timedelta(seconds=3),
            reason=DeliveryAbandonReason.CANCELLED,
        )
        is abandoned
    )
    with pytest.raises(ValueError, match="another reason"):
        terminating.terminate(reason=DeliveryAbandonReason.NON_RETRYABLE)
    with pytest.raises(ValueError, match="only a staged"):
        delivered.terminate(reason=DeliveryAbandonReason.CANCELLED)
    with pytest.raises(ValueError, match="reason cannot change"):
        terminating.abandon(
            at=NOW + timedelta(seconds=2),
            reason=DeliveryAbandonReason.NON_RETRYABLE,
        )
