"""Content-free OpenAI usage and failure telemetry contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OpenAIUsageRecord:
    """Low-cardinality metadata recorded after one successful API response."""

    operation: str
    response_id: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int


@dataclass(frozen=True, slots=True)
class OpenAIFailureRecord:
    """Content-free failure telemetry emitted after SDK retries are exhausted."""

    operation: str
    code: str
    latency_ms: int


class OpenAIUsageRecorder(Protocol):
    """Persist provider metadata without prompts or generated content."""

    def record_usage(self, record: OpenAIUsageRecord) -> None: ...

    def record_failure(self, record: OpenAIFailureRecord) -> None: ...


class NullOpenAIUsageRecorder:
    """Default recorder used until the CloudWatch adapter is implemented."""

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        del record

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        del record
