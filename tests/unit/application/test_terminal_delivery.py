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
    DiscordDeliveryTarget,
    OutboxOperation,
    OutboxStatus,
    TerminalDeliveryPlan,
    content_sha256,
    prepare_terminal_outbox_operations,
)
from shittim_chest.domain import (
    AFFECTION_RULES_VERSION,
    AffectionAssessment,
    AffectionAssessmentStatus,
    AffectionProfile,
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    FinalDecision,
    ParticipantAffection,
    ParticipantSlot,
    RecoveryState,
    Vote,
    assess_affection,
)

NOW = datetime(2026, 7, 27, 1, 0, tzinfo=UTC)
AI_DISCLAIMER = "この出力はAI生成であり、正確性や専門的判断を保証するものではありません。"
DISPLAY_NAMES = {
    ParticipantSlot.PARTICIPANT_A: "アロナ",
    ParticipantSlot.PARTICIPANT_B: "プラナ",
    ParticipantSlot.PARTICIPANT_C: "安倍晋三",
}


def votes_for_winner(winner: ParticipantSlot) -> tuple[Vote, ...]:
    others = tuple(participant for participant in ParticipantSlot if participant is not winner)
    return (
        Vote(winner, others[0], 3, 3, 3, "winner vote"),
        Vote(others[0], winner, 5, 5, 5, "support one"),
        Vote(others[1], winner, 4, 4, 4, "support two"),
    )


def snapshot(
    *,
    phase: DebatePhase = DebatePhase.GENERATING_DECISION,
    final_decision: FinalDecision | None = None,
    error_code: str | None = None,
    thread_id: str | None = "103",
    terminal_delivery: TerminalDeliveryPlan | None = None,
    affection_assessment: AffectionAssessment | None = None,
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
        votes=(votes_for_winner(final_decision.winner) if final_decision is not None else ()),
        error_code=error_code,
        terminal_delivery=terminal_delivery,
        affection_assessment=affection_assessment,
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
        victory_message="選んでくれてありがとう。私らしくまとめるね。",
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


def test_completed_delivery_contains_decision_actions_and_caveats_without_disclaimer() -> None:
    source = snapshot(final_decision=final_decision())

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    result_content = operations[0].content
    decision_content = "\n".join(operation.content for operation in operations[1:])

    assert result_content.startswith("**投票結果**")
    assert "- アロナ: 1票" in result_content
    assert "- プラナ: 2票" in result_content
    assert "- 安倍晋三: 0票" in result_content
    assert "**勝者**\n> プラナ (2票)" in result_content
    assert "**勝利の言葉**" in decision_content
    assert "選んでくれてありがとう。私らしくまとめるね。" in decision_content
    assert "**最終決定**" in decision_content
    assert "フルーツを添えたパンケーキ" in decision_content
    assert "- 薄力粉と卵を混ぜる" in decision_content
    assert "- 甘さは好みで調整する" in decision_content
    assert AI_DISCLAIMER not in decision_content


def test_completed_delivery_reports_effective_affection_changes() -> None:
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 590, 35, 35, 625),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 98, -43, -43, 55),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 987, 50, 13, 1000),
        ),
        assessed_at=NOW,
    )
    source = snapshot(final_decision=final_decision(), affection_assessment=assessment)

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )

    affection = operations[-1]
    assert "親愛度" not in operations[0].content
    assert affection.operation_id == "terminal-completed-affection-0000"
    assert affection.bot_slot is DiscordBotSlot.MODERATOR
    assert affection.thread_id == "103"
    assert affection.channel_id == "102"
    assert affection.delivery_target is DiscordDeliveryTarget.CHANNEL
    assert affection.delivery_target_id == "102"
    assert affection.record_schema_version == 3
    assert affection.delivery_sequence == 320
    assert "## 💗 ぬしの親愛度結果" in affection.content
    assert "### 🩵 アロナ" in affection.content
    assert "💗💗💗💗💗💗🤍🤍🤍🤍" in affection.content
    assert "**625点** / 1000　📈 **+35**" in affection.content
    assert "### 💙 プラナ" in affection.content
    assert "🤍🤍🤍🤍🤍🤍🤍🤍🤍🤍" in affection.content
    assert "**55点** / 1000　📉 **-43**" in affection.content
    assert "### 💜 安倍晋三" in affection.content
    assert "💗💗💗💗💗💗💗💗💗💗" in affection.content
    assert "**1000点** / 1000　📈 **+13**" in affection.content
    assert "メモリアルロビーが開放されました！" not in affection.content  # noqa: RUF001


