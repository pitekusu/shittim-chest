"""Owned recovery loop for persisted Discord outbox operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from shittim_chest.adapters.discord.errors import DiscordAdapterError
from shittim_chest.application.discord import OutboxOperation
from shittim_chest.application.errors import OutboxRecoveryFailed
from shittim_chest.application.models import DebateSnapshot, MetricEvent
from shittim_chest.application.ports import (
    Clock,
    DiscordOutboxRepository,
    DiscordPublisher,
    Metrics,
)

DEFAULT_OUTBOX_POLL_SECONDS = 1.0
_EXPIRY_EPSILON = timedelta(microseconds=1)

_Sleep = Callable[[float], Awaitable[None]]


class DiscordOutboxRecovery:
    """Drain one leased attempt's pending outbox in persisted chunk order."""

    def __init__(
        self,
        *,
        outbox: DiscordOutboxRepository,
        publisher: DiscordPublisher,
        clock: Clock,
        metrics: Metrics,
        sleep: _Sleep = asyncio.sleep,
        poll_seconds: float = DEFAULT_OUTBOX_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("outbox poll interval must be positive")
        self._outbox = outbox
        self._publisher = publisher
        self._clock = clock
        self._metrics = metrics
        self._sleep = sleep
        self._poll_seconds = poll_seconds

    async def drain(self, *, expected: DebateSnapshot) -> None:
        """Recover every unsent operation, preserving persisted retry scheduling."""

        while True:
            pending = await self._outbox.list_pending(
                debate_id=expected.state.debate_id,
                attempt_id=expected.state.attempt_id,
            )
            if not pending:
                return

            operation = pending[0]
            delay = _availability_delay(operation, self._clock.now())
            if delay > 0:
                await self._sleep(delay)
                continue

            try:
                sent = await self._publisher.publish_persisted(
                    expected=expected,
                    operation_id=operation.operation_id,
                )
            except asyncio.CancelledError:
                raise
            except DiscordAdapterError as error:
                if not error.retryable:
                    raise OutboxRecoveryFailed(error.code) from error
                self._metrics.increment(
                    MetricEvent.OUTBOX_RETRY_SCHEDULED,
                    debate_id=expected.state.debate_id,
                )
                continue

            if sent is None:
                await self._sleep(self._poll_seconds)
                continue
            self._metrics.increment(
                MetricEvent.OUTBOX_RECOVERED,
                debate_id=expected.state.debate_id,
            )


def _availability_delay(operation: OutboxOperation, now: datetime) -> float:
    next_retry_at = operation.next_retry_at
    claim_expires_at = operation.claim_expires_at
    available_at = next_retry_at
    if claim_expires_at is not None:
        claim_available_at = claim_expires_at + _EXPIRY_EPSILON
        if available_at is None or claim_available_at > available_at:
            available_at = claim_available_at
    if available_at is None or available_at <= now:
        return 0.0
    return (available_at - now).total_seconds()
