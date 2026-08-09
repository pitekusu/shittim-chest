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
    policy_id: str
    reasoning_mode: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    web_search_source_count: int | None = None
    web_search_source_rejected_count: int | None = None
    web_search_source_rejected_kinds: str | None = None
    url_citation_count: int | None = None
    evidence_source_count: int | None = None
    title_fallback_count: int | None = None
    title_fallback_kinds: str | None = None


@dataclass(frozen=True, slots=True)
class OpenAIFailureRecord:
    """Content-free failure telemetry emitted after SDK retries are exhausted."""

    operation: str
    code: str
    policy_id: str
    latency_ms: int
    diagnostic_context: str | None = None
    diagnostic_kind: str | None = None


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
