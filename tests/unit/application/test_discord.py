"""Tests for SDK-independent Discord contracts and deterministic formatting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest

from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    DebateSnapshot,
    DeliveryAbandonReason,
    DiscordBotSlot,
    DiscordErrorCode,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
    OutboxOperation,
    OutboxStatus,
    PanelAction,
    PanelCustomId,
    PanelOperation,
    PanelOperationKind,
    content_sha256,
    nonce_from_uuid7,
    prepare_final_proposal_outbox_operations,
    prepare_initial_opinion_outbox_operations,
    prepare_outbox_operations,
    prepare_terminal_outbox_operations,
    sanitize_discord_model_text,
    split_discord_message,
)
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
)

NOW = datetime(2026, 7, 17, tzinfo=UTC)
GUILD_ID = "101"
CHANNEL_ID = "102"
THREAD_ID = "103"
MESSAGE_ID = "104"


def identities() -> tuple[DiscordIdentityConfig, ...]:
    return tuple(
        DiscordIdentityConfig(slot, str(201 + index))
        for index, slot in enumerate(DISCORD_BOT_SLOTS)
    )


def outbox() -> OutboxOperation:
    return OutboxOperation(
        operation_id="post-0001",
        debate_id=DebateId.new(),
        attempt_id=AttemptId.new(),
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id=THREAD_ID,
        content="message",
        content_hash=content_sha256("message"),
        nonce=nonce_from_uuid7(uuid7()),
        chunk_sequence=0,
        status=OutboxStatus.PREPARED,
        created_at=NOW,
    )


def terminal_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    state = DebateState.accepted(debate_id, attempt_id, at=NOW)
    for index, phase in enumerate(
        (
            DebatePhase.PREPARING_EVIDENCE,
            DebatePhase.COLLECTING_INITIAL_OPINIONS,
            DebatePhase.DISCUSSING,
            DebatePhase.COLLECTING_FINAL_PROPOSALS,
            DebatePhase.SELECTING_WINNER,
            DebatePhase.GENERATING_DECISION,
        ),
        1,
    ):
        state = state.transition_to(phase, at=NOW + timedelta(seconds=index))
    return DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="201",
        thread_id=THREAD_ID,
        control_panel_message_id="202",
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "first *line*\r\nsecond",
            ("@everyone action",),
            ("careful",),
        ),
    )


def initial_opinion_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    state = DebateState.accepted(debate_id, attempt_id, at=NOW)
    state = state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=NOW + timedelta(seconds=1))
    state = state.transition_to(
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        at=NOW + timedelta(seconds=2),
    )
    return DebateSnapshot(
        state=state,
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="201",
        thread_id=THREAD_ID,
        control_panel_message_id="202",
        initial_opinions=tuple(
            InitialOpinion(
                participant,
                f"summary *{participant.value}*",
                f"proposal @everyone {participant.value}",
            )
            for participant in ParticipantSlot
        ),
    )


def final_proposal_snapshot() -> DebateSnapshot:
    snapshot = initial_opinion_snapshot()
    state = snapshot.state.transition_to(
        DebatePhase.DISCUSSING,
        at=NOW + timedelta(seconds=3),
    ).transition_to(
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        at=NOW + timedelta(seconds=4),
    )
    return replace(
        snapshot,
        state=state,
        final_proposals=tuple(
            FinalProposal(
                participant,
                f"title *{participant.value}*",
                f"proposal @everyone {participant.value}",
            )
            for participant in ParticipantSlot
        ),
    )


def test_runtime_config_requires_one_distinct_identity_per_slot_and_nonempty_allowlist() -> None:
    config = DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=identities(),
        schema_version="runtime-v1",
    )

    assert config.allows(guild_id=GUILD_ID, channel_id=CHANNEL_ID)
    assert not config.allows(guild_id="999", channel_id=CHANNEL_ID)
    assert not config.allows(guild_id=GUILD_ID, channel_id="999")
    assert config.application_id_for(DiscordBotSlot.PARTICIPANT_C) == "204"

    with pytest.raises(ValueError, match="must not be empty"):
        replace(config, allowed_channel_ids=frozenset())
    with pytest.raises(ValueError, match="each Discord Bot slot"):
        replace(config, identities=identities()[:-1])
    with pytest.raises(ValueError, match="distinct"):
        replace(
            config,
            identities=tuple(DiscordIdentityConfig(slot, "201") for slot in DISCORD_BOT_SLOTS),
        )
    with pytest.raises(ValueError, match="snowflake"):
        replace(config, guild_id="guild")
    with pytest.raises(ValueError, match="schema version"):
        replace(config, schema_version=" ")


def test_nonce_digest_and_panel_custom_id_have_stable_external_shapes() -> None:
    nonce = nonce_from_uuid7(uuid7())
    assert len(nonce) == 22
    assert "=" not in nonce
    assert content_sha256("é") == "4a99557e4033c3539de2eb65472017cad5f9557f7a0625a09f1c3f6e2ba69c4c"

    custom_id = PanelCustomId(DebateId.new(), "operation_123", PanelAction.CANCEL)
    encoded = custom_id.encode()
    assert len(encoded) <= 100
    assert PanelCustomId.parse(encoded) == custom_id

    with pytest.raises(ValueError, match="UUIDv7"):
        nonce_from_uuid7(uuid4())
    with pytest.raises(ValueError, match="must not be empty"):
        content_sha256(" ")
    with pytest.raises(ValueError, match="1-36"):
        replace(custom_id, operation_id="x" * 37)
    for malformed in ("foreign:v1:value", "shittim:v2:value", encoded + ":extra"):
        with pytest.raises(ValueError, match="panel custom ID"):
            PanelCustomId.parse(malformed)


@pytest.mark.parametrize("action", tuple(PanelAction))
def test_panel_custom_id_binds_each_action_to_one_attempt(action: PanelAction) -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()

    custom_id = PanelCustomId.for_attempt(
        debate_id=debate_id,
        attempt_id=attempt_id,
        action=action,
    )

    assert len(custom_id.operation_id) == 33
    assert custom_id.expected_attempt_id() == attempt_id
    assert PanelCustomId.parse(custom_id.encode()) == custom_id


def test_panel_operation_attempt_rejects_an_action_suffix_mismatch() -> None:
    custom_id = PanelCustomId.for_attempt(
        debate_id=DebateId.new(),
        attempt_id=AttemptId.new(),
        action=PanelAction.CANCEL,
    )

    with pytest.raises(ValueError, match="bound to its action"):
        replace(custom_id, action=PanelAction.RETRY).expected_attempt_id()


def test_message_split_is_deterministic_bounded_and_prefers_paragraphs() -> None:
    assert split_discord_message(" short ") == ("short",)
    content = f"{'a' * 1_200}\n\n{'b' * 1_200}\nline\n{'c' * 2_100}"

    chunks = split_discord_message(content)

    assert chunks == split_discord_message(content)
    assert len(chunks) >= 3
    assert all(len(chunk) <= 2_000 for chunk in chunks)
    assert tuple(chunk.splitlines()[0] for chunk in chunks) == tuple(
        f"[{index}/{len(chunks)}]" for index in range(1, len(chunks) + 1)
    )
    with pytest.raises(ValueError, match="must not be empty"):
        split_discord_message("\n\n")


def test_prepare_outbox_operations_binds_chunks_slots_hashes_and_unique_nonces() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    content = f"{'a' * 1_500}\n\n{'b' * 1_500}"
    chunks = split_discord_message(content)
    operations = prepare_outbox_operations(
        operation_prefix="initial-participant-a",
        debate_id=debate_id,
        attempt_id=attempt_id,
        bot_slot=DiscordBotSlot.PARTICIPANT_A,
        thread_id=THREAD_ID,
        content=content,
        nonce_sources=tuple(uuid7() for _ in chunks),
        created_at=NOW,
    )

    assert tuple(operation.content for operation in operations) == chunks
    assert tuple(operation.chunk_sequence for operation in operations) == tuple(range(len(chunks)))
    assert len({operation.nonce for operation in operations}) == len(operations)
    assert all(operation.bot_slot is DiscordBotSlot.PARTICIPANT_A for operation in operations)
    assert all(
        operation.content_hash == content_sha256(operation.content) for operation in operations
    )
    assert DiscordErrorCode.BOTS_NOT_READY.value == "DISCORD_BOTS_NOT_READY"

    with pytest.raises(ValueError, match="one UUIDv7"):
        prepare_outbox_operations(
            operation_prefix="post",
            debate_id=debate_id,
            attempt_id=attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id=THREAD_ID,
            content=content,
            nonce_sources=(),
            created_at=NOW,
        )


def test_prepare_initial_opinions_binds_three_bots_global_order_and_replay_stable_nonces() -> None:
    snapshot = initial_opinion_snapshot()

    operations = prepare_initial_opinion_outbox_operations(snapshot=snapshot, created_at=NOW)
    replay = prepare_initial_opinion_outbox_operations(snapshot=snapshot, created_at=NOW)

    assert operations == replay
    assert tuple(operation.bot_slot for operation in operations) == (
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (0, 8, 16)
    assert all(operation.phase is DebatePhase.DISCUSSING for operation in operations)
    assert all(operation.plan_id == "initial-opinions" for operation in operations)
    assert len({operation.nonce for operation in operations}) == 3
    assert all(len(operation.nonce) == 22 for operation in operations)
    assert all("@everyone" in operation.content for operation in operations)
    assert all("\\*" in operation.content for operation in operations)


def test_prepare_initial_opinions_rejects_wrong_phase_context_and_oversized_output() -> None:
    snapshot = initial_opinion_snapshot()
    with pytest.raises(ValueError, match="generation phase"):
        prepare_initial_opinion_outbox_operations(
            snapshot=replace(
                snapshot,
                state=snapshot.state.transition_to(
                    DebatePhase.DISCUSSING,
                    at=NOW + timedelta(seconds=3),
                ),
            ),
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="bound Discord thread"):
        prepare_initial_opinion_outbox_operations(
            snapshot=replace(snapshot, thread_id=None),
            created_at=NOW,
        )
    with pytest.raises(ValueError, match="each participant exactly once"):
        prepare_initial_opinion_outbox_operations(
            snapshot=replace(snapshot, initial_opinions=snapshot.initial_opinions[:2]),
            created_at=NOW,
        )
    oversized = replace(
        snapshot.initial_opinions[0],
        summary="x" * 18_000,
    )
    with pytest.raises(ValueError, match="reserved delivery sequence"):
        prepare_initial_opinion_outbox_operations(
            snapshot=replace(
                snapshot,
                initial_opinions=(oversized, *snapshot.initial_opinions[1:]),
            ),
            created_at=NOW,
        )


def test_prepare_final_proposals_binds_three_bots_reserved_order_and_stable_nonces() -> None:
    snapshot = final_proposal_snapshot()

    operations = prepare_final_proposal_outbox_operations(snapshot=snapshot, created_at=NOW)
    replay = prepare_final_proposal_outbox_operations(snapshot=snapshot, created_at=NOW)

    assert operations == replay
    assert tuple(operation.bot_slot for operation in operations) == (
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    )
    assert tuple(operation.delivery_sequence for operation in operations) == (100, 108, 116)
    assert all(operation.phase is DebatePhase.SELECTING_WINNER for operation in operations)
    assert all(operation.plan_id == "final-proposals" for operation in operations)
    assert len({operation.nonce for operation in operations}) == 3
    assert all(len(operation.nonce) == 22 for operation in operations)
    assert all("@everyone" in operation.content for operation in operations)
    assert all("\\*" in operation.content for operation in operations)


def test_prepare_final_proposals_rejects_wrong_phase_and_oversized_output() -> None:
    snapshot = final_proposal_snapshot()
    with pytest.raises(ValueError, match="generation phase"):
        prepare_final_proposal_outbox_operations(
            snapshot=replace(
                snapshot,
                state=snapshot.state.transition_to(
                    DebatePhase.SELECTING_WINNER,
                    at=NOW + timedelta(seconds=5),
                ),
            ),
            created_at=NOW,
        )
    oversized = replace(snapshot.final_proposals[0], proposal="x" * 18_000)
    with pytest.raises(ValueError, match="reserved delivery sequence"):
        prepare_final_proposal_outbox_operations(
            snapshot=replace(
                snapshot,
                final_proposals=(oversized, *snapshot.final_proposals[1:]),
            ),
            created_at=NOW,
        )


def test_outbox_and_panel_contracts_reject_invalid_external_identifiers_and_states() -> None:
    prepared = outbox()
    claimed = replace(
        prepared,
        status=OutboxStatus.CLAIMED,
        claim_owner="publisher",
        claim_expires_at=NOW + timedelta(seconds=60),
        delivery_attempt=1,
    )
    sent = replace(
        claimed,
        status=OutboxStatus.SENT,
        claim_owner=None,
        claim_expires_at=None,
        message_id=MESSAGE_ID,
        sent_at=NOW + timedelta(seconds=1),
    )
    assert sent.status is OutboxStatus.SENT

    with pytest.raises(ValueError, match="snowflake"):
        replace(prepared, thread_id="thread")
    with pytest.raises(ValueError, match="2000"):
        replace(prepared, content="x" * 2_001)
    with pytest.raises(ValueError, match="content hash"):
        replace(prepared, content_hash="bad")
    with pytest.raises(ValueError, match="nonce"):
        replace(prepared, nonce="bad")
    with pytest.raises(ValueError, match="chunk sequence"):
        replace(prepared, chunk_sequence=-1)
    with pytest.raises(ValueError, match="delivery attempt"):
        replace(prepared, delivery_attempt=-1)
    with pytest.raises(ValueError, match="owner and expiry"):
        replace(prepared, claim_owner="publisher")
    with pytest.raises(ValueError, match="attempted owner"):
        replace(prepared, status=OutboxStatus.CLAIMED)
    with pytest.raises(ValueError, match="requires message ID"):
        replace(prepared, status=OutboxStatus.SENT)
    with pytest.raises(ValueError, match="only a sent"):
        replace(prepared, message_id=MESSAGE_ID)
    with pytest.raises(ValueError, match="cannot retain a claim"):
        replace(prepared, claim_owner="publisher", claim_expires_at=NOW)
    with pytest.raises(ValueError, match=r"unattempted.*retry"):
        replace(prepared, next_retry_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="retain a retry"):
        replace(claimed, next_retry_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="retain delivery"):
        replace(sent, claim_owner="publisher", claim_expires_at=NOW)

    panel = PanelOperation(
        operation_id="cancel-operation",
        kind=PanelOperationKind.CANCEL,
        debate_id=prepared.debate_id,
        source_attempt_id=prepared.attempt_id,
        result_attempt_id=prepared.attempt_id,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        requester_id="requester",
        created_at=NOW,
        thread_id=THREAD_ID,
        message_id=MESSAGE_ID,
    )
    assert panel.kind is PanelOperationKind.CANCEL
    with pytest.raises(ValueError, match="new result"):
        replace(panel, kind=PanelOperationKind.RETRY)
    with pytest.raises(ValueError, match="preserve"):
        replace(panel, result_attempt_id=AttemptId.new())
    with pytest.raises(ValueError, match="control panel message"):
        replace(panel, message_id="panel-message")


def test_terminal_v2_operations_reserve_global_ranges_and_unique_nonces() -> None:
    source = terminal_snapshot()
    completed = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW + timedelta(seconds=7),
    )
    failed = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.FAILED,
        created_at=NOW + timedelta(seconds=7),
        error_code="provider_failed",
    )
    cancelled = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW + timedelta(seconds=7),
    )

    assert completed[0].delivery_sequence == 300
    assert failed[0].delivery_sequence == 900
    assert cancelled[0].delivery_sequence == 910
    assert len({completed[0].nonce, failed[0].nonce, cancelled[0].nonce}) == 3
    assert all(
        operation.record_schema_version == 2 for operation in (*completed, *failed, *cancelled)
    )
    assert "**最終決定**" in completed[0].content
    assert "> first \\*line\\*" in completed[0].content
    assert "> second" in completed[0].content
    assert "> - @everyone action" in completed[0].content


def test_model_display_sanitizer_normalizes_escapes_and_rejects_controls() -> None:
    assert sanitize_discord_model_text("e\u0301\r\n\t*bold* @everyone") == (
        "é\n \\*bold\\* @everyone"
    )
    for unsafe in ("bad\x00text", "bad\u202etext", "bad\ufdd0text"):
        with pytest.raises(ValueError, match="forbidden Unicode"):
            sanitize_discord_model_text(unsafe)


def test_outbox_v2_abandonment_is_bounded_and_cannot_retain_delivery_state() -> None:
    prepared = prepare_terminal_outbox_operations(
        snapshot=terminal_snapshot(),
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW + timedelta(seconds=7),
    )[0]
    abandoned = replace(
        prepared,
        status=OutboxStatus.ABANDONED,
        abandoned_at=NOW + timedelta(seconds=8),
        abandon_reason=DeliveryAbandonReason.CANCELLED,
    )

    assert abandoned.status is OutboxStatus.ABANDONED
    with pytest.raises(ValueError, match="exceeds its bound"):
        replace(prepared, delivery_attempt=4)
    with pytest.raises(ValueError, match="cannot retain delivery state"):
        replace(abandoned, next_retry_at=NOW + timedelta(seconds=9))


def test_outbox_v2_rejects_incomplete_or_mismatched_delivery_identity() -> None:
    prepared = prepare_terminal_outbox_operations(
        snapshot=terminal_snapshot(),
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW + timedelta(seconds=7),
    )[0]

    with pytest.raises(ValueError, match="requires phase, plan, sequence, and deadline"):
        replace(prepared, phase=None)
    with pytest.raises(ValueError, match="requires phase, plan, sequence, and deadline"):
        replace(prepared, plan_id=None)
    with pytest.raises(ValueError, match="requires phase, plan, sequence, and deadline"):
        replace(prepared, delivery_sequence=None)
    with pytest.raises(ValueError, match="requires phase, plan, sequence, and deadline"):
        replace(prepared, deadline_at=None)
    with pytest.raises(ValueError, match="cannot contain v2 delivery fields"):
        replace(prepared, record_schema_version=1)
    with pytest.raises(ValueError, match="abandonment cannot precede"):
        replace(
            prepared,
            status=OutboxStatus.ABANDONED,
            abandoned_at=NOW,
            abandon_reason=DeliveryAbandonReason.CANCELLED,
        )
    with pytest.raises(ValueError, match="timestamp and reason"):
        replace(prepared, status=OutboxStatus.ABANDONED)


def test_outbox_rejects_persisted_hash_schema_deadline_and_result_conflicts() -> None:
    legacy = outbox()
    v2 = prepare_terminal_outbox_operations(
        snapshot=terminal_snapshot(),
        target_phase=DebatePhase.CANCELLED,
        created_at=NOW + timedelta(seconds=7),
    )[0]

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        replace(legacy, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="must match the UTF-8"):
        replace(legacy, content_hash="a" * 64)
    with pytest.raises(ValueError, match="unsupported outbox"):
        replace(legacy, record_schema_version=3)
    with pytest.raises(ValueError, match="delivery sequence"):
        replace(v2, delivery_sequence=True)
    with pytest.raises(ValueError, match="exactly 15 minutes"):
        replace(v2, deadline_at=v2.created_at + timedelta(minutes=14))
    with pytest.raises(ValueError, match="only an abandoned"):
        replace(legacy, abandon_reason=DeliveryAbandonReason.CANCELLED)
    with pytest.raises(ValueError, match="positive delivery attempt"):
        replace(
            legacy,
            status=OutboxStatus.SENT,
            message_id=MESSAGE_ID,
            sent_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="only outbox v2"):
        replace(
            legacy,
            status=OutboxStatus.ABANDONED,
            abandoned_at=NOW + timedelta(seconds=1),
            abandon_reason=DeliveryAbandonReason.CANCELLED,
        )


def test_terminal_delivery_rejects_invalid_target_content_and_bounds() -> None:
    source = terminal_snapshot()

    with pytest.raises(ValueError, match="active attempt"):
        prepare_terminal_outbox_operations(
            snapshot=replace(
                source,
                state=source.state.transition_to(
                    DebatePhase.CANCELLED,
                    at=NOW + timedelta(seconds=8),
                ),
            ),
            target_phase=DebatePhase.CANCELLED,
            created_at=NOW + timedelta(seconds=9),
        )
    with pytest.raises(ValueError, match="bound Discord thread"):
        prepare_terminal_outbox_operations(
            snapshot=replace(source, thread_id=None),
            target_phase=DebatePhase.CANCELLED,
            created_at=NOW + timedelta(seconds=8),
        )
    with pytest.raises(ValueError, match="completed delivery requires"):
        prepare_terminal_outbox_operations(
            snapshot=replace(source, final_decision=None),
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW + timedelta(seconds=8),
        )
    with pytest.raises(ValueError, match="completed delivery requires"):
        prepare_terminal_outbox_operations(
            snapshot=source,
            target_phase=DebatePhase.COMPLETED,
            created_at=NOW + timedelta(seconds=8),
            error_code="provider_failed",
        )
    with pytest.raises(ValueError, match="failed delivery requires"):
        prepare_terminal_outbox_operations(
            snapshot=source,
            target_phase=DebatePhase.FAILED,
            created_at=NOW + timedelta(seconds=8),
        )
    with pytest.raises(ValueError, match="cancelled delivery cannot"):
        prepare_terminal_outbox_operations(
            snapshot=source,
            target_phase=DebatePhase.CANCELLED,
            created_at=NOW + timedelta(seconds=8),
            error_code="provider_failed",
        )
    with pytest.raises(ValueError, match="target must be"):
        prepare_terminal_outbox_operations(
            snapshot=source,
            target_phase=DebatePhase.DISCUSSING,
            created_at=NOW + timedelta(seconds=8),
        )


def test_terminal_renderer_preserves_quotes_and_escapes_across_chunk_boundaries() -> None:
    source = replace(
        terminal_snapshot(),
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "word " * 1_000 + "a" * 1_971 + "*" * 40,
            ("action " * 600,),
            (),
        ),
    )

    operations = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW + timedelta(seconds=7),
    )

    assert len(operations) > 1
    assert all(len(operation.content) <= 2_000 for operation in operations)
    for operation in operations:
        body = operation.content.split("\n", 1)[1]
        for line in body.splitlines():
            if "word" in line or "action" in line or "a" * 100 in line or "\\*" in line:
                assert line.startswith(">")
                assert not line.startswith("*")
                assert not line.endswith("\\")


def test_model_display_sanitizer_escapes_complete_discord_markdown_surface() -> None:
    assert sanitize_discord_model_text(r"\\`*_{}[]()<>#+-.!|>~=") == (
        r"\\\\\`\*\_\{\}\[\]\(\)\<\>\#\+\-\.\!\|\>\~\="
    )


def test_terminal_renderer_preserves_intentional_blank_quoted_lines() -> None:
    source = replace(
        terminal_snapshot(),
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "first\n\nthird",
            (),
            (),
        ),
    )

    operation = prepare_terminal_outbox_operations(
        snapshot=source,
        target_phase=DebatePhase.COMPLETED,
        created_at=NOW + timedelta(seconds=7),
    )[0]

    assert "> first\n>\n> third" in operation.content
    with pytest.raises(ValueError, match="must not be empty"):
        sanitize_discord_model_text(" \t\r\n")


def test_panel_custom_id_rejects_invalid_action_and_non_uuid_attempt() -> None:
    debate_id = DebateId.new()

    with pytest.raises(ValueError, match="invalid panel custom ID"):
        PanelCustomId.parse(f"shittim:v1:{debate_id}:operation:unknown")
    invalid = PanelCustomId(debate_id, "z" * 32 + "c", PanelAction.CANCEL)
    with pytest.raises(ValueError, match="does not contain a UUIDv7"):
        invalid.expected_attempt_id()
