"""Focused tests for durable terminal Discord delivery contracts."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import RFC_4122, UUID

import pytest

from shittim_chest.application import (
    DebateSnapshot,
    DiscordBotSlot,
    OutboxOperation,
    OutboxStatus,
    TerminalDeliveryPlan,
    content_sha256,
    prepare_terminal_outbox_operations,
)
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    FinalDecision,
    ParticipantSlot,
    RecoveryState,
)

NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
AI_DISCLAIMER = "AI生成であり、正確性や専門的判断を保証するものではありません。"


def snapshot(
    *,
    phase: DebatePhase = DebatePhase.GENERATING_DECISION,
    final_decision: FinalDecision | None = None,
    error_code: str | None = None,
    thread_id: str | None = "103",
    terminal_delivery: TerminalDeliveryPlan | None = None,
) -> DebateSnapshot:
    return DebateSnapshot(
        state=DebateState(
            debate_id=DebateId.new(),
            attempt_id=AttemptId.new(),
            phase=phase,
            recovery_state=RecoveryState.NONE,
            updated_at=NOW,
            failed_from_phase=(
                DebatePhase.GENERATING_DECISION if phase is DebatePhase.FAILED else None
            ),
        ),
        question="今日の朝ごはんは何がいい?甘いものが食べたい",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="101",
        channel_id="102",
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="104",
        thread_id=thread_id,
        control_panel_message_id="105",
        final_decision=final_decision,
        error_code=error_code,
        terminal_delivery=terminal_delivery,
    )


def final_decision(
    *,
    winner: ParticipantSlot = ParticipantSlot.PARTICIPANT_B,
    decision: str = "フルーツを添えたパンケーキ",
) -> FinalDecision:
    return FinalDecision(
        winner=winner,
        decision=decision,
        actions=("薄力粉と卵を混ぜる", "いちごを添える"),
        caveats=("甘さは好みで調整する",),
    )


def plan_from_operations(
    *,
    target_phase: DebatePhase,
    operations: tuple[OutboxOperation, ...],
) -> TerminalDeliveryPlan:
    operation_ids = tuple(operation.operation_id for operation in operations)
    content_hashes = tuple(operation.content_hash for operation in operations)
    return TerminalDeliveryPlan(
        target_phase=target_phase,
        operation_ids=operation_ids,
        content_hashes=content_hashes,
        staged_at=NOW,
    )


def test_completed_delivery_contains_decision_actions_caveats_and_disclaimer() -> None:
    source = snapshot(final_decision=final_decision())

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
    )
    content = "\n".join(operation.content for operation in operations)

    assert "**最終決定**" in content
    assert "フルーツを添えたパンケーキ" in content
    assert "- 薄力粉と卵を混ぜる" in content
    assert "- 甘さは好みで調整する" in content
    assert AI_DISCLAIMER in content


def test_failed_and_cancelled_delivery_have_safe_terminal_content() -> None:
    failed = prepare_terminal_outbox_operations(
        snapshot=snapshot(),
        target_phase=DebatePhase.FAILED,
        error_code="OPENAI_TIMEOUT",
        created_at=NOW,
    )
    cancelled = prepare_terminal_outbox_operations(
        snapshot=snapshot(),
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW,
    )
    failed_content = "\n".join(operation.content for operation in failed)
    cancelled_content = "\n".join(operation.content for operation in cancelled)

    assert "**討論を完了できませんでした**" in failed_content
    assert "`OPENAI_TIMEOUT`" in failed_content
    assert "再試行は操作パネルから行えます。" in failed_content
    assert "**討論を中止しました**" in cancelled_content
    assert AI_DISCLAIMER in failed_content
    assert AI_DISCLAIMER in cancelled_content
    assert all(operation.bot_slot is DiscordBotSlot.MODERATOR for operation in failed)
    assert all(operation.bot_slot is DiscordBotSlot.MODERATOR for operation in cancelled)


@pytest.mark.parametrize(
    ("winner", "expected_bot_slot"),
    (
        (ParticipantSlot.PARTICIPANT_A, DiscordBotSlot.PARTICIPANT_A),
        (ParticipantSlot.PARTICIPANT_B, DiscordBotSlot.PARTICIPANT_B),
        (ParticipantSlot.PARTICIPANT_C, DiscordBotSlot.PARTICIPANT_C),
    ),
)
def test_completed_delivery_is_owned_by_the_persisted_winner(
    winner: ParticipantSlot,
    expected_bot_slot: DiscordBotSlot,
) -> None:
    operations = prepare_terminal_outbox_operations(
        snapshot=snapshot(final_decision=final_decision(winner=winner)),
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
    )

    assert all(operation.bot_slot is expected_bot_slot for operation in operations)


def test_terminal_operations_are_replay_stable_and_use_unique_uuid7_nonces() -> None:
    source = snapshot(final_decision=final_decision(decision="甘い朝食 " * 1_100))

    first = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
    )
    replay = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
    )
    decoded_nonces = tuple(
        UUID(bytes=base64.urlsafe_b64decode(f"{operation.nonce}==")) for operation in first
    )

    assert len(first) > 1
    assert first == replay
    assert len({operation.nonce for operation in first}) == len(first)
    assert all(nonce.version == 7 and nonce.variant == RFC_4122 for nonce in decoded_nonces)
    assert all(operation.bot_slot is DiscordBotSlot.PARTICIPANT_B for operation in first)
    assert all(operation.status is OutboxStatus.PREPARED for operation in first)


def test_plan_preserves_chunk_operation_id_and_content_hash_correspondence() -> None:
    source = snapshot(final_decision=final_decision(decision="甘い朝食 " * 700))
    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
    )
    delivery = plan_from_operations(
        target_phase=DebatePhase.COMPLETED,
        operations=operations,
    )

    assert tuple(zip(delivery.operation_ids, delivery.content_hashes, strict=True)) == tuple(
        (operation.operation_id, operation.content_hash) for operation in operations
    )
    assert tuple(operation.chunk_sequence for operation in operations) == tuple(
        range(len(operations))
    )
    assert all(
        operation.operation_id == f"terminal-completed-{operation.chunk_sequence:04d}"
        and operation.content_hash == content_sha256(operation.content)
        for operation in operations
    )


def test_terminal_operation_preconditions_fail_closed() -> None:
    with pytest.raises(ValueError, match="bound Discord thread"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(final_decision=final_decision(), thread_id=None),
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="decision without an error"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(),
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="decision without an error"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(final_decision=final_decision()),
            target_phase=DebatePhase.COMPLETED,
            error_code="UNEXPECTED",
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="requires an error code"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(),
            target_phase=DebatePhase.FAILED,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="cannot contain an error code"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(),
            target_phase=DebatePhase.CANCELLED,
            error_code="UNEXPECTED",
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="target must be completed"):
        prepare_terminal_outbox_operations(
            snapshot=snapshot(),
            target_phase=DebatePhase.DISCUSSING,
            created_at=NOW,
        )


def test_terminal_delivery_plan_validates_shape_and_completion_time() -> None:
    delivery = TerminalDeliveryPlan(
        target_phase=DebatePhase.CANCELLED,
        operation_ids=("terminal-cancelled-0000",),
        content_hashes=("a" * 64,),
        staged_at=NOW,
    )
    completed_at = NOW + timedelta(seconds=1)
    completed = delivery.complete(at=completed_at)

    assert delivery.completed_at is None
    assert completed.completed_at == completed_at
    assert completed.complete(at=NOW + timedelta(seconds=2)) is completed
    with pytest.raises(ValueError, match="before it is staged"):
        delivery.complete(at=NOW - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        delivery.complete(at=datetime(2026, 7, 27, 1, 0))

    with pytest.raises(ValueError, match="terminal phase"):
        replace(delivery, target_phase=DebatePhase.DISCUSSING)
    with pytest.raises(ValueError, match="non-empty and unique"):
        replace(delivery, operation_ids=("duplicate", "duplicate"), content_hashes=("a" * 64,) * 2)
    with pytest.raises(ValueError, match="hashes must match"):
        replace(delivery, content_hashes=())
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(delivery, content_hashes=("A" * 64,))
    with pytest.raises(ValueError, match="before it is staged"):
        replace(delivery, completed_at=NOW - timedelta(microseconds=1))


@pytest.mark.parametrize(
    ("target_phase", "decision", "error_code"),
    (
        (DebatePhase.COMPLETED, final_decision(), None),
        (DebatePhase.FAILED, None, "OPENAI_TIMEOUT"),
        (DebatePhase.CANCELLED, None, None),
    ),
)
def test_snapshot_requires_delivery_to_complete_only_after_terminal_transition(
    target_phase: DebatePhase,
    decision: FinalDecision | None,
    error_code: str | None,
) -> None:
    source = snapshot(final_decision=decision, error_code=error_code)
    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=target_phase,
        error_code=error_code,
        created_at=NOW,
    )
    staged_plan = plan_from_operations(target_phase=target_phase, operations=operations)
    active = replace(source, terminal_delivery=staged_plan)
    terminal_state = replace(
        active.state,
        phase=target_phase,
        updated_at=NOW + timedelta(seconds=2),
        failed_from_phase=(
            DebatePhase.GENERATING_DECISION if target_phase is DebatePhase.FAILED else None
        ),
    )

    assert active.terminal_delivery_complete is False
    with pytest.raises(ValueError, match="completed delivery plan"):
        replace(active, state=terminal_state)
    with pytest.raises(ValueError, match="active attempt"):
        replace(active, terminal_delivery=staged_plan.complete(at=NOW + timedelta(seconds=1)))

    terminal = replace(
        active,
        state=terminal_state,
        terminal_delivery=staged_plan.complete(at=NOW + timedelta(seconds=1)),
    )
    assert terminal.terminal_delivery_complete is True


def test_snapshot_rejects_terminal_payload_or_plan_mismatch() -> None:
    cancelled_plan = TerminalDeliveryPlan(
        target_phase=DebatePhase.CANCELLED,
        operation_ids=("terminal-cancelled-0000",),
        content_hashes=("a" * 64,),
        staged_at=NOW,
    )
    source = snapshot()

    with pytest.raises(ValueError, match="requires a complete Discord binding"):
        replace(source, thread_id=None, terminal_delivery=cancelled_plan)
    with pytest.raises(ValueError, match="requires a decision"):
        replace(
            source, terminal_delivery=replace(cancelled_plan, target_phase=DebatePhase.COMPLETED)
        )
    with pytest.raises(ValueError, match="requires an error code"):
        replace(source, terminal_delivery=replace(cancelled_plan, target_phase=DebatePhase.FAILED))
    with pytest.raises(ValueError, match="cannot retain an error code"):
        replace(source, error_code="UNEXPECTED", terminal_delivery=cancelled_plan)

    completed_plan = cancelled_plan.complete(at=NOW + timedelta(seconds=1))
    terminal_state = replace(
        source.state,
        phase=DebatePhase.FAILED,
        updated_at=NOW + timedelta(seconds=2),
        failed_from_phase=DebatePhase.GENERATING_DECISION,
    )
    with pytest.raises(ValueError, match="completed delivery plan"):
        replace(source, state=terminal_state, terminal_delivery=completed_plan)
