"""Offline contracts for four-client readiness and lifecycle ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shittim_chest.adapters.discord import (
    DiscordClientSupervisor,
    DiscordPyGateway,
    build_discord_clients,
)
from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    AcceptDebateRequest,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)


def config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id="101",
        allowed_channel_ids=frozenset({"102"}),
        identities=tuple(
            DiscordIdentityConfig(slot, str(201 + index))
            for index, slot in enumerate(DISCORD_BOT_SLOTS)
        ),
        schema_version="runtime-v1",
    )


def request(*, guild_id: str = "101", channel_id: str = "102") -> AcceptDebateRequest:
    return AcceptDebateRequest("question", "301", guild_id, channel_id, "401")


def mocked_clients() -> dict[DiscordBotSlot, discord.Client]:
    clients: dict[DiscordBotSlot, discord.Client] = {}
    for slot in DISCORD_BOT_SLOTS:
        client = MagicMock(spec=discord.Client)
        client.user = SimpleNamespace(id=500 + len(clients))
        client.is_ready.return_value = True
        client.start = AsyncMock()
        client.close = AsyncMock()
        clients[slot] = cast(discord.Client, client)
    return clients


def test_client_builder_uses_guilds_only_safe_mentions_and_bounded_rate_limits() -> None:
    clients = build_discord_clients(config())

    assert set(clients) == set(DISCORD_BOT_SLOTS)
    assert len({id(client) for client in clients.values()}) == 4
    for slot, client in clients.items():
        assert client.application_id == int(config().application_id_for(slot))
        assert client.intents.guilds
        assert not client.intents.message_content
        assert not client.intents.members
        assert client.allowed_mentions is not None
        assert cast(Any, client.allowed_mentions).to_dict() == {"parse": []}
        assert client.http.max_ratelimit_timeout == 30.0


@pytest.mark.asyncio
async def test_gateway_closes_acceptance_when_one_identity_is_not_ready() -> None:
    clients = mocked_clients()
    gateway = DiscordPyGateway(clients=clients, config=config())

    assert await gateway.all_identities_ready()
    assert await gateway.request_is_allowed(request())
    assert not await gateway.request_is_allowed(request(channel_id="999"))

    cast(Any, clients[DiscordBotSlot.PARTICIPANT_B]).is_ready.return_value = False
    assert not await gateway.all_identities_ready()


@pytest.mark.asyncio
async def test_supervisor_starts_and_closes_every_client_when_one_exits() -> None:
    clients = mocked_clients()
    blocker = asyncio.Event()

    async def wait_until_cancelled(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await blocker.wait()

    for slot in DISCORD_BOT_SLOTS[1:]:
        cast(Any, clients[slot]).start.side_effect = wait_until_cancelled
    supervisor = DiscordClientSupervisor(clients)
    tokens = {slot: f"token-{index}" for index, slot in enumerate(DISCORD_BOT_SLOTS)}

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        await supervisor.run(tokens)

    for slot, client in clients.items():
        cast(Any, client).start.assert_awaited_once_with(tokens[slot], reconnect=True)
        cast(Any, client).close.assert_awaited_once()


@pytest.mark.asyncio
async def test_supervisor_rejects_missing_duplicate_or_empty_tokens_before_start() -> None:
    clients = mocked_clients()
    supervisor = DiscordClientSupervisor(clients)
    valid = {slot: f"token-{index}" for index, slot in enumerate(DISCORD_BOT_SLOTS)}

    with pytest.raises(ValueError, match="exactly one"):
        await supervisor.run({DiscordBotSlot.MODERATOR: "token"})
    with pytest.raises(ValueError, match="distinct"):
        await supervisor.run(dict.fromkeys(DISCORD_BOT_SLOTS, "duplicate"))
    with pytest.raises(ValueError, match="must not be empty"):
        await supervisor.run({**valid, DiscordBotSlot.MODERATOR: " "})

    for client in clients.values():
        cast(Any, client).start.assert_not_awaited()
