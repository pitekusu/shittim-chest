"""Bounded, read-only Discord verification for a standalone release task."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping

import discord

from shittim_chest.adapters.discord.bootstrap_api import (
    DiscordBootstrapApi,
    create_discord_bootstrap_http_client,
)
from shittim_chest.adapters.discord.command_schema import (
    CommandSchemaError,
    classify_command_inventory,
    command_schema_hash,
)
from shittim_chest.adapters.discord.gateway import build_discord_clients
from shittim_chest.application import DISCORD_BOT_SLOTS, DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.config import parse_discord_runtime_config
from shittim_chest.config.models import StartupConfigurationError

LOGGER = logging.getLogger(__name__)
READY_TIMEOUT_SECONDS = 120.0
PROCESS_TIMEOUT_SECONDS = 180.0
READINESS_POLL_SECONDS = 0.25

_TOKEN_ENV = {
    DiscordBotSlot.MODERATOR: "DISCORD_TOKEN_MODERATOR",
    DiscordBotSlot.PARTICIPANT_A: "DISCORD_TOKEN_PARTICIPANT_A",
    DiscordBotSlot.PARTICIPANT_B: "DISCORD_TOKEN_PARTICIPANT_B",
    DiscordBotSlot.PARTICIPANT_C: "DISCORD_TOKEN_PARTICIPANT_C",
}


class DiscordReleaseSmokeError(RuntimeError):
    """Stable, content-free standalone smoke failure."""

    __slots__ = ("category",)

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class DiscordReleaseSmoke:
    """Connect exactly four identities and perform only read operations."""

    __slots__ = ("_api_factory", "_clients", "_config", "_tokens")

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        config: DiscordRuntimeConfig,
        tokens: Mapping[DiscordBotSlot, str],
        api_factory: Callable[[str], DiscordBootstrapApi],
    ) -> None:
        if set(clients) != set(DISCORD_BOT_SLOTS):
            raise ValueError("smoke requires exactly four Discord clients")
        if set(tokens) != set(DISCORD_BOT_SLOTS):
            raise ValueError("smoke requires exactly four Discord tokens")
        token_values = tuple(tokens[slot] for slot in DISCORD_BOT_SLOTS)
        if any(not value.strip() for value in token_values) or len(set(token_values)) != 4:
            raise ValueError("smoke Discord tokens must be non-empty and distinct")
        self._clients = dict(clients)
        self._config = config
        self._tokens = dict(tokens)
        self._api_factory = api_factory

    async def run(self) -> None:
        """Complete the read-only verification or fail within the process deadline."""

        LOGGER.info("smoke_started")
        try:
            async with asyncio.timeout(PROCESS_TIMEOUT_SECONDS):
                await self._run_clients()
        except TimeoutError:
            raise DiscordReleaseSmokeError("process_timeout") from None

    async def _run_clients(self) -> None:
        tasks = {
            slot: asyncio.create_task(
                client.start(self._tokens[slot], reconnect=False),
                name=f"discord-smoke:{slot.value}",
            )
            for slot, client in self._clients.items()
        }
        try:
            await self._wait_until_ready(tasks)
            self._verify_cached_guild_permissions()
            await asyncio.to_thread(self._verify_rest_inventory)
            LOGGER.info("guild_verified")
            LOGGER.info("channel_permissions_verified")
            LOGGER.info("participant_commands_zero")
            LOGGER.info("smoke_passed")
        finally:
            await self._close_clients(tasks)

    async def _wait_until_ready(self, tasks: Mapping[DiscordBotSlot, asyncio.Task[None]]) -> None:
        reported: set[DiscordBotSlot] = set()
        try:
            async with asyncio.timeout(READY_TIMEOUT_SECONDS):
                while len(reported) != len(DISCORD_BOT_SLOTS):
                    for slot, task in tasks.items():
                        if task.done():
                            task.result()
                            raise DiscordReleaseSmokeError("gateway_stopped")
                        client = self._clients[slot]
                        if client.is_ready() and client.user is not None and slot not in reported:
                            expected = self._config.application_id_for(slot)
                            if str(client.user.id) != expected:
                                raise DiscordReleaseSmokeError("identity_mismatch")
                            reported.add(slot)
                            LOGGER.info("identity_ready slot=%s", slot.value)
                    if len(reported) != len(DISCORD_BOT_SLOTS):
                        await asyncio.sleep(READINESS_POLL_SECONDS)
        except TimeoutError:
            raise DiscordReleaseSmokeError("ready_timeout") from None

    def _verify_cached_guild_permissions(self) -> None:
        for slot, client in self._clients.items():
            guild = client.get_guild(int(self._config.guild_id))
            if guild is None or guild.me is None:
                raise DiscordReleaseSmokeError("guild_unavailable")
            for channel_id in self._config.allowed_channel_ids:
                channel = guild.get_channel(int(channel_id))
                if not isinstance(channel, discord.TextChannel):
                    raise DiscordReleaseSmokeError("channel_unavailable")
                permissions = channel.permissions_for(guild.me)
                required = (
                    "view_channel",
                    "read_message_history",
                    "send_messages_in_threads",
                )
                if slot is DiscordBotSlot.MODERATOR:
                    required += ("send_messages", "embed_links", "create_public_threads")
                if any(not getattr(permissions, name) for name in required):
                    raise DiscordReleaseSmokeError("permission_denied")

    def _verify_rest_inventory(self) -> None:
        for slot in DISCORD_BOT_SLOTS:
            api = self._api_factory(self._tokens[slot])
            try:
                expected_application = self._config.application_id_for(slot)
                user = api.get_current_user()
                application = api.get_current_application()
                if (
                    user.get("id") != expected_application
                    or application.get("id") != expected_application
                ):
                    raise DiscordReleaseSmokeError("identity_mismatch")
                if api.get_guild(self._config.guild_id).get("id") != self._config.guild_id:
                    raise DiscordReleaseSmokeError("guild_unavailable")
                for channel_id in self._config.allowed_channel_ids:
                    channel = api.get_channel(channel_id)
                    if (
                        channel.get("id") != channel_id
                        or channel.get("guild_id") != self._config.guild_id
                    ):
                        raise DiscordReleaseSmokeError("channel_unavailable")
                global_commands = api.get_global_commands(expected_application)
                guild_commands = api.get_guild_commands(
                    expected_application,
                    self._config.guild_id,
                )
                if global_commands:
                    raise DiscordReleaseSmokeError("unsafe_command_inventory")
                if slot is DiscordBotSlot.MODERATOR:
                    try:
                        classify_command_inventory(guild_commands)
                    except CommandSchemaError:
                        raise DiscordReleaseSmokeError("unsafe_command_inventory") from None
                elif guild_commands:
                    raise DiscordReleaseSmokeError("participant_command_present")
            finally:
                api.close()

    async def _close_clients(
        self,
        tasks: Mapping[DiscordBotSlot, asyncio.Task[None]],
    ) -> None:
        await asyncio.gather(
            *(client.close() for client in self._clients.values()),
            return_exceptions=True,
        )
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


def load_smoke_environment(
    environ: Mapping[str, str],
) -> tuple[DiscordRuntimeConfig, Mapping[DiscordBotSlot, str]]:
    """Validate only the five injected secrets and immutable schema hash."""

    try:
        runtime, _ = parse_discord_runtime_config(_required(environ, "SHITTIM_RUNTIME_CONFIG_JSON"))
        expected_hash = _required(environ, "SHITTIM_EXPECTED_COMMAND_SCHEMA_HASH")
        if expected_hash != command_schema_hash():
            raise ValueError("command schema hash mismatch")
        tokens = {slot: _required(environ, name) for slot, name in _TOKEN_ENV.items()}
        if len(set(tokens.values())) != len(DISCORD_BOT_SLOTS):
            raise ValueError("Discord tokens must be distinct")
        return runtime, tokens
    except KeyError, TypeError, ValueError:
        raise StartupConfigurationError from None


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ[name]
    if not value.strip():
        raise ValueError("required smoke setting is empty")
    return value


def _api_factory(token: str) -> DiscordBootstrapApi:
    return DiscordBootstrapApi(client=create_discord_bootstrap_http_client(), bot_token=token)


async def run_from_environment(environ: Mapping[str, str] | None = None) -> None:
    """Run the real bounded smoke without constructing any AWS or OpenAI SDK client."""

    runtime, tokens = load_smoke_environment(os.environ if environ is None else environ)
    smoke = DiscordReleaseSmoke(
        clients=build_discord_clients(runtime),
        config=runtime,
        tokens=tokens,
        api_factory=_api_factory,
    )
    await smoke.run()


def main() -> int:
    """Process entrypoint that emits only one stable failure category."""

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(run_from_environment())
    except Exception as error:
        category = (
            error.category if isinstance(error, DiscordReleaseSmokeError) else "internal_error"
        )
        LOGGER.error("smoke_failed category=%s", category)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DiscordReleaseSmoke",
    "DiscordReleaseSmokeError",
    "load_smoke_environment",
    "main",
    "run_from_environment",
)