def test_affection_channel_post_adds_the_memorial_link_only_for_the_persisted_unlock() -> None:
    source = snapshot(final_decision=final_decision())
    profile = AffectionProfile(
        requester_key="A" * 43,
        requester_username=source.requester_username,
        requester_display_name=source.requester_display_name,
        scores=(500, 990, 500),
        version=1,
        updated_at=NOW,
    )
    _, assessment = assess_affection(
        profile,
        scores=(0, 10, 0),
        assessed_at=NOW,
        debate_id=source.state.debate_id,
        operation_seed="memorial-notice",
    )
    source = replace(source, affection_assessment=assessment)
    memorial_url = "https://records.example.invalid/memorial"

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
        records_memorial_url=memorial_url,
    )

    affection = operations[-1]
    assert affection.operation_id == "terminal-completed-affection-0000"
    assert affection.delivery_target is DiscordDeliveryTarget.CHANNEL
    assert "## 🎉 メモリアルロビーが開放されました！" in affection.content  # noqa: RUF001
    assert "> 💞 **プラナ**との特別なロビーが利用できます。" in affection.content
    assert f"> 🔗 [メモリアルロビーを開く]({memorial_url})" in affection.content
    assert all(memorial_url not in operation.content for operation in operations[:-1])

    with pytest.raises(ValueError, match="requires the Records Memorial URL"):
        prepare_terminal_outbox_operations(
            snapshot=source,
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
            participant_display_names=DISPLAY_NAMES,
        )


def test_affection_channel_post_matches_the_requester_facing_result_contract() -> None:
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 500, 32, 32, 532),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 500, -12, -12, 488),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 500, -20, -20, 480),
        ),
        assessed_at=NOW,
    )
    source = replace(
        snapshot(final_decision=final_decision(), affection_assessment=assessment),
        requester_display_name="パワー系ウナギ",
    )

    affection = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names={
            **DISPLAY_NAMES,
            ParticipantSlot.PARTICIPANT_C: "安倍晋三AI",
        },
    )[-1]

    assert affection.content.startswith("## 💗 パワー系ウナギの親愛度結果")
    assert "### 🩵 アロナ" in affection.content
    assert "**532点** / 1000　📈 **+32**" in affection.content
    assert "### 💙 プラナ" in affection.content
    assert "**488点** / 1000　📉 **-12**" in affection.content
    assert "### 💜 安倍晋三AI" in affection.content
    assert "**480点** / 1000　📉 **-20**" in affection.content


def test_completed_delivery_explains_unavailable_affection_without_zeroing_scores() -> None:
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.UNAVAILABLE,
        rules_version=AFFECTION_RULES_VERSION,
        participants=tuple(
            ParticipantAffection(participant, 500, None, 0, 500) for participant in ParticipantSlot
        ),
        assessed_at=NOW,
    )
    source = snapshot(final_decision=final_decision(), affection_assessment=assessment)

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )

    affection = operations[-1]
    assert "親愛度" not in operations[0].content
    assert "質問の評価を完了できなかったため、" in affection.content
    assert "親愛度は変更していません。" in affection.content
    assert "500点" not in affection.content
    assert affection.delivery_target is DiscordDeliveryTarget.CHANNEL


def test_completed_delivery_explains_a_tied_ballot_before_the_winner_speaks() -> None:
    tied_votes = (
        Vote(
            ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_B,
            1,
            1,
            1,
            "support b",
        ),
        Vote(
            ParticipantSlot.PARTICIPANT_B,
            ParticipantSlot.PARTICIPANT_C,
            2,
            2,
            2,
            "support c",
        ),
        Vote(
            ParticipantSlot.PARTICIPANT_C,
            ParticipantSlot.PARTICIPANT_A,
            5,
            5,
            5,
            "support a",
        ),
    )
    source = replace(
        snapshot(final_decision=final_decision(winner=ParticipantSlot.PARTICIPANT_A)),
        votes=tied_votes,
    )

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )

    assert all(f"- {name}: 1票" in operations[0].content for name in DISPLAY_NAMES.values())
    assert "**勝者**\n> アロナ (1票)" in operations[0].content
    assert "同票のため、規定の評価基準で勝者を決定しました。" in operations[0].content
    assert operations[1].bot_slot is DiscordBotSlot.PARTICIPANT_A


def test_legacy_completed_delivery_without_victory_message_keeps_the_decision() -> None:
    source = snapshot(
        final_decision=replace(final_decision(), victory_message=None),
    )

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    decision_content = "\n".join(operation.content for operation in operations[1:])

    assert "**勝利の言葉**" not in decision_content
    assert "**最終決定**" in decision_content
    assert "フルーツを添えたパンケーキ" in decision_content


def test_failed_and_cancelled_delivery_have_concise_terminal_content() -> None:
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
    assert AI_DISCLAIMER not in failed_content
    assert AI_DISCLAIMER not in cancelled_content
    assert all(operation.bot_slot is DiscordBotSlot.MODERATOR for operation in failed)
    assert all(operation.bot_slot is DiscordBotSlot.MODERATOR for operation in cancelled)


