"""Bounded Responses API generation for the best-effort idle farewell."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urlsplit

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from openai.types.responses.parsed_response import ParsedResponse
from openai.types.responses.response_function_web_search import ResponseFunctionWebSearch
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import AnnotationURLCitation, ResponseOutputText
from pydantic import ValidationError

from shittim_chest.adapters.openai.config import OpenAIAdapterConfig, PersonaPrompts
from shittim_chest.adapters.openai.errors import (
    OpenAIAdapterError,
    OpenAIConfigurationError,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRateLimited,
    OpenAIRefusal,
    OpenAIUnavailable,
)
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
from shittim_chest.adapters.openai.observability import (
    NullOpenAIUsageRecorder,
    OpenAIFailureRecord,
    OpenAIUsageRecord,
    OpenAIUsageRecorder,
)
from shittim_chest.adapters.openai.prompts import farewell_input, farewell_instructions
from shittim_chest.adapters.openai.schemas import FarewellOutputV2
from shittim_chest.application.farewell import FarewellTimeContext, prepare_farewell_content
from shittim_chest.domain import ParticipantSlot

_INITIAL_MAX_OUTPUT_TOKENS = 4_000
_RETRY_MAX_OUTPUT_TOKENS = 8_000
_MAX_APPLICATION_ATTEMPTS = 2
_TOTAL_TIMEOUT_SECONDS = 120.0
_WEATHER_REALTIME_FEED = "oai-weather"
_MAX_CITATION_URL_CHARACTERS = 1_800


@dataclass(frozen=True, slots=True)
class _FarewellEvidence:
    citation_urls: tuple[str, ...]
    source_count: int
    known_feed_count: int


@dataclass(slots=True)
class OpenAIFarewellGenerator:
    """Generate one cited greeting with one bounded application-level retry."""

    client: AsyncOpenAI
    personas: PersonaPrompts
    limiter: OpenAIRequestLimiter
    config: OpenAIAdapterConfig = field(default_factory=OpenAIAdapterConfig)
    recorder: OpenAIUsageRecorder = field(default_factory=NullOpenAIUsageRecorder)

    async def generate(
        self,
        *,
        participant: ParticipantSlot,
        time_context: FarewellTimeContext,
    ) -> str:
        """Return a one-message greeting and first valid citation, or fail boundedly."""

        operation = "farewell_generation"
        started = monotonic()
        responses: list[ParsedResponse[FarewellOutputV2]] = []
        last_response: ParsedResponse[FarewellOutputV2] | None = None
        last_evidence = _FarewellEvidence((), 0, 0)
        attempts = 0
        prior_failure_reason: str | None = None
        last_error: OpenAIAdapterError | None = None

        def record_acquired_usage() -> None:
            if not responses:
                return
            self._record_usage(
                operation,
                tuple(responses),
                last_evidence,
                started,
                attempt_count=attempts,
                prior_failure_reason=prior_failure_reason,
            )

        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                for attempts in range(1, _MAX_APPLICATION_ATTEMPTS + 1):
                    try:
                        response = await self._request(
                            participant=participant,
                            time_context=time_context,
                            max_output_tokens=(
                                _INITIAL_MAX_OUTPUT_TOKENS
                                if attempts == 1
                                else _RETRY_MAX_OUTPUT_TOKENS
                            ),
                        )
                        responses.append(response)
                        last_response = response
                        last_evidence = _inspect_evidence(response)
                        parsed = _extract_parsed(response)
                        _require_completed_web_search(response)
                        if not last_evidence.citation_urls:
                            raise OpenAIInvalidOutput(
                                diagnostic_context="farewell_citation",
                                diagnostic_kind="missing",
                            )
                        content = prepare_farewell_content(
                            parsed.message,
                            last_evidence.citation_urls[0],
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        last_error = _map_error(error)
                        if attempts == 1 and _retryable_farewell_error(last_error):
                            prior_failure_reason = last_error.diagnostic_kind or last_error.code
                            continue
                        break
                    self._record_usage(
                        operation,
                        tuple(responses),
                        last_evidence,
                        started,
                        attempt_count=attempts,
                        prior_failure_reason=prior_failure_reason,
                    )
                    return content
        except asyncio.CancelledError:
            record_acquired_usage()
            raise
        except TimeoutError:
            last_error = OpenAIUnavailable(
                diagnostic_context="farewell_request",
                diagnostic_kind="total_timeout",
            )

        failure = last_error or OpenAIUnavailable(
            diagnostic_context="farewell_request",
            diagnostic_kind="unknown",
        )
        record_acquired_usage()
        self._record_failure(
            operation,
            failure,
            started,
            response=last_response,
            evidence=last_evidence,
            attempt_count=attempts,
        )
        raise failure

    async def _request(
        self,
        *,
        participant: ParticipantSlot,
        time_context: FarewellTimeContext,
        max_output_tokens: int,
    ) -> ParsedResponse[FarewellOutputV2]:
        async with self.limiter.slot():
            return await self.client.responses.parse(
                model=self.config.model,
                instructions=farewell_instructions(self.personas.for_participant(participant)),
                input=farewell_input(
                    local_datetime=time_context.local_datetime,
                    period=time_context.period,
                    season=time_context.season,
                ),
                text_format=FarewellOutputV2,
                include=["web_search_call.action.sources"],
                max_output_tokens=max_output_tokens,
                reasoning={"effort": "medium"},
                store=False,
                tools=[
                    {
                        "type": "web_search",
                        "search_context_size": "medium",
                        "user_location": {
                            "type": "approximate",
                            "country": "JP",
                            "city": "Tokyo",
                            "region": "Tokyo",
                            "timezone": "Asia/Tokyo",
                        },
                    }
                ],
                tool_choice="required",
                max_tool_calls=4,
                parallel_tool_calls=False,
                truncation="disabled",
            )

    def _record_usage(
        self,
        operation: str,
        responses: tuple[ParsedResponse[FarewellOutputV2], ...],
        evidence: _FarewellEvidence,
        started: float,
        *,
        attempt_count: int,
        prior_failure_reason: str | None,
    ) -> None:
        response = responses[-1]
        usages = tuple(candidate.usage for candidate in responses if candidate.usage is not None)
        self.recorder.record_usage(
            OpenAIUsageRecord(
                operation=operation,
                response_id=response.id,
                model=str(response.model),
                policy_id=self.config.policy.policy_id.value,
                reasoning_mode=self.config.policy.reasoning_mode.value,
                latency_ms=max(0, round((monotonic() - started) * 1_000)),
                input_tokens=sum(usage.input_tokens for usage in usages),
                output_tokens=sum(usage.output_tokens for usage in usages),
                cached_input_tokens=sum(
                    usage.input_tokens_details.cached_tokens for usage in usages
                ),
                reasoning_tokens=sum(
                    usage.output_tokens_details.reasoning_tokens for usage in usages
                ),
                web_search_source_count=evidence.source_count,
                web_search_source_rejected_count=0,
                realtime_feed_count=evidence.known_feed_count,
                realtime_feed_kinds=(_WEATHER_REALTIME_FEED if evidence.known_feed_count else None),
                url_citation_count=len(evidence.citation_urls),
                evidence_source_count=1,
                retry_count=attempt_count - 1,
                attempt_count=attempt_count,
                prior_failure_reason=prior_failure_reason,
                prior_response_id=responses[0].id if attempt_count > 1 else None,
            )
        )

    def _record_failure(
        self,
        operation: str,
        error: OpenAIAdapterError,
        started: float,
        *,
        response: ParsedResponse[FarewellOutputV2] | None,
        evidence: _FarewellEvidence,
        attempt_count: int,
    ) -> None:
        self.recorder.record_failure(
            OpenAIFailureRecord(
                operation=operation,
                code=error.code,
                policy_id=self.config.policy.policy_id.value,
                latency_ms=max(0, round((monotonic() - started) * 1_000)),
                diagnostic_context=error.diagnostic_context,
                diagnostic_kind=error.diagnostic_kind,
                response_id=response.id if response is not None else None,
                attempt_count=attempt_count,
                web_search_source_count=evidence.source_count,
                realtime_feed_count=evidence.known_feed_count,
                url_citation_count=len(evidence.citation_urls),
            )
        )


def _extract_parsed(
    response: ParsedResponse[FarewellOutputV2],
) -> FarewellOutputV2:
    if response.status != "completed":
        reason = response.status
        if response.status == "incomplete":
            reason = (
                response.incomplete_details.reason
                if response.incomplete_details is not None
                and response.incomplete_details.reason is not None
                else "missing"
            )
        raise OpenAIIncompleteResponse(
            diagnostic_context="response_status",
            diagnostic_kind=reason,
        )
    message_seen = False
    for output in response.output:
        if not isinstance(output, ResponseOutputMessage):
            continue
        message_seen = True
        if output.status != "completed":
            raise OpenAIIncompleteResponse(
                diagnostic_context="message_status",
                diagnostic_kind=output.status,
            )
        for content in output.content:
            if isinstance(content, ResponseOutputRefusal):
                raise OpenAIRefusal(
                    diagnostic_context="farewell_message",
                    diagnostic_kind="refusal",
                )
    if not message_seen:
        raise OpenAIIncompleteResponse(
            diagnostic_context="message_status",
            diagnostic_kind="missing",
        )
    if response.output_parsed is None:
        raise OpenAIInvalidOutput(
            diagnostic_context="farewell_schema",
            diagnostic_kind="missing",
        )
    return response.output_parsed


def _inspect_evidence(
    response: ParsedResponse[FarewellOutputV2],
) -> _FarewellEvidence:
    citations: list[str] = []
    source_count = 0
    known_feed_count = 0
    for output in response.output:
        if isinstance(output, ResponseFunctionWebSearch):
            sources = getattr(output.action, "sources", None)
            if isinstance(sources, list):
                source_count += len(sources)
                known_feed_count += sum(_is_known_weather_feed(source) for source in sources)
            continue
        if not isinstance(output, ResponseOutputMessage):
            continue
        for content in output.content:
            if not isinstance(content, ResponseOutputText):
                continue
            for annotation in content.annotations:
                if not isinstance(annotation, AnnotationURLCitation):
                    continue
                url = _valid_citation_url(annotation.url)
                if url is not None:
                    citations.append(url)
    return _FarewellEvidence(tuple(citations), source_count, known_feed_count)


def _require_completed_web_search(response: ParsedResponse[FarewellOutputV2]) -> None:
    search_seen = False
    for output in response.output:
        if not isinstance(output, ResponseFunctionWebSearch):
            continue
        search_seen = True
        if output.status != "completed":
            raise OpenAIIncompleteResponse(
                diagnostic_context="web_search_status",
                diagnostic_kind=output.status,
            )
    if not search_seen:
        raise OpenAIIncompleteResponse(
            diagnostic_context="web_search_status",
            diagnostic_kind="missing",
        )


def _is_known_weather_feed(source: object) -> int:
    source_type = getattr(source, "type", None)
    name = getattr(source, "name", None)
    extra = getattr(source, "model_extra", None)
    if name is None and isinstance(extra, dict):
        name = extra.get("name")
    return int(source_type == "api" and name == _WEATHER_REALTIME_FEED)


def _valid_citation_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > _MAX_CITATION_URL_CHARACTERS or any(char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return url


def _retryable_farewell_error(error: OpenAIAdapterError) -> bool:
    if isinstance(error, (OpenAIConfigurationError, OpenAIRefusal)):
        return False
    if isinstance(error, OpenAIIncompleteResponse) and error.diagnostic_kind == "content_filter":
        return False
    return isinstance(
        error,
        (OpenAIIncompleteResponse, OpenAIInvalidOutput, OpenAIRateLimited, OpenAIUnavailable),
    )


def _map_error(error: Exception) -> OpenAIAdapterError:
    if isinstance(error, OpenAIAdapterError):
        return error
    if isinstance(error, (ValidationError, TypeError, ValueError)):
        return OpenAIInvalidOutput(
            diagnostic_context="farewell_schema",
            diagnostic_kind="invalid",
        )
    if isinstance(error, RateLimitError):
        return OpenAIRateLimited(
            diagnostic_context="farewell_request",
            diagnostic_kind="rate_limited",
        )
    if isinstance(error, AuthenticationError):
        return OpenAIConfigurationError(
            diagnostic_context="farewell_request",
            diagnostic_kind="authentication",
        )
    if isinstance(error, PermissionDeniedError):
        return OpenAIConfigurationError(
            diagnostic_context="farewell_request",
            diagnostic_kind="permission",
        )
    if isinstance(error, NotFoundError):
        return OpenAIConfigurationError(
            diagnostic_context="farewell_request",
            diagnostic_kind="not_found",
        )
    if isinstance(error, APITimeoutError):
        return OpenAIUnavailable(
            diagnostic_context="farewell_request",
            diagnostic_kind="timeout",
        )
    if isinstance(error, APIConnectionError):
        return OpenAIUnavailable(
            diagnostic_context="farewell_request",
            diagnostic_kind="connection",
        )
    if isinstance(error, APIStatusError):
        if error.status_code == 429:
            return OpenAIRateLimited(
                diagnostic_context="farewell_request",
                diagnostic_kind="rate_limited",
            )
        if error.status_code >= 500:
            return OpenAIUnavailable(
                diagnostic_context="farewell_request",
                diagnostic_kind="server",
            )
        return OpenAIConfigurationError(
            diagnostic_context="farewell_request",
            diagnostic_kind="client_status",
        )
    return OpenAIInvalidOutput(
        diagnostic_context="farewell_schema",
        diagnostic_kind="invalid",
    )


__all__ = ("OpenAIFarewellGenerator",)
