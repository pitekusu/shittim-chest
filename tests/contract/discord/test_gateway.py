"""Offline contracts for four-client readiness and lifecycle ownership."""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import discord.http
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
        farewell_channel_id="102",
        identities=tuple(
            DiscordIdentityConfig(slot, str(201 + index))
            for index, slot in enumerate(DISCORD_BOT_SLOTS)
        ),
        schema_version="runtime-v1",
    )


def request(*, guild_id: str = "101", channel_id: str = "102") -> AcceptDebateRequest:
    return AcceptDebateRequest(
        "question",
        "301",
        "pitekusu",
        "ぬし",
        guild_id,
        channel_id,
        "401",
    )


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


@pytest.mark.asyncio
async def test_client_builder_uses_guilds_only_safe_mentions_and_bounded_rate_limits() -> None:
    clients = build_discord_clients(config())

    try:
        assert set(clients) == set(DISCORD_BOT_SLOTS)
        assert len({id(client) for client in clients.values()}) == 4
        assert len({id(client.http.connector) for client in clients.values()}) == 4
        for slot, client in clients.items():
            assert client.application_id == int(config().application_id_for(slot))
            assert client.intents.guilds
            assert not client.intents.message_content
            assert not client.intents.members
            assert client.allowed_mentions is not None
            assert cast(Any, client.allowed_mentions).to_dict() == {"parse": []}
            assert client.http.http_trace is not None
            assert len(client.http.http_trace.on_request_end) == 1
            assert client.http.max_ratelimit_timeout == 300.0
            connector = client.http.connector
            assert isinstance(connector, aiohttp.TCPConnector)
            assert connector.limit == 0
            context = cast(Any, connector)._ssl
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname
    finally:
        supervisor = DiscordClientSupervisor(clients)
        await supervisor.close()
        await supervisor.close()
    assert all(client.http.connector.closed for client in clients.values())


@pytest.mark.asyncio
async def test_failed_login_closes_every_owned_connector(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = build_discord_clients(config())
    monkeypatch.setattr(
        discord.http.HTTPClient,
        "static_login",
        AsyncMock(side_effect=discord.LoginFailure("offline fixture")),
    )
    supervisor = DiscordClientSupervisor(clients)

    with pytest.raises(discord.LoginFailure):
        await supervisor.run({slot: f"fixture-{slot.value}" for slot in DISCORD_BOT_SLOTS})

    assert all(client.http.connector.closed for client in clients.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("offline fixture"), asyncio.CancelledError()])
async def test_owned_connector_closes_when_sdk_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    clients = build_discord_clients(config())
    client = clients[DiscordBotSlot.MODERATOR]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(discord.Client, "close", AsyncMock(side_effect=failure))
            with pytest.raises(type(failure)):
                await client.close()
        assert client.http.connector.closed
    finally:
        await DiscordClientSupervisor(clients).close()


@pytest.mark.asyncio
async def test_client_rate_limit_ceiling_allows_a_300_second_bucket_with_capacity() -> None:
    ratelimit = discord.http.Ratelimit(300.0)
    ratelimit.update(
        cast(
            Any,
            SimpleNamespace(
                headers={
                    "X-Ratelimit-Limit": "50",
                    "X-Ratelimit-Remaining": "49",
                    "X-Ratelimit-Reset-After": "300.0",
                }
            ),
        )
    )

    await ratelimit.acquire()

    assert ratelimit.remaining == 48


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
async def test_gateway_preflights_exact_thread_permissions_without_a_write() -> None:
    clients = mocked_clients()
    moderator = cast(Any, clients[DiscordBotSlot.MODERATOR])
    member = SimpleNamespace(id=moderator.user.id)
    guild = SimpleNamespace(id=101, me=member)
    permissions = SimpleNamespace(
        view_channel=True,
        send_messages_in_threads=True,
        read_message_history=True,
    )
    thread = MagicMock(spec=discord.Thread)
    thread.guild = guild
    thread.archived = False
    thread.locked = False
    thread.permissions_for.return_value = permissions
    moderator.get_channel.return_value = thread
    gateway = DiscordPyGateway(clients=clients, config=config())

    assert await gateway.delivery_target_is_ready(
        bot_slot=DiscordBotSlot.MODERATOR,
        guild_id="101",
        thread_id="102",
    )
    moderator.fetch_channel.assert_not_awaited()
    thread.permissions_for.assert_called_once_with(member)

    permissions.send_messages_in_threads = False
    assert not await gateway.delivery_target_is_ready(
        bot_slot=DiscordBotSlot.MODERATOR,
        guild_id="101",
        thread_id="102",
    )
    permissions.send_messages_in_threads = True
    thread.archived = True
    assert not await gateway.delivery_target_is_ready(
        bot_slot=DiscordBotSlot.MODERATOR,
        guild_id="101",
        thread_id="102",
    )


@pytest.mark.asyncio
async def test_gateway_preflight_fails_closed_for_unknown_or_wrong_guild_thread() -> None:
    clients = mocked_clients()
    moderator = cast(Any, clients[DiscordBotSlot.MODERATOR])
    moderator.get_channel.return_value = None
    moderator.fetch_channel.return_value = None
    gateway = DiscordPyGateway(clients=clients, config=config())

    assert not await gateway.delivery_target_is_ready(
        bot_slot=DiscordBotSlot.MODERATOR,
        guild_id="101",
        thread_id="not-a-snowflake",
    )
    assert not await gateway.delivery_target_is_ready(
        bot_slot=DiscordBotSlot.MODERATOR,
        guild_id="101",
        thread_id="102",
    )


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