def test_failed_and_cancelled_delivery_preserve_applied_affection_visibility() -> None:
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 500, 35, 35, 535),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 500, -43, -43, 457),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 987, 50, 13, 1_000),
        ),
        assessed_at=NOW,
    )

    failed = prepare_terminal_outbox_operations(
        snapshot=snapshot(affection_assessment=assessment),
        target_phase=DebatePhase.FAILED,
        error_code="OPENAI_TIMEOUT",
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    cancelled = prepare_terminal_outbox_operations(
        snapshot=snapshot(affection_assessment=assessment),
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )

    for operations, expected_sequence in ((failed, 910), (cancelled, 920)):
        assert "親愛度" not in operations[0].content
        affection = operations[-1]
        assert affection.delivery_sequence == expected_sequence
        assert affection.delivery_target is DiscordDeliveryTarget.CHANNEL
        assert affection.channel_id == "102"
        assert "### 🩵 アロナ" in affection.content
        assert "**535点** / 1000　📈 **+35**" in affection.content
        assert "**457点** / 1000　📉 **-43**" in affection.content
        assert "**1000点** / 1000　📈 **+13**" in affection.content


@pytest.mark.parametrize(
    ("winner", "expected_bot_slot"),
    (
        (ParticipantSlot.PARTICIPANT_A, DiscordBotSlot.PARTICIPANT_A),
        (ParticipantSlot.PARTICIPANT_B, DiscordBotSlot.PARTICIPANT_B),
        (ParticipantSlot.PARTICIPANT_C, DiscordBotSlot.PARTICIPANT_C),
    ),
)
def test_completed_delivery_announces_with_moderator_then_uses_the_persisted_winner(
    winner: ParticipantSlot,
    expected_bot_slot: DiscordBotSlot,
) -> None:
    operations = prepare_terminal_outbox_operations(
        snapshot=snapshot(final_decision=final_decision(winner=winner)),
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )

    assert operations[0].bot_slot is DiscordBotSlot.MODERATOR
    assert all(operation.bot_slot is expected_bot_slot for operation in operations[1:])
    assert tuple(operation.delivery_sequence for operation in operations) == tuple(
        range(300, 300 + len(operations))
    )


def test_terminal_operations_are_replay_stable_and_use_unique_uuid7_nonces() -> None:
    source = snapshot(final_decision=final_decision(decision="甘い朝食 " * 1_100))

    first = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    replay = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    decoded_nonces = tuple(
        UUID(bytes=base64.urlsafe_b64decode(f"{operation.nonce}==")) for operation in first
    )

    assert len(first) > 1
    assert first == replay
    assert len({operation.nonce for operation in first}) == len(first)
    assert all(nonce.version == 7 and nonce.variant == RFC_4122 for nonce in decoded_nonces)
    assert first[0].bot_slot is DiscordBotSlot.MODERATOR
    assert all(operation.bot_slot is DiscordBotSlot.PARTICIPANT_B for operation in first[1:])
    assert all(operation.status is OutboxStatus.PREPARED for operation in first)


def test_plan_preserves_chunk_operation_id_and_content_hash_correspondence() -> None:
    source = snapshot(final_decision=final_decision(decision="甘い朝食 " * 700))
    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW,
        participant_display_names=DISPLAY_NAMES,
    )
    delivery = plan_from_operations(
        target_phase=DebatePhase.COMPLETED,
        operations=operations,
    )

    assert tuple(zip(delivery.operation_ids, delivery.content_hashes, strict=True)) == tuple(
        (operation.operation_id, operation.content_hash) for operation in operations
    )
    assert operations[0].operation_id == "terminal-completed-result-0000"
    assert operations[0].chunk_sequence == 0
    assert tuple(operation.chunk_sequence for operation in operations[1:]) == tuple(
        range(len(operations) - 1)
    )
    assert all(
        operation.operation_id == f"terminal-completed-decision-{operation.chunk_sequence:04d}"
        and operation.content_hash == content_sha256(operation.content)
        for operation in operations[1:]
    )


def test_terminal_operation_preconditions_fail_closed() -> None:
    completed = snapshot(final_decision=final_decision())
    with pytest.raises(ValueError, match="participant display names"):
        prepare_terminal_outbox_operations(
            snapshot=completed,
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="each participant display name exactly once"):
        prepare_terminal_outbox_operations(
            snapshot=completed,
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
            participant_display_names={ParticipantSlot.PARTICIPANT_A: "アロナ"},
        )
    with pytest.raises(ValueError, match="winner conflicts with the durable ballot"):
        prepare_terminal_outbox_operations(
            snapshot=replace(
                completed,
                final_decision=final_decision(winner=ParticipantSlot.PARTICIPANT_A),
            ),
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW,
            participant_display_names=DISPLAY_NAMES,
        )
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
        participant_display_names=(
            DISPLAY_NAMES if target_phase is DebatePhase.COMPLETED else None
        ),
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
