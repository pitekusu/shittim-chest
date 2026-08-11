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
from shittim_chest.adapters.openai.schemas import FarewellOutputV1
from shittim_chest.application.farewell import FarewellTimeContext, prepare_farewell_content
from shittim_chest.domain import ParticipantSlot


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
        try:
            async with self.limiter.slot():
                response = await self.client.responses.parse(
                    model=self.config.model,
                    instructions=farewell_instructions(self.personas.for_participant(participant)),
                    input=farewell_input(
                        local_datetime=time_context.local_datetime,
                        period=time_context.period,
                        season=time_context.season,
                    ),
                    text_format=FarewellOutputV1,
                    include=["web_search_call.action.sources"],
                    max_output_tokens=600,
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
            parsed = _extract_parsed(response)
            source_urls, citation_urls = _extract_urls(response)
            weather_url = _validated_url(parsed.weather_source_url)
            news_url = _validated_url(parsed.news_source_url)
            required = {weather_url, news_url}
            if len(required) != 2 or not required.issubset(source_urls & citation_urls):
                raise OpenAIInvalidOutput()
            content = prepare_farewell_content(parsed.message)
        except asyncio.CancelledError:
            raise
        except OpenAIAdapterError as error:
            self._record_failure(operation, error, started)
            raise
        except (ValidationError, TypeError, ValueError) as error:
            invalid = OpenAIInvalidOutput()
            self._record_failure(operation, invalid, started)
            raise invalid from error
        except RateLimitError as error:
            failure = OpenAIRateLimited()
            self._record_failure(operation, failure, started)
            raise failure from error
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as error:
            failure = OpenAIConfigurationError()
            self._record_failure(operation, failure, started)
            raise failure from error
        except (APIConnectionError, APITimeoutError) as error:
            failure = OpenAIUnavailable()
            self._record_failure(operation, failure, started)
            raise failure from error
        except APIStatusError as error:
            failure: OpenAIAdapterError = (
                OpenAIUnavailable() if error.status_code >= 500 else OpenAIConfigurationError()
            )
            self._record_failure(operation, failure, started)
            raise failure from error
        self._record_usage(operation, response, len(source_urls), len(citation_urls), started)
        return content

    def _record_usage(
        self,
        operation: str,
        response: ParsedResponse[FarewellOutputV1],
        source_count: int,
        citation_count: int,
        started: float,
    ) -> None:
        usage = response.usage
        self.recorder.record_usage(
            OpenAIUsageRecord(
                operation=operation,
                response_id=response.id,
                model=str(response.model),
                policy_id=self.config.policy.policy_id.value,
                reasoning_mode=self.config.policy.reasoning_mode.value,
                latency_ms=max(0, round((monotonic() - started) * 1_000)),
                input_tokens=usage.input_tokens if usage is not None else 0,
                output_tokens=usage.output_tokens if usage is not None else 0,
                cached_input_tokens=(
                    usage.input_tokens_details.cached_tokens if usage is not None else 0
                ),
                reasoning_tokens=(
                    usage.output_tokens_details.reasoning_tokens if usage is not None else 0
                ),
                web_search_source_count=source_count,
                web_search_source_rejected_count=0,
                url_citation_count=citation_count,
                evidence_source_count=2,
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
            )
        )


def _extract_parsed(
    response: ParsedResponse[FarewellOutputV1],
) -> FarewellOutputV1:
    if response.status == "incomplete":
        raise OpenAIIncompleteResponse()
    for output in response.output:
        if isinstance(output, ResponseOutputMessage):
            if output.status == "incomplete":
                raise OpenAIIncompleteResponse()
            if any(content.type == "refusal" for content in output.content):
                raise OpenAIRefusal()
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
                if not isinstance(content, ResponseOutputText):
                    continue
                if not isinstance(content.annotations, list):
                    raise OpenAIInvalidOutput()
                for annotation in content.annotations:
                    if not isinstance(annotation, AnnotationURLCitation):
                        raise OpenAIInvalidOutput()
                    citation_urls.add(_validated_url(annotation.url))
    return source_urls, citation_urls


def _validated_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OpenAIInvalidOutput()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
        raise OpenAIInvalidOutput()
    return value


__all__ = ("OpenAIFarewellGenerator",)
