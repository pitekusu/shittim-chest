"""Publish fenced DynamoDB outbox operations through discord.py."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from datetime import timedelta

import discord

from shittim_chest.adapters.discord.errors import (
    DiscordAdapterError,
    DiscordDeliveryConflict,
    DiscordDeliveryRejected,
    DiscordIdentityUnavailable,
    DiscordOutboxNotFound,
    DiscordPermissionDenied,
    DiscordRateLimited,
    DiscordThreadLocked,
    DiscordThreadUnavailable,
    DiscordUnavailable,
)
from shittim_chest.application.discord import (
    DISCORD_BOT_SLOTS,
    OUTBOX_CLAIM_SECONDS,
    DiscordBotSlot,
    OutboxOperation,
    OutboxStatus,
    content_sha256,
)
from shittim_chest.application.models import DebateSnapshot
from shittim_chest.application.ports import Clock, DiscordOutboxRepository

DEFAULT_HISTORY_LIMIT = 500
DEFAULT_RETRY_DELAY_SECONDS = 30.0
DISCORD_MAX_RATELIMIT_TIMEOUT_SECONDS = 30.0
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 45.0


class DiscordPyPublisher:
    """Claim, reconcile, publish, and complete persisted Discord message chunks."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        outbox: DiscordOutboxRepository,
        clock: Clock,
        claim_owner: str,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        delivery_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    ) -> None:
        if set(clients) != set(DISCORD_BOT_SLOTS):
            raise ValueError("publisher requires exactly one client for each Discord Bot slot")
        if len({id(client) for client in clients.values()}) != len(DISCORD_BOT_SLOTS):
            raise ValueError("publisher Discord clients must be distinct")
        if any(
            client.http.max_ratelimit_timeout != DISCORD_MAX_RATELIMIT_TIMEOUT_SECONDS
            for client in clients.values()
        ):
            raise ValueError("publisher clients must set max_ratelimit_timeout to 30 seconds")
        if not claim_owner.strip():
            raise ValueError("claim owner must not be empty")
        if isinstance(history_limit, bool) or not isinstance(history_limit, int):
            raise TypeError("history limit must be an integer")
        if history_limit < 1:
            raise ValueError("history limit must be positive")
        if retry_delay_seconds <= 0:
            raise ValueError("retry delay must be positive")
        if not 0 < delivery_timeout_seconds < OUTBOX_CLAIM_SECONDS:
            raise ValueError("delivery timeout must be positive and shorter than the outbox claim")
        self._clients = dict(clients)
        self._outbox = outbox
        self._clock = clock
        self._claim_owner = claim_owner
        self._history_limit = history_limit
        self._retry_delay_seconds = retry_delay_seconds
        self._delivery_timeout_seconds = delivery_timeout_seconds

    async def publish_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None:
        """Publish one persisted operation, or return None when it is not claimable yet."""

        current = await self._outbox.get(
            debate_id=expected.state.debate_id,
            attempt_id=expected.state.attempt_id,
            operation_id=operation_id,
        )
        if current is None:
            raise DiscordOutboxNotFound
        if current.status is OutboxStatus.SENT:
            return current

        claimed = await self._outbox.claim(
            expected=expected,
            operation_id=operation_id,
            claim_owner=self._claim_owner,
            at=self._clock.now(),
        )
        if claimed is None:
            return None

        try:
            async with asyncio.timeout(self._delivery_timeout_seconds):
                client = self._ready_client(claimed.bot_slot)
                thread = await self._resolve_thread(client, expected, claimed)
                if thread.locked:
                    raise DiscordThreadLocked

                if claimed.delivery_attempt > 1:
                    reconciled = await self._find_existing(client, thread, claimed)
                    if reconciled is not None:
                        message_id = reconciled.id
                    else:
                        message_id = await self._send(client, thread, claimed)
                else:
                    message_id = await self._send(client, thread, claimed)
            return await self._complete(expected, claimed, message_id)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            await self._reschedule(expected, claimed, self._retry_delay_seconds)
            raise DiscordUnavailable from error
        except DiscordAdapterError:
            raise
        except discord.RateLimited as error:
            await self._reschedule(expected, claimed, max(1.0, error.retry_after))
            raise DiscordRateLimited from error
        except discord.Forbidden as error:
            raise DiscordPermissionDenied from error
        except discord.NotFound as error:
            raise DiscordThreadUnavailable from error
        except discord.HTTPException as error:
            mapped = self._map_http_error(error)
            if mapped.retryable:
                await self._reschedule(expected, claimed, self._retry_after(error))
            raise mapped from error
        except OSError as error:
            await self._reschedule(expected, claimed, self._retry_delay_seconds)
            raise DiscordUnavailable from error

    def _ready_client(self, slot: DiscordBotSlot) -> discord.Client:
        client = self._clients[slot]
        if client.user is None or not client.is_ready():
            raise DiscordIdentityUnavailable
        return client

    async def _resolve_thread(
        self,
        client: discord.Client,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> discord.Thread:
        channel = client.get_channel(int(operation.thread_id))
        if channel is None:
            channel = await client.fetch_channel(int(operation.thread_id))
        if not isinstance(channel, discord.Thread):
            raise DiscordThreadUnavailable
        if str(channel.guild.id) != expected.guild_id:
            raise DiscordThreadUnavailable
        return channel

    async def _find_existing(
        self,
        client: discord.Client,
        thread: discord.Thread,
        operation: OutboxOperation,
    ) -> discord.Message | None:
        user = client.user
        if user is None:
            raise DiscordIdentityUnavailable
        matched: discord.Message | None = None
        async for message in thread.history(
            limit=self._history_limit,
            after=operation.created_at,
            oldest_first=True,
        ):
            if message.author.id != user.id or str(message.nonce) != operation.nonce:
                continue
            if message.content != operation.content:
                raise DiscordDeliveryConflict
            if content_sha256(message.content) != operation.content_hash:
                raise DiscordDeliveryConflict
            if matched is None or message.id < matched.id:
                matched = message
        return matched

    def _validate_message(
        self,
        client: discord.Client,
        message: discord.Message,
        operation: OutboxOperation,
    ) -> None:
        user = client.user
        if user is None:
            raise DiscordIdentityUnavailable
        if (
            str(message.channel.id) != operation.thread_id
            or message.author.id != user.id
            or str(message.nonce) != operation.nonce
            or message.content != operation.content
            or content_sha256(message.content) != operation.content_hash
        ):
            raise DiscordDeliveryConflict

    async def _send(
        self,
        client: discord.Client,
        thread: discord.Thread,
        operation: OutboxOperation,
    ) -> int:
        message = await thread.send(
            operation.content,
            nonce=operation.nonce,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._validate_message(client, message, operation)
        return message.id

    async def _complete(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        message_id: int,
    ) -> OutboxOperation:
        return await self._outbox.mark_sent(
            expected=expected,
            operation_id=operation.operation_id,
            claim_owner=self._claim_owner,
            message_id=str(message_id),
            at=self._clock.now(),
        )

    async def _reschedule(
        self,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        delay_seconds: float,
    ) -> None:
        at = self._clock.now()
        await self._outbox.reschedule(
            expected=expected,
            operation_id=operation.operation_id,
            claim_owner=self._claim_owner,
            at=at,
            next_retry_at=at + timedelta(seconds=delay_seconds),
        )

    def _map_http_error(self, error: discord.HTTPException) -> DiscordAdapterError:
        if error.status == 429:
            return DiscordRateLimited()
        if error.status in {408, 409} or error.status >= 500:
            return DiscordUnavailable()
        return DiscordDeliveryRejected()

    def _retry_after(self, error: discord.HTTPException) -> float:
        raw = error.response.headers.get("Retry-After")
        if raw is None:
            return self._retry_delay_seconds
        try:
            parsed = float(raw)
        except TypeError, ValueError:
            return self._retry_delay_seconds
        if not math.isfinite(parsed) or parsed <= 0:
            return self._retry_delay_seconds
        return max(1.0, parsed)
