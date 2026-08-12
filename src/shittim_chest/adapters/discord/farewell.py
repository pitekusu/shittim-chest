"""Best-effort direct text-channel delivery for one idle farewell."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import discord

from shittim_chest.application.discord import (
    DISCORD_BOT_SLOTS,
    DiscordBotSlot,
    DiscordRuntimeConfig,
)
from shittim_chest.domain import ParticipantSlot

DEFAULT_FAREWELL_DELIVERY_TIMEOUT_SECONDS = 20.0


class FarewellDeliveryError(RuntimeError):
    """Stable, content-free omission reason for direct farewell delivery."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DiscordFarewellSender:
    """Send at most one non-durable message through the selected participant Bot."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        config: DiscordRuntimeConfig,
        timeout_seconds: float = DEFAULT_FAREWELL_DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        if set(clients) != set(DISCORD_BOT_SLOTS):
            raise ValueError("farewell sender requires every Discord Bot client")
        if timeout_seconds <= 0:
            raise ValueError("farewell delivery timeout must be positive")
        self._clients = dict(clients)
        self._config = config
        self._timeout_seconds = timeout_seconds

    async def send(
        self,
        *,
        participant: ParticipantSlot,
        content: str,
        nonce: str,
    ) -> None:
        """Post once and classify whether the coordinator may retry this nonce."""

        client = self._clients[DiscordBotSlot(participant.value)]
        try:
            async with asyncio.timeout(self._timeout_seconds):
                channel = await self._resolve_channel(client)
                message = await channel.send(
                    content,
                    nonce=nonce,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
                self._verify_message(client, message, content=content, nonce=nonce)
        except TimeoutError as error:
            raise FarewellDeliveryError("farewell_delivery_timeout") from error
        except FarewellDeliveryError:
            raise
        except discord.Forbidden as error:
            raise FarewellDeliveryError("farewell_permission_denied") from error
        except discord.NotFound as error:
            raise FarewellDeliveryError("farewell_channel_unavailable") from error
        except discord.HTTPException as error:
            if error.status != 429 and error.status < 500:
                raise FarewellDeliveryError("farewell_delivery_rejected") from error
            raise FarewellDeliveryError("farewell_discord_unavailable") from error
        except OSError as error:
            raise FarewellDeliveryError("farewell_discord_unavailable") from error

    async def _resolve_channel(self, client: discord.Client) -> discord.TextChannel:
        user = client.user
        if user is None or not client.is_ready():
            raise FarewellDeliveryError("farewell_identity_unavailable")
        channel_id = int(self._config.farewell_channel_id)
        channel = client.get_channel(channel_id)
        if channel is None:
            channel = await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise FarewellDeliveryError("farewell_channel_not_text")
        if (
            str(channel.guild.id) != self._config.guild_id
            or str(channel.id) not in self._config.allowed_channel_ids
        ):
            raise FarewellDeliveryError("farewell_channel_outside_boundary")
        member = channel.guild.me
        if member is None or member.id != user.id:
            raise FarewellDeliveryError("farewell_identity_mismatch")
        permissions = channel.permissions_for(member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.read_message_history
        ):
            raise FarewellDeliveryError("farewell_permission_denied")
        return channel

    def _verify_message(
        self,
        client: discord.Client,
        message: discord.Message,
        *,
        content: str,
        nonce: str,
    ) -> None:
        user = client.user
        if user is None:
            raise FarewellDeliveryError("farewell_identity_unavailable")
        if (
            str(message.channel.id) != self._config.farewell_channel_id
            or message.author.id != user.id
            or message.content != content
            or str(message.nonce) != nonce
        ):
            raise FarewellDeliveryError("farewell_acknowledgement_mismatch")


__all__ = ("DiscordFarewellSender", "FarewellDeliveryError")
