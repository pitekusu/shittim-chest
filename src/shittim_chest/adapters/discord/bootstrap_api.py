"""Narrow Discord REST boundary for release-time bootstrap and read-only smoke."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import cast

import httpx

from shittim_chest.application import DiscordBotSlot, DiscordRuntimeConfig

from .command_schema import (
    CommandInventoryState,
    CommandSchemaError,
    canonical_command_payload,
    classify_command_inventory,
)

DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_BOOTSTRAP_TIMEOUT_SECONDS = 10.0


@unique
class DiscordBootstrapFailure(StrEnum):
    """Stable, content-free provider failure categories."""

    AUTHENTICATION = "authentication_failed"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    SERVER = "server_error"
    TIMEOUT = "timeout"
    TRANSPORT = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSAFE_INVENTORY = "unsafe_inventory"
    POSTCONDITION = "postcondition_failed"


class DiscordBootstrapError(RuntimeError):
    """A Discord bootstrap operation failed without retaining provider content."""

    __slots__ = ("category",)

    def __init__(self, category: DiscordBootstrapFailure) -> None:
        self.category = category
        super().__init__(category.value)


@dataclass(frozen=True, slots=True)
class DiscordBootstrapInspection:
    """Content-free state needed to make a release bootstrap decision."""

    endpoint_sha256: str | None
    endpoint_matches: bool
    global_command_count: int
    guild_command_count: int
    command_state: CommandInventoryState
    channel_count: int


class DiscordBootstrapApi:
    """Call only the Discord endpoints approved for bootstrap."""

    __slots__ = ("_client",)

    def __init__(self, *, client: httpx.Client, bot_token: str) -> None:
        if not bot_token.strip():
            raise ValueError("Bot token must not be empty")
        self._client = client
        self._client.headers["Authorization"] = f"Bot {bot_token}"

    def get_current_user(self) -> Mapping[str, object]:
        return self._object("GET", "/users/@me")

    def get_current_application(self) -> Mapping[str, object]:
        return self._object("GET", "/applications/@me")

    def get_guild(self, guild_id: str) -> Mapping[str, object]:
        return self._object("GET", f"/guilds/{guild_id}")

    def get_channel(self, channel_id: str) -> Mapping[str, object]:
        return self._object("GET", f"/channels/{channel_id}")

    def get_global_commands(self, application_id: str) -> tuple[Mapping[str, object], ...]:
        return self._objects("GET", f"/applications/{application_id}/commands")

    def get_guild_commands(
        self,
        application_id: str,
        guild_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        return self._objects(
            "GET",
            f"/applications/{application_id}/guilds/{guild_id}/commands",
        )

    def edit_interactions_endpoint(self, endpoint_url: str) -> Mapping[str, object]:
        return self._object(
            "PATCH",
            "/applications/@me",
            payload={"interactions_endpoint_url": endpoint_url},
        )

    def bulk_overwrite_guild_commands(
        self,
        application_id: str,
        guild_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        return self._objects(
            "PUT",
            f"/applications/{application_id}/guilds/{guild_id}/commands",
            payload=[canonical_command_payload()],
        )

    def close(self) -> None:
        """Close the owned HTTP transport without exposing authorization state."""

        self._client.close()

    def _object(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
    ) -> Mapping[str, object]:
        value = self._json(method, path, payload=payload)
        if not isinstance(value, Mapping):
            raise DiscordBootstrapError(DiscordBootstrapFailure.INVALID_RESPONSE)
        return cast(Mapping[str, object], value)

    def _objects(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        value = self._json(method, path, payload=payload)
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            raise DiscordBootstrapError(DiscordBootstrapFailure.INVALID_RESPONSE)
        return tuple(cast(Mapping[str, object], item) for item in value)

    def _json(self, method: str, path: str, *, payload: object | None = None) -> object:
        try:
            if payload is None:
                response = self._client.request(method, path)
            else:
                response = self._client.request(method, path, json=payload)
        except httpx.TimeoutException:
            raise DiscordBootstrapError(DiscordBootstrapFailure.TIMEOUT) from None
        except httpx.HTTPError:
            raise DiscordBootstrapError(DiscordBootstrapFailure.TRANSPORT) from None
        if response.status_code == 401:
            raise DiscordBootstrapError(DiscordBootstrapFailure.AUTHENTICATION)
        if response.status_code == 403:
            raise DiscordBootstrapError(DiscordBootstrapFailure.FORBIDDEN)
        if response.status_code == 404:
            raise DiscordBootstrapError(DiscordBootstrapFailure.NOT_FOUND)
        if response.status_code == 429:
            raise DiscordBootstrapError(DiscordBootstrapFailure.RATE_LIMITED)
        if response.status_code >= 500:
            raise DiscordBootstrapError(DiscordBootstrapFailure.SERVER)
        if not 200 <= response.status_code < 300:
            raise DiscordBootstrapError(DiscordBootstrapFailure.INVALID_RESPONSE)
        try:
            return response.json()
        except ValueError:
            raise DiscordBootstrapError(DiscordBootstrapFailure.INVALID_RESPONSE) from None


class DiscordBootstrapService:
    """Apply fail-closed bootstrap decisions over one moderator Application."""

    __slots__ = ("_api", "_endpoint", "_public_key", "_runtime")

    def __init__(
        self,
        *,
        api: DiscordBootstrapApi,
        runtime: DiscordRuntimeConfig,
        expected_public_key: str,
        expected_endpoint: str,
    ) -> None:
        if len(expected_public_key) != 64 or any(
            character not in "0123456789abcdef" for character in expected_public_key
        ):
            raise ValueError("Discord public key must be 64 lowercase hexadecimal characters")
        if not expected_endpoint.startswith("https://"):
            raise ValueError("expected interactions endpoint must use HTTPS")
        self._api = api
        self._runtime = runtime
        self._public_key = expected_public_key
        self._endpoint = expected_endpoint

    def inspect(self) -> DiscordBootstrapInspection:
        application_id = self._runtime.application_id_for(DiscordBotSlot.MODERATOR)
        user = self._api.get_current_user()
        application = self._api.get_current_application()
        if user.get("id") != application_id or application.get("id") != application_id:
            raise DiscordBootstrapError(DiscordBootstrapFailure.IDENTITY_MISMATCH)
        if application.get("verify_key") != self._public_key:
            raise DiscordBootstrapError(DiscordBootstrapFailure.IDENTITY_MISMATCH)
        guild = self._api.get_guild(self._runtime.guild_id)
        if guild.get("id") != self._runtime.guild_id:
            raise DiscordBootstrapError(DiscordBootstrapFailure.IDENTITY_MISMATCH)
        for channel_id in sorted(self._runtime.allowed_channel_ids):
            channel = self._api.get_channel(channel_id)
            if channel.get("id") != channel_id or channel.get("guild_id") != self._runtime.guild_id:
                raise DiscordBootstrapError(DiscordBootstrapFailure.IDENTITY_MISMATCH)
        global_commands = self._api.get_global_commands(application_id)
        guild_commands = self._api.get_guild_commands(application_id, self._runtime.guild_id)
        if global_commands:
            raise DiscordBootstrapError(DiscordBootstrapFailure.UNSAFE_INVENTORY)
        try:
            command_state = classify_command_inventory(guild_commands)
        except CommandSchemaError:
            raise DiscordBootstrapError(DiscordBootstrapFailure.UNSAFE_INVENTORY) from None
        endpoint = application.get("interactions_endpoint_url")
        if endpoint is not None and not isinstance(endpoint, str):
            raise DiscordBootstrapError(DiscordBootstrapFailure.INVALID_RESPONSE)
        return DiscordBootstrapInspection(
            endpoint_sha256=_sha256(endpoint) if endpoint is not None else None,
            endpoint_matches=endpoint == self._endpoint,
            global_command_count=0,
            guild_command_count=len(guild_commands),
            command_state=command_state,
            channel_count=len(self._runtime.allowed_channel_ids),
        )

    def reconcile_endpoint(self) -> bool:
        current = self.inspect()
        if current.endpoint_matches:
            return False
        response = self._api.edit_interactions_endpoint(self._endpoint)
        if response.get("interactions_endpoint_url") != self._endpoint:
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION)
        if not self.inspect().endpoint_matches:
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION)
        return True

    def assess_command(self) -> CommandInventoryState:
        inspection = self.inspect()
        if not inspection.endpoint_matches:
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION)
        return inspection.command_state

    def reconcile_command(self) -> bool:
        state = self.assess_command()
        if state is CommandInventoryState.MATCH:
            return False
        application_id = self._runtime.application_id_for(DiscordBotSlot.MODERATOR)
        response = self._api.bulk_overwrite_guild_commands(
            application_id,
            self._runtime.guild_id,
        )
        try:
            if classify_command_inventory(response) is not CommandInventoryState.MATCH:
                raise CommandSchemaError("bulk overwrite response does not match")
        except CommandSchemaError:
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION) from None
        if self.inspect().command_state is not CommandInventoryState.MATCH:
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION)
        return True

    def verify(self) -> DiscordBootstrapInspection:
        inspection = self.inspect()
        if (
            not inspection.endpoint_matches
            or inspection.command_state is not CommandInventoryState.MATCH
        ):
            raise DiscordBootstrapError(DiscordBootstrapFailure.POSTCONDITION)
        return inspection


def create_discord_bootstrap_http_client() -> httpx.Client:
    """Create a no-retry client with a bounded total Discord API timeout."""

    return httpx.Client(
        base_url=DISCORD_API_BASE_URL,
        timeout=DISCORD_BOOTSTRAP_TIMEOUT_SECONDS,
        headers={"User-Agent": "shittim-chest-discord-bootstrap/1"},
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = (
    "DiscordBootstrapApi",
    "DiscordBootstrapError",
    "DiscordBootstrapFailure",
    "DiscordBootstrapInspection",
    "DiscordBootstrapService",
    "create_discord_bootstrap_http_client",
)
