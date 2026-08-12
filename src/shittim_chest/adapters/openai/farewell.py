"""One-shot Responses API generation for the best-effort idle farewell."""

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
from openai.types.responses.response_function_web_search import (
    ActionFind,
    ActionOpenPage,
    ActionSearch,
    ActionSearchSource,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from openai.types.responses.response_output_text import AnnotationURLCitation, ResponseOutputText
from openai.types.responses.response_reasoning_item import ResponseReasoningItem
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
from shittim_chest.adapters.openai.schemas import FarewellOutputV1
from shittim_chest.application.farewell import FarewellTimeContext, prepare_farewell_content
from shittim_chest.domain import ParticipantSlot

_INITIAL_MAX_OUTPUT_TOKENS = 4_000
_RETRY_MAX_OUTPUT_TOKENS = 8_000
_MAX_OUTPUT_TOKENS_REASON = "max_output_tokens"


@dataclass(slots=True)
class OpenAIFarewellGenerator:
    """Generate one source-verified greeting without retaining source content."""

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
        """Run one logical request and return only sanitized display content."""

        operation = "farewell_generation"
        started = monotonic()
        retry_count = 0
        prior_incomplete_reason: str | None = None
        responses: list[ParsedResponse[FarewellOutputV1]] = []
        source_count: int | None = None
        citation_count: int | None = None
        evidence_source_count: int | None = None

        def record_retry_usage() -> None:
            if retry_count == 1 and responses:
                self._record_usage(
                    operation,
                    tuple(responses),
                    source_count,
                    citation_count,
                    started,
                    retry_count=retry_count,
                    prior_incomplete_reason=prior_incomplete_reason,
                    evidence_source_count=evidence_source_count,
                )

        def record_failure(error: OpenAIAdapterError) -> None:
            record_retry_usage()
            self._record_failure(operation, error, started)

        try:
            response = await self._request(
                participant=participant,
                time_context=time_context,
                max_output_tokens=_INITIAL_MAX_OUTPUT_TOKENS,
            )
            responses.append(response)
            try:
                parsed = _extract_parsed(response)
            except OpenAIIncompleteResponse as error:
                if not _retryable_incomplete(error):
                    raise
                retry_count = 1
                prior_incomplete_reason = error.diagnostic_kind
                response = await self._request(
                    participant=participant,
                    time_context=time_context,
                    max_output_tokens=_RETRY_MAX_OUTPUT_TOKENS,
                )
                responses.append(response)
                parsed = _extract_parsed(response)
            source_urls, citation_urls = _extract_urls(response)
            source_count = len(source_urls)
            citation_count = len(citation_urls)
            weather_url = _validated_url(parsed.weather_source_url)
            news_url = _validated_url(parsed.news_source_url)
            required = {weather_url, news_url}
            if len(required) != 2 or not required.issubset(source_urls & citation_urls):
                raise OpenAIInvalidOutput()
            evidence_source_count = 2
            content = prepare_farewell_content(parsed.message)
        except asyncio.CancelledError:
            record_retry_usage()
            raise
        except OpenAIAdapterError as error:
            record_failure(error)
            raise
        except (ValidationError, TypeError, ValueError) as error:
            invalid = OpenAIInvalidOutput()
            record_failure(invalid)
            raise invalid from error
        except RateLimitError as error:
            failure = OpenAIRateLimited()
            record_failure(failure)
            raise failure from error
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as error:
            failure = OpenAIConfigurationError()
            record_failure(failure)
            raise failure from error
        except (APIConnectionError, APITimeoutError) as error:
            failure = OpenAIUnavailable()
            record_failure(failure)
            raise failure from error
        except APIStatusError as error:
            failure: OpenAIAdapterError = (
                OpenAIUnavailable() if error.status_code >= 500 else OpenAIConfigurationError()
            )
            record_failure(failure)
            raise failure from error
        self._record_usage(
            operation,
            tuple(responses),
            len(source_urls),
            len(citation_urls),
            started,
            retry_count=retry_count,
            prior_incomplete_reason=prior_incomplete_reason,
        )
        return content

    async def _request(
        self,
        *,
        participant: ParticipantSlot,
        time_context: FarewellTimeContext,
        max_output_tokens: int,
    ) -> ParsedResponse[FarewellOutputV1]:
        async with self.limiter.slot():
            return await self.client.responses.parse(
                model=self.config.model,
                instructions=farewell_instructions(self.personas.for_participant(participant)),
                input=farewell_input(
                    local_datetime=time_context.local_datetime,
                    period=time_context.period,
                    season=time_context.season,
                ),
                text_format=FarewellOutputV1,
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
        responses: tuple[ParsedResponse[FarewellOutputV1], ...],
        source_count: int | None,
        citation_count: int | None,
        started: float,
        *,
        retry_count: int,
        prior_incomplete_reason: str | None,
        evidence_source_count: int | None = 2,
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
                web_search_source_count=source_count,
                web_search_source_rejected_count=0,
                url_citation_count=citation_count,
                evidence_source_count=evidence_source_count,
                retry_count=retry_count,
                prior_incomplete_reason=prior_incomplete_reason,
                prior_response_id=responses[0].id if len(responses) > 1 else None,
            )
        )

    def _record_failure(
        self,
        operation: str,
        error: OpenAIAdapterError,
        started: float,
    ) -> None:
        self.recorder.record_failure(
            OpenAIFailureRecord(
                operation=operation,
                code=error.code,
                policy_id=self.config.policy.policy_id.value,
                latency_ms=max(0, round((monotonic() - started) * 1_000)),
                diagnostic_context=error.diagnostic_context,
                diagnostic_kind=error.diagnostic_kind,
            )
        )


