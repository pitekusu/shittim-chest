from __future__ import annotations

import json
from typing import cast

import httpx
import pytest

from shittim_chest.adapters.discord.bootstrap_api import (
    DiscordBootstrapApi,
    DiscordBootstrapError,
    DiscordBootstrapFailure,
    DiscordBootstrapService,
)
from shittim_chest.adapters.discord.command_schema import (
    CommandInventoryState,
    canonical_command_payload,
)
from shittim_chest.application import (
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)

APPLICATION_ID = "301"
GUILD_ID = "101"
CHANNEL_ID = "201"
PUBLIC_KEY = "a" * 64
ENDPOINT = "https://example.invalid/interactions"


def runtime() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=tuple(
            DiscordIdentityConfig(slot=slot, application_id=str(301 + index))
            for index, slot in enumerate(DiscordBotSlot)
        ),
        schema_version="1",
    )


class DiscordServer:
    def __init__(self) -> None:
        self.endpoint: str | None = None
        self.guild_commands: list[dict[str, object]] = []
        self.global_commands: list[dict[str, object]] = []
        self.writes: list[str] = []
        self.status_override: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bot token-placeholder":
            return httpx.Response(401, json={})
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"unsafe": "provider body"})
        path = request.url.path.removeprefix("/api/v10")
        if path == "/users/@me" and request.method == "GET":
            return httpx.Response(200, json={"id": APPLICATION_ID})
        if path == "/applications/@me" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": APPLICATION_ID,
                    "verify_key": PUBLIC_KEY,
                    "interactions_endpoint_url": self.endpoint,
                },
            )
        if path == "/applications/@me" and request.method == "PATCH":
            self.writes.append("endpoint")
            value = json.loads(request.content)
            self.endpoint = value["interactions_endpoint_url"]
            return httpx.Response(200, json={"interactions_endpoint_url": self.endpoint})
        if path == f"/guilds/{GUILD_ID}" and request.method == "GET":
            return httpx.Response(200, json={"id": GUILD_ID})
        if path == f"/channels/{CHANNEL_ID}" and request.method == "GET":
            return httpx.Response(200, json={"id": CHANNEL_ID, "guild_id": GUILD_ID})
        if path == f"/applications/{APPLICATION_ID}/commands" and request.method == "GET":
            return httpx.Response(200, json=self.global_commands)
        guild_path = f"/applications/{APPLICATION_ID}/guilds/{GUILD_ID}/commands"
        if path == guild_path and request.method == "GET":
            return httpx.Response(200, json=self.guild_commands)
        if path == guild_path and request.method == "PUT":
            self.writes.append("command")
            self.guild_commands = json.loads(request.content)
            return httpx.Response(200, json=self.guild_commands)
        raise AssertionError((request.method, path))


def service(server: DiscordServer) -> DiscordBootstrapService:
    auth_value = "token-placeholder"
    client = httpx.Client(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(server),
    )
    return DiscordBootstrapService(
        api=DiscordBootstrapApi(client=client, bot_token=auth_value),
        runtime=runtime(),
        expected_public_key=PUBLIC_KEY,
        expected_endpoint=ENDPOINT,
    )


def test_endpoint_and_command_reconcile_only_when_different() -> None:
    server = DiscordServer()
    current = service(server)

    inspection = current.inspect()
    assert not inspection.endpoint_matches
    assert inspection.command_state is CommandInventoryState.EMPTY
    assert current.reconcile_endpoint()
    assert current.assess_command() is CommandInventoryState.EMPTY
    assert current.reconcile_command()
    assert current.verify().command_state is CommandInventoryState.MATCH
    assert server.writes == ["endpoint", "command"]

    assert not current.reconcile_endpoint()
    assert not current.reconcile_command()
    assert server.writes == ["endpoint", "command"]


def test_unknown_inventory_fails_without_any_write() -> None:
    server = DiscordServer()
    server.guild_commands = [{"name": "unknown", "type": 1}]

    with pytest.raises(DiscordBootstrapError) as caught:
        service(server).inspect()

    assert caught.value.category is DiscordBootstrapFailure.UNSAFE_INVENTORY
    assert server.writes == []


@pytest.mark.parametrize(
    ("status", "category"),
    (
        (401, DiscordBootstrapFailure.AUTHENTICATION),
        (403, DiscordBootstrapFailure.FORBIDDEN),
        (404, DiscordBootstrapFailure.NOT_FOUND),
        (429, DiscordBootstrapFailure.RATE_LIMITED),
        (500, DiscordBootstrapFailure.SERVER),
    ),
)
def test_provider_errors_are_content_free(
    status: int,
    category: DiscordBootstrapFailure,
) -> None:
    server = DiscordServer()
    server.status_override = status

    with pytest.raises(DiscordBootstrapError) as caught:
        service(server).inspect()

    assert caught.value.category is category
    assert "provider body" not in str(caught.value)


def test_generated_fields_are_tolerated_but_semantic_unknowns_fail() -> None:
    server = DiscordServer()
    command = canonical_command_payload()
    command.update({"id": "1", "version": "2"})
    server.guild_commands = [command]
    assert service(server).inspect().command_state is CommandInventoryState.MATCH

    options = command["options"]
    assert isinstance(options, list)
    option = cast(dict[str, object], options[0])
    option["choices"] = []
    with pytest.raises(DiscordBootstrapError):
        service(server).inspect()
