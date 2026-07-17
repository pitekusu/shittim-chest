"""Small production implementations of SDK-independent application Protocols."""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime

from shittim_chest.adapters.openai import OpenAIFailureRecord, OpenAIUsageRecord
from shittim_chest.application.models import MetricEvent
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    FinalProposal,
    ParticipantSlot,
)


class SystemClock:
    """Return timezone-aware UTC wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid7IdGenerator:
    """Create independent UUIDv7 debate and attempt identifiers."""

    def new_debate_id(self) -> DebateId:
        return DebateId.new()

    def new_attempt_id(self) -> AttemptId:
        return AttemptId.new()


class SecureCandidateOrderer:
    """Randomize anonymous candidate order with the operating system RNG."""

    def __init__(self, random_source: random.Random | None = None) -> None:
        self._random = random_source or random.SystemRandom()

    def order_candidates(
        self,
        *,
        voter: ParticipantSlot,
        candidates: tuple[FinalProposal, ...],
    ) -> tuple[FinalProposal, ...]:
        del voter
        return tuple(self._random.sample(candidates, k=len(candidates)))


class ContentFreeTelemetry:
    """Emit JSON metadata without prompts, generated text, or credential values."""

    def __init__(self, *, logger: logging.Logger, environment: str) -> None:
        self._logger = logger
        self._environment = environment

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None:
        self._emit(event.value, debate_id=str(debate_id))

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self._emit(
            "openai_request_completed",
            operation=record.operation,
            response_id=record.response_id,
            model=record.model,
            policy_id=record.policy_id,
            reasoning_mode=record.reasoning_mode,
            latency_ms=record.latency_ms,
            input_tokens=record.input_tokens,
            output_tokens=record.output_tokens,
            cached_input_tokens=record.cached_input_tokens,
            reasoning_tokens=record.reasoning_tokens,
        )

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self._emit(
            "openai_request_failed",
            operation=record.operation,
            code=record.code,
            policy_id=record.policy_id,
            latency_ms=record.latency_ms,
        )

    def runtime_event(self, event: str, **fields: str | int) -> None:
        self._emit(event, **fields)

    def _emit(self, event: str, **fields: str | int) -> None:
        payload: dict[str, str | int] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "severity": "INFO",
            "service": "shittim-chest",
            "environment": self._environment,
            "event": event,
            **fields,
        }
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def lease_owner_id() -> str:
    """Create a unique owner for this process without relying on mutable host state."""

    return f"runtime-{AttemptId.new()}"
