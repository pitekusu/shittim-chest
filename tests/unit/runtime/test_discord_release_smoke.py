from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import discord
import pytest

import shittim_chest.runtime.discord_release_smoke as smoke_module
from shittim_chest.adapters.discord.bootstrap_api import DiscordBootstrapApi
from shittim_chest.adapters.discord.command_schema import (
    canonical_command_payload,
    command_schema_hash,
)
from shittim_chest.application import (
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.config import StartupConfigurationError
from shittim_chest.runtime.discord_release_smoke import (
    DiscordReleaseSmoke,
    DiscordReleaseSmokeError,
    load_smoke_environment,
)


def runtime() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id="101",
        allowed_channel_ids=frozenset({"201"}),
        identities=tuple(
            DiscordIdentityConfig(slot=slot, application_id=str(301 + index))
            for index, slot in enumerate(DiscordBotSlot)
        ),
        schema_version="1",
    )


def environment() -> dict[str, str]:
    return {
        "SHITTIM_EXPECTED_COMMAND_SCHEMA_HASH": command_schema_hash(),
        "SHITTIM_RUNTIME_CONFIG_JSON": json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "guild_id": "101",
                "allowed_channel_ids": ["201"],
                "identities": [
                    {"slot": slot.value, "application_id": str(301 + index)}
                    for index, slot in enumerate(DiscordBotSlot)
                ],
            }
        ),
        "DISCORD_TOKEN_MODERATOR": "token-moderator-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_A": "token-a-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_B": "token-b-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_C": "token-c-placeholder",
    }


@dataclass(slots=True)
class FakePermissions:
    view_channel: bool = True
    read_message_history: bool = True
    send_messages_in_threads: bool = True
    send_messages: bool = True
    embed_links: bool = True
    create_public_threads: bool = True


class FakeChannel:
    def permissions_for(self, member: object) -> FakePermissions:
        del member
        return FakePermissions()


class FakeGuild:
    me = object()

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return FakeChannel() if channel_id == 201 else None


class FakeClient:
    def __init__(self, application_id: str, *, ready: bool = True) -> None:
        self.user = SimpleNamespace(id=int(application_id))
        self._ready = ready
        self._closed = asyncio.Event()

    async def start(self, token: str, *, reconnect: bool) -> None:
        assert token.startswith("token-")
        assert not reconnect
        await self._closed.wait()

    def is_ready(self) -> bool:
        return self._ready

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return FakeGuild() if guild_id == 101 else None

    async def close(self) -> None:
        self._closed.set()


class FakeApi:
    def __init__(self, application_id: str, *, moderator: bool) -> None:
        self.application_id = application_id
        self.moderator = moderator

    def get_current_user(self) -> dict[str, object]:
        return {"id": self.application_id}

    def get_current_application(self) -> dict[str, object]:
        return {"id": self.application_id}

    def get_guild(self, guild_id: str) -> dict[str, object]:
        return {"id": guild_id}

    def get_channel(self, channel_id: str) -> dict[str, object]:
        return {"id": channel_id, "guild_id": "101"}

    def get_global_commands(self, application_id: str) -> tuple[object, ...]:
        assert application_id == self.application_id
        return ()

    def get_guild_commands(
        self,
        application_id: str,
        guild_id: str,
    ) -> tuple[dict[str, object], ...]:
        assert application_id == self.application_id
        assert guild_id == "101"
        return (canonical_command_payload(),) if self.moderator else ()

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_smoke_verifies_four_ready_identities_without_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = runtime()
    tokens = {slot: f"token-{slot.value}-placeholder" for slot in DiscordBotSlot}
    clients = {
        slot: cast(discord.Client, FakeClient(config.application_id_for(slot)))
        for slot in DiscordBotSlot
    }
    application_by_token = {
        tokens[slot]: (config.application_id_for(slot), slot is DiscordBotSlot.MODERATOR)
        for slot in DiscordBotSlot
    }

    def api_factory(token: str) -> DiscordBootstrapApi:
        application_id, moderator = application_by_token[token]
        return cast(DiscordBootstrapApi, FakeApi(application_id, moderator=moderator))

    monkeypatch.setattr(smoke_module.discord, "TextChannel", FakeChannel)
    caplog.set_level(logging.INFO)
    current = DiscordReleaseSmoke(
        clients=clients,
        config=config,
        tokens=tokens,
        api_factory=api_factory,
    )

    await current.run()

    assert caplog.text.count("identity_ready") == 4
    assert "smoke_passed" in caplog.text
    for private_value in (*tokens.values(), "101", "201", "301", "302", "303", "304"):
        assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_smoke_ready_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    config = runtime()
    tokens = {slot: f"token-{slot.value}" for slot in DiscordBotSlot}
    clients = {
        slot: cast(discord.Client, FakeClient(config.application_id_for(slot), ready=False))
        for slot in DiscordBotSlot
    }
    monkeypatch.setattr(smoke_module, "READY_TIMEOUT_SECONDS", 0.01)
    current = DiscordReleaseSmoke(
        clients=clients,
        config=config,
        tokens=tokens,
        api_factory=cast(Callable[[str], DiscordBootstrapApi], None),
    )

    with pytest.raises(DiscordReleaseSmokeError) as caught:
        await current.run()

    assert caught.value.category == "ready_timeout"


def test_smoke_environment_requires_exactly_five_secrets_and_local_hash() -> None:
    config, tokens = load_smoke_environment(environment())
    assert config == runtime()
    assert set(tokens) == set(DiscordBotSlot)

    missing = environment()
    missing.pop("DISCORD_TOKEN_PARTICIPANT_C")
    with pytest.raises(StartupConfigurationError):
        load_smoke_environment(missing)

    mismatch = environment()
    mismatch["SHITTIM_EXPECTED_COMMAND_SCHEMA_HASH"] = "0" * 64
    with pytest.raises(StartupConfigurationError):
        load_smoke_environment(mismatch)
