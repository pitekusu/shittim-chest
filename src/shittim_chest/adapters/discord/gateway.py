"""discord.py client construction, readiness policy, and owned lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

import discord

from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    AcceptDebateRequest,
    DiscordBotSlot,
    DiscordRuntimeConfig,
)

DISCORD_MAX_RATELIMIT_TIMEOUT_SECONDS = 30.0
InteractionHandler = Callable[[discord.Interaction[discord.Client]], Awaitable[None]]


class DiscordModeratorClient(discord.Client):
    """Dedicated moderator client with one explicit component interaction handler."""

    _interaction_handler: InteractionHandler | None = None

    def set_interaction_handler(self, handler: InteractionHandler) -> None:
        """Register the sole controller-owned component dispatch boundary."""

        if self._interaction_handler is not None:
            raise ValueError("moderator interaction handler is already registered")
        self._interaction_handler = handler

    def clear_interaction_handler(self) -> None:
        """Detach the controller handler during deterministic shutdown."""

        self._interaction_handler = None

    async def on_interaction(self, interaction: discord.Interaction[discord.Client]) -> None:
        """Dispatch components without discord.py's deprecated event decorator path."""

        if self._interaction_handler is not None:
            await self._interaction_handler(interaction)


def build_discord_clients(
    config: DiscordRuntimeConfig,
) -> dict[DiscordBotSlot, discord.Client]:
    """Create exactly four least-privilege clients without reading Bot tokens."""

    clients: dict[DiscordBotSlot, discord.Client] = {}
    for slot in DISCORD_BOT_SLOTS:
        intents = discord.Intents.none()
        intents.guilds = True
        client_type = DiscordModeratorClient if slot is DiscordBotSlot.MODERATOR else discord.Client
        clients[slot] = client_type(
            application_id=int(config.application_id_for(slot)),
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_ratelimit_timeout=DISCORD_MAX_RATELIMIT_TIMEOUT_SECONDS,
        )
    return clients


class DiscordPyGateway:
    """Expose four-client readiness and the configured Guild/channel boundary."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        config: DiscordRuntimeConfig,
    ) -> None:
        if set(clients) != set(DISCORD_BOT_SLOTS):
            raise ValueError("gateway requires exactly one client for each Discord Bot slot")
        if len({id(client) for client in clients.values()}) != len(DISCORD_BOT_SLOTS):
            raise ValueError("gateway Discord clients must be distinct")
        self._clients = dict(clients)
        self._config = config

    async def all_identities_ready(self) -> bool:
        """Return true only while every configured Bot identity is READY."""

        return all(
            client.is_ready() and client.user is not None for client in self._clients.values()
        )

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        """Apply the fail-closed public Guild/channel allowlist."""

        return self._config.allows(guild_id=request.guild_id, channel_id=request.channel_id)


class DiscordClientSupervisor:
    """Own four client start tasks and close every client when one task exits."""

    def __init__(self, clients: Mapping[DiscordBotSlot, discord.Client]) -> None:
        if set(clients) != set(DISCORD_BOT_SLOTS):
            raise ValueError("supervisor requires exactly one client for each Discord Bot slot")
        self._clients = dict(clients)

    async def run(self, tokens: Mapping[DiscordBotSlot, str]) -> None:
        """Run all clients until cancellation or an unexpected client exit."""

        self._validate_tokens(tokens)
        tasks = {
            slot: asyncio.create_task(
                self._clients[slot].start(tokens[slot], reconnect=True),
                name=f"discord:{slot.value}",
            )
            for slot in DISCORD_BOT_SLOTS
        }
        try:
            done, _ = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError("a Discord client stopped unexpectedly")
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            await self.close()

    async def close(self) -> None:
        """Close every client concurrently without exposing token values."""

        async with asyncio.TaskGroup() as group:
            for slot, client in self._clients.items():
                group.create_task(client.close(), name=f"discord-close:{slot.value}")

    @staticmethod
    def _validate_tokens(tokens: Mapping[DiscordBotSlot, str]) -> None:
        if set(tokens) != set(DISCORD_BOT_SLOTS):
            raise ValueError("exactly one Bot token is required for each Discord Bot slot")
        values = tuple(tokens[slot] for slot in DISCORD_BOT_SLOTS)
        if any(not token.strip() for token in values):
            raise ValueError("Bot tokens must not be empty")
        if len(set(values)) != len(values):
            raise ValueError("Bot tokens must be distinct")