def _extract_parsed(
    response: ParsedResponse[FarewellOutputV1],
) -> FarewellOutputV1:
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
    for output in response.output:
        if isinstance(output, ResponseOutputMessage):
            if output.status != "completed":
                raise OpenAIIncompleteResponse(
                    diagnostic_context="message_status",
                    diagnostic_kind=output.status,
                )
            for content in output.content:
                if isinstance(content, ResponseOutputRefusal):
                    raise OpenAIRefusal()
                if not isinstance(content, ResponseOutputText):
                    raise OpenAIInvalidOutput()
    if response.output_parsed is None:
        raise OpenAIInvalidOutput()
    return response.output_parsed


def _extract_urls(
    response: ParsedResponse[FarewellOutputV1],
) -> tuple[set[str], set[str]]:
    source_urls: set[str] = set()
    citation_urls: set[str] = set()
    if not isinstance(response.output, list):
        raise OpenAIInvalidOutput()
    for output in response.output:
        if isinstance(output, ResponseFunctionWebSearch):
            if output.status != "completed":
                raise OpenAIIncompleteResponse(
                    diagnostic_context="web_search_status",
                    diagnostic_kind=output.status,
                )
            action = output.action
            if isinstance(action, (ActionOpenPage, ActionFind)):
                continue
            if not isinstance(action, ActionSearch) or not isinstance(action.sources, list):
                raise OpenAIInvalidOutput()
            for source in action.sources:
                if not isinstance(source, ActionSearchSource):
                    raise OpenAIInvalidOutput()
                if source.type == "url":
                    source_urls.add(_validated_url(source.url))
                elif source.type != "api":
                    raise OpenAIInvalidOutput()
        elif isinstance(output, ResponseOutputMessage):
            if not isinstance(output.content, list):
                raise OpenAIInvalidOutput()
            for content in output.content:
                if isinstance(content, ResponseOutputRefusal):
                    continue
                if not isinstance(content, ResponseOutputText):
                    raise OpenAIInvalidOutput()
                if not isinstance(content.annotations, list):
                    raise OpenAIInvalidOutput()
                for annotation in content.annotations:
                    if not isinstance(annotation, AnnotationURLCitation):
                        raise OpenAIInvalidOutput()
                    citation_urls.add(_validated_url(annotation.url))
        elif isinstance(output, ResponseReasoningItem):
            continue
        else:
            raise OpenAIInvalidOutput()
    return source_urls, citation_urls


def _retryable_incomplete(error: OpenAIIncompleteResponse) -> bool:
    return (
        error.diagnostic_context == "response_status"
        and error.diagnostic_kind == _MAX_OUTPUT_TOKENS_REASON
    )


def _validated_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenAIInvalidOutput()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
        raise OpenAIInvalidOutput()
    return value


__all__ = ("OpenAIFarewellGenerator",)
