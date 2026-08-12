"""Offline contract for one direct participant farewell message."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shittim_chest.adapters.discord import DiscordFarewellSender, FarewellDeliveryError
from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.domain import ParticipantSlot


def config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id="101",
        allowed_channel_ids=frozenset({"102", "103"}),
        farewell_channel_id="103",
        identities=tuple(
            DiscordIdentityConfig(slot, str(201 + index))
            for index, slot in enumerate(DISCORD_BOT_SLOTS)
        ),
        schema_version="2",
    )


def clients() -> dict[DiscordBotSlot, discord.Client]:
    values: dict[DiscordBotSlot, discord.Client] = {}
    for index, slot in enumerate(DISCORD_BOT_SLOTS):
        client = MagicMock(spec=discord.Client)
        client.user = SimpleNamespace(id=400 + index)
        client.is_ready.return_value = True
        client.fetch_channel = AsyncMock()
        values[slot] = cast(discord.Client, client)
    return values


@pytest.mark.asyncio
async def test_selected_participant_posts_once_to_normal_channel_and_verifies_ack() -> None:
    bot_clients = clients()
    selected = cast(Any, bot_clients[DiscordBotSlot.PARTICIPANT_B])
    member = SimpleNamespace(id=selected.user.id)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 103
    channel.guild = SimpleNamespace(id=101, me=member)
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    selected.get_channel.return_value = channel
    message = MagicMock(spec=discord.Message)
    message.channel = channel
    message.author = selected.user
    message.content = "みんな、また楽しく話そうね!" * 5
    message.nonce = "A" * 22
    channel.send = AsyncMock(return_value=message)
    sender = DiscordFarewellSender(clients=bot_clients, config=config())

    await sender.send(
        participant=ParticipantSlot.PARTICIPANT_B,
        content=message.content,
        nonce=message.nonce,
    )

    channel.send.assert_awaited_once()
    assert channel.send.await_args is not None
    kwargs = channel.send.await_args.kwargs
    assert kwargs["nonce"] == "A" * 22
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert kwargs["suppress_embeds"] is True
    for slot, client in bot_clients.items():
        if slot is not DiscordBotSlot.PARTICIPANT_B:
            cast(Any, client).get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_thread_or_missing_permission_is_rejected_without_a_post() -> None:
    bot_clients = clients()
    selected = cast(Any, bot_clients[DiscordBotSlot.PARTICIPANT_A])
    selected.get_channel.return_value = MagicMock(spec=discord.Thread)
    sender = DiscordFarewellSender(clients=bot_clients, config=config())

    with pytest.raises(FarewellDeliveryError, match="farewell_channel_not_text"):
        await sender.send(
            participant=ParticipantSlot.PARTICIPANT_A,
            content="x" * 60,
            nonce="B" * 22,
        )

    selected.get_channel.return_value.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (429, "farewell_discord_unavailable"),
        (503, "farewell_discord_unavailable"),
        (400, "farewell_delivery_rejected"),
    ],
)
@pytest.mark.asyncio
async def test_http_failures_are_classified_for_bounded_retry(
    status: int,
    expected_code: str,
) -> None:
    bot_clients = clients()
    selected = cast(Any, bot_clients[DiscordBotSlot.PARTICIPANT_C])
    member = SimpleNamespace(id=selected.user.id)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 103
    channel.guild = SimpleNamespace(id=101, me=member)
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    selected.get_channel.return_value = channel
    response = cast(Any, SimpleNamespace(status=status, reason="test status"))
    channel.send = AsyncMock(side_effect=discord.HTTPException(response, "test"))
    sender = DiscordFarewellSender(clients=bot_clients, config=config())

    with pytest.raises(FarewellDeliveryError) as captured:
        await sender.send(
            participant=ParticipantSlot.PARTICIPANT_C,
            content="挨拶\n参考リンク: https://example.test/source",
            nonce="C" * 22,
        )

    assert captured.value.code == expected_code
