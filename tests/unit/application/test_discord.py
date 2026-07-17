"""Tests for SDK-independent Discord contracts and deterministic formatting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest

from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
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
    prepare_outbox_operations,
    split_discord_message,
)
from shittim_chest.domain import AttemptId, DebateId

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


def test_message_split_is_deterministic_bounded_and_prefers_paragraphs() -> None:
    assert split_discord_message(" short ") == ("short",)
    content = f"{'a' * 1_200}\n\n{'b' * 1_200}\nline\n{'c' * 2_100}"

    chunks = split_discord_message(content)

    assert chunks == split_discord_message(content)
    assert len(chunks) >= 3
    assert all(len(chunk) <= 2_000 for chunk in chunks)
    assert tuple(chunk.split(" ", 1)[0] for chunk in chunks) == tuple(
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


def test_outbox_and_panel_contracts_reject_invalid_external_identifiers_and_states() -> None:
    prepared = outbox()
    claimed = replace(
        prepared,
        status=OutboxStatus.CLAIMED,
        claim_owner="publisher",
        claim_expires_at=NOW + timedelta(seconds=60),
    )
    sent = replace(
        claimed,
        status=OutboxStatus.SENT,
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
    with pytest.raises(ValueError, match="requires an owner"):
        replace(prepared, status=OutboxStatus.CLAIMED)
    with pytest.raises(ValueError, match="requires message ID"):
        replace(prepared, status=OutboxStatus.SENT)
    with pytest.raises(ValueError, match="only a sent"):
        replace(prepared, message_id=MESSAGE_ID)

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
