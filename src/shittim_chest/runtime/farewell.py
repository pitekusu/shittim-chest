"""Coordinate one process-memory-only farewell for an eligible IDLE stop."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import Protocol

from shittim_chest.application.farewell import (
    FAREWELL_GENERATION_LEAD,
    FarewellCandidate,
    FarewellTimeContext,
    farewell_nonce,
    farewell_time_context,
)
from shittim_chest.application.ports import Clock
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

DEFAULT_FAREWELL_STATE_POLL_SECONDS = 60.0


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
    ) -> None: ...


class _RuntimeTelemetry(Protocol):
    def runtime_event(self, event: str, **fields: str | int) -> None: ...


class IdleFarewellCoordinator:
    """Pre-generate at 28 minutes and conditionally deliver during shutdown."""

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
        self._candidate: FarewellCandidate | None = None

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
        """Inspect once and generate at most once for one exact IDLE period."""

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
            self._candidate = None
            return self._poll_seconds
        stop_eligible_at = state.stop_eligible_at
        if stop_eligible_at is None:
            self._candidate = None
            return self._poll_seconds
        identity = (state.generation, stop_eligible_at)
        if (
            self._candidate is not None
            and (
                self._candidate.generation,
                self._candidate.stop_eligible_at,
            )
            != identity
        ):
            self._candidate = None
        now = self._clock.now()
        generate_at = stop_eligible_at - FAREWELL_GENERATION_LEAD
        if now < generate_at:
            return max(0.001, min(self._poll_seconds, (generate_at - now).total_seconds()))
        if self._attempted_identity == identity:
            return self._poll_seconds
        self._attempted_identity = identity
        participant = self._random.choice(PARTICIPANTS)
        try:
            content = await self._generator.generate(
                participant=participant,
                time_context=farewell_time_context(now),
            )
            current = await self._runtime_state.get()
            if (
                current is None
                or current.status is not RuntimeStatus.IDLE
                or current.generation != state.generation
                or current.stop_eligible_at != stop_eligible_at
                or self._clock.now() >= stop_eligible_at
            ):
                self._candidate = None
                self._telemetry.runtime_event(
                    "farewell_generation_discarded",
                    reason="farewell_idle_period_changed",
                )
                return self._poll_seconds
            self._candidate = FarewellCandidate(
                generation=state.generation,
                stop_eligible_at=stop_eligible_at,
                participant=participant,
                content=content,
                nonce=farewell_nonce(
                    generation=state.generation,
                    stop_eligible_at=stop_eligible_at,
                    participant=participant,
                ),
            )
            self._telemetry.runtime_event(
                "farewell_generation_completed",
                participant_slot=participant.value,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._candidate = None
            self._telemetry.runtime_event(
                "farewell_generation_omitted",
                reason=_error_code(error, fallback="farewell_generation_failed"),
            )
        return self._poll_seconds

    async def deliver_before_shutdown(self) -> None:
        """Consume the candidate once and send only for its normal STOPPING fence."""

        candidate = self._candidate
        self._candidate = None
        if candidate is None:
            self._telemetry.runtime_event(
                "farewell_delivery_omitted",
                reason="farewell_candidate_unavailable",
            )
            return
        try:
            state = await self._runtime_state.get()
            if not _eligible_for_delivery(state, candidate, now=self._clock.now()):
                self._telemetry.runtime_event(
                    "farewell_delivery_omitted",
                    reason="farewell_stop_not_eligible",
                )
                return
            await self._sender.send(
                participant=candidate.participant,
                content=candidate.content,
                nonce=candidate.nonce,
            )
            self._telemetry.runtime_event(
                "farewell_delivery_completed",
                participant_slot=candidate.participant.value,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._telemetry.runtime_event(
                "farewell_delivery_omitted",
                reason=_error_code(error, fallback="farewell_delivery_failed"),
            )


def _eligible_for_delivery(
    state: RuntimeState | None,
    candidate: FarewellCandidate,
    *,
    now: datetime,
) -> bool:
    return bool(
        state is not None
        and state.status is RuntimeStatus.STOPPING
        and state.generation == candidate.generation
        and state.stopping_at is not None
        and state.stopping_at >= candidate.stop_eligible_at
        and now >= candidate.stop_eligible_at
    )


def _error_code(error: Exception, *, fallback: str) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code) is not None:
        return code
    return fallback


__all__ = ("DEFAULT_FAREWELL_STATE_POLL_SECONDS", "IdleFarewellCoordinator")
