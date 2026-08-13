"""Generate and deliver one best-effort farewell during an eligible IDLE period."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import Protocol

from shittim_chest.application.farewell import (
    FAREWELL_GENERATION_LEAD,
    FarewellTimeContext,
    farewell_nonce,
    farewell_time_context,
)
from shittim_chest.application.ports import Clock
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

DEFAULT_FAREWELL_STATE_POLL_SECONDS = 60.0
_DELIVERY_ATTEMPTS = 2
_DELIVERY_TOTAL_TIMEOUT_SECONDS = 60.0
_RETRYABLE_DELIVERY_CODES = frozenset(
    {
        "farewell_delivery_timeout",
        "farewell_discord_unavailable",
    }
)


class _RuntimeStateReader(Protocol):
    async def get(self) -> RuntimeState | None: ...


class _FarewellGenerator(Protocol):
    async def generate(
        self,
        *,
        participant: ParticipantSlot,
        time_context: FarewellTimeContext,
    ) -> str: ...


class _FarewellSender(Protocol):
    async def send(
        self,
        *,
        participant: ParticipantSlot,
        content: str,
        nonce: str,
        reconcile: bool = False,
    ) -> None: ...


class _RuntimeTelemetry(Protocol):
    def runtime_event(self, event: str, **fields: str | int) -> None: ...


class IdleFarewellCoordinator:
    """Send around 25 IDLE minutes while the selected Bot is still running."""

    def __init__(
        self,
        *,
        clock: Clock,
        runtime_state: _RuntimeStateReader,
        generator: _FarewellGenerator,
        sender: _FarewellSender,
        telemetry: _RuntimeTelemetry,
        random_source: random.Random | None = None,
        poll_seconds: float = DEFAULT_FAREWELL_STATE_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("farewell state poll interval must be positive")
        self._clock = clock
        self._runtime_state = runtime_state
        self._generator = generator
        self._sender = sender
        self._telemetry = telemetry
        self._random = random_source or random.SystemRandom()
        self._poll_seconds = poll_seconds
        self._attempted_identity: tuple[int, datetime] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Watch the persisted IDLE deadline without busy polling."""

        while not stop.is_set():
            delay = await self.prepare_once()
            try:
                async with asyncio.timeout(delay):
                    await stop.wait()
            except TimeoutError:
                continue

    async def prepare_once(self) -> float:
        """Generate and send at most once for one exact IDLE identity."""

        try:
            state = await self._runtime_state.get()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._telemetry.runtime_event(
                "farewell_generation_omitted",
                reason=_error_code(error, fallback="farewell_state_unavailable"),
            )
            return self._poll_seconds
        if state is None or state.status is not RuntimeStatus.IDLE:
            return self._poll_seconds
        stop_eligible_at = state.stop_eligible_at
        if stop_eligible_at is None:
            return self._poll_seconds
        identity = (state.generation, stop_eligible_at)
        now = self._clock.now()
        generate_at = stop_eligible_at - FAREWELL_GENERATION_LEAD
        if now < generate_at:
            return max(0.001, min(self._poll_seconds, (generate_at - now).total_seconds()))
        if now >= stop_eligible_at or self._attempted_identity == identity:
            return self._poll_seconds
        self._attempted_identity = identity
        participant = self._random.choice(PARTICIPANTS)
        try:
            content = await self._generator.generate(
                participant=participant,
                time_context=farewell_time_context(now),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._telemetry.runtime_event(
                "farewell_generation_omitted",
                reason=_error_code(error, fallback="farewell_generation_failed"),
            )
            return self._poll_seconds
        self._telemetry.runtime_event("farewell_generation_completed")
        nonce = farewell_nonce(
            generation=state.generation,
            stop_eligible_at=stop_eligible_at,
            participant=participant,
        )
        await self._deliver(
            identity=identity,
            participant=participant,
            content=content,
            nonce=nonce,
        )
        return self._poll_seconds

    async def _deliver(
        self,
        *,
        identity: tuple[int, datetime],
        participant: ParticipantSlot,
        content: str,
        nonce: str,
    ) -> None:
        attempts = 0
        try:
            async with asyncio.timeout(_DELIVERY_TOTAL_TIMEOUT_SECONDS):
                for attempts in range(1, _DELIVERY_ATTEMPTS + 1):
                    if not await self._same_idle(identity):
                        self._telemetry.runtime_event(
                            "farewell_generation_discarded",
                            reason="farewell_idle_period_changed",
                            delivery_attempt_count=attempts - 1,
                        )
                        return
                    try:
                        await self._sender.send(
                            participant=participant,
                            content=content,
                            nonce=nonce,
                            reconcile=attempts > 1,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        reason = _error_code(error, fallback="farewell_delivery_failed")
                        if attempts == 1 and reason in _RETRYABLE_DELIVERY_CODES:
                            continue
                        self._telemetry.runtime_event(
                            "farewell_delivery_omitted",
                            reason=reason,
                            delivery_attempt_count=attempts,
                        )
                        return
                    self._telemetry.runtime_event(
                        "farewell_delivery_completed",
                        delivery_attempt_count=attempts,
                    )
                    return
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._telemetry.runtime_event(
                "farewell_delivery_omitted",
                reason="farewell_delivery_total_timeout",
                delivery_attempt_count=attempts,
            )
        except Exception as error:
            self._telemetry.runtime_event(
                "farewell_delivery_omitted",
                reason=_error_code(error, fallback="farewell_state_unavailable"),
                delivery_attempt_count=attempts,
            )

    async def _same_idle(self, identity: tuple[int, datetime]) -> bool:
        state = await self._runtime_state.get()
        return bool(
            state is not None
            and state.status is RuntimeStatus.IDLE
            and (state.generation, state.stop_eligible_at) == identity
            and self._clock.now() < identity[1]
        )


def _error_code(error: Exception, *, fallback: str) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) is not None:
        return code
    return fallback


__all__ = ("DEFAULT_FAREWELL_STATE_POLL_SECONDS", "IdleFarewellCoordinator")
