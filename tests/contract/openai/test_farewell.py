"""Contract tests for the availability-first farewell Responses request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError, AsyncOpenAI, AuthenticationError
from openai.types.responses.response import Response
from openai.types.responses.response_function_web_search import ActionSearchSource

from shittim_chest.adapters.openai import (
    OpenAIConfigurationError,
    OpenAIFailureRecord,
    OpenAIFarewellGenerator,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRefusal,
    OpenAIRequestLimiter,
    OpenAIUnavailable,
    OpenAIUsageRecord,
    PersonaPrompts,
)
from shittim_chest.adapters.openai.schemas import FarewellOutputV2
from shittim_chest.application.farewell import FarewellTimeContext
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

WEATHER_URL = "https://weather.example.test/tokyo"
NEWS_URL = "https://news.example.test/fun"
OTHER_URL = "https://sources.example.test/different"
MESSAGE = "東京は夏らしい夜ですね。今日の科学ニュースも楽しかったです。また集まりましょう!"


@dataclass(slots=True)
class Observer:
    usages: list[OpenAIUsageRecord] = field(default_factory=list)
    failures: list[OpenAIFailureRecord] = field(default_factory=list)

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self.usages.append(record)

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self.failures.append(record)


def response(
    *,
    citations: tuple[str, ...] = (NEWS_URL,),
    source_urls: tuple[str, ...] = (WEATHER_URL,),
    status: str = "completed",
    search_status: str = "completed",
    message_status: str = "completed",
    incomplete_reason: str | None = None,
    response_id: str = "resp_farewell",
    message: str = MESSAGE,
    refusal: bool = False,
    additional_output: dict[str, object] | None = None,
    weather_realtime_feed: str | None = None,
    unknown_source: bool = False,
    include_search: bool = True,
) -> SimpleNamespace:
    annotations = [
        {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 5,
            "title": "Source",
            "url": url,
        }
        for url in citations
    ]
    output: list[dict[str, object]] = []
    if include_search:
        output.append(
            {
                "id": "ws_1",
                "type": "web_search_call",
                "status": search_status,
                "action": {
                    "type": "search",
                    "query": "東京 今日 天気 楽しいニュース",
                    "sources": [{"type": "url", "url": url} for url in source_urls],
                },
            }
        )
    if additional_output is not None:
        output.append(additional_output)
    content: list[dict[str, object]]
    if refusal:
        content = [{"type": "refusal", "refusal": "declined"}]
    else:
        content = [{"type": "output_text", "text": "{}", "annotations": annotations}]
    output.append(
        {
            "id": "msg_1",
            "type": "message",
            "status": message_status,
            "role": "assistant",
            "content": content,
        }
    )
    typed = Response.model_validate(
        {
            "id": response_id,
            "object": "response",
            "created_at": 1_786_448_400,
            "status": "completed",
            "completed_at": 1_786_448_401,
            "error": None,
            "incomplete_details": None,
            "model": "gpt-5.6-luna",
            "output": output,
            "parallel_tool_calls": False,
            "tool_choice": "required",
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
            "usage": {
                "input_tokens": 20,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens": 10,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 30,
            },
        }
    )
    if include_search and (weather_realtime_feed is not None or unknown_source):
        search_output = typed.output[0]
        assert search_output.type == "web_search_call"
        sources: list[ActionSearchSource] = []
        if weather_realtime_feed is not None:
            sources.append(
                ActionSearchSource.model_construct(
                    type="api",
                    name=weather_realtime_feed,
                )
            )
        if unknown_source:
            sources.append(
                ActionSearchSource.model_construct(
                    type="future-source",
                    future_field="ignored",
                )
            )
            sources.append(
                ActionSearchSource.model_construct(
                    type="url",
                    url=OTHER_URL,
                    future_field="ignored",
                )
            )
        sources.extend(ActionSearchSource(type="url", url=url) for url in source_urls)
        object.__setattr__(search_output.action, "sources", sources)
    return SimpleNamespace(
        id=typed.id,
        model=typed.model,
        status=status,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason is not None else None
        ),
        output=typed.output,
        usage=typed.usage,
        output_parsed=FarewellOutputV2(message=message),
    )


def service_for(value: object) -> tuple[OpenAIFarewellGenerator, AsyncMock, Observer]:
    parse = AsyncMock(return_value=value)
    client = cast(AsyncOpenAI, SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    observer = Observer()
    prompts = PersonaPrompts({slot: f"private prompt {slot.value}" for slot in PARTICIPANTS})
    return (
        OpenAIFarewellGenerator(
            client=client,
            personas=prompts,
            limiter=OpenAIRequestLimiter(),
            recorder=observer,
        ),
        parse,
        observer,
    )


async def generate(service: OpenAIFarewellGenerator) -> str:
    return await service.generate(
        participant=ParticipantSlot.PARTICIPANT_B,
        time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
    )


@pytest.mark.asyncio
async def test_request_uses_message_only_schema_and_first_citation() -> None:
    service, parse, observer = service_for(response(citations=(NEWS_URL, OTHER_URL)))

    content = await generate(service)

    assert content == f"{MESSAGE}\n参考リンク: {NEWS_URL}"
    assert parse.await_args is not None
    request = parse.await_args.kwargs
    assert request["store"] is False
    assert request["tool_choice"] == "required"
    assert request["max_tool_calls"] == 4
    assert request["text_format"] is FarewellOutputV2
    assert set(FarewellOutputV2.model_fields) == {"message"}
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["reasoning"] == {"effort": "medium"}
    assert request["tools"][0]["user_location"] == {
        "type": "approximate",
        "country": "JP",
        "city": "Tokyo",
        "region": "Tokyo",
        "timezone": "Asia/Tokyo",
    }
    usage = observer.usages[0]
    assert usage.url_citation_count == 2
    assert usage.evidence_source_count == 1
    assert usage.attempt_count == 1


@pytest.mark.asyncio
async def test_one_citation_succeeds_without_weather_feed_or_source_match() -> None:
    service, _, observer = service_for(response(citations=(NEWS_URL,), source_urls=(OTHER_URL,)))

    content = await generate(service)

    assert content.endswith(NEWS_URL)
    assert observer.failures == []
    assert observer.usages[0].realtime_feed_count == 0


@pytest.mark.asyncio
async def test_unknown_source_and_extra_shape_are_ignored_when_citation_is_valid() -> None:
    service, _, observer = service_for(
        response(
            unknown_source=True,
            weather_realtime_feed="oai-weather",
            citations=(NEWS_URL,),
        )
    )

    content = await generate(service)

    assert content.endswith(NEWS_URL)
    assert observer.failures == []
    assert observer.usages[0].realtime_feed_count == 1


@pytest.mark.asyncio
async def test_zero_citations_retries_once_then_records_safe_failure_metadata() -> None:
    service, parse, observer = service_for(response(citations=()))
    parse.side_effect = [
        response(citations=(), response_id="resp_first"),
        response(citations=(), response_id="resp_second"),
    ]

    with pytest.raises(OpenAIInvalidOutput):
        await generate(service)

    assert parse.await_count == 2
    assert [call.kwargs["max_output_tokens"] for call in parse.await_args_list] == [
        4_000,
        8_000,
    ]
    failure = observer.failures[0]
    assert failure.response_id == "resp_second"
    assert failure.attempt_count == 2
    assert failure.url_citation_count == 0
    assert failure.diagnostic_context == "farewell_citation"
    assert NEWS_URL not in repr(observer.failures)


@pytest.mark.asyncio
async def test_transport_failure_never_uses_a_third_application_request() -> None:
    service, parse, observer = service_for(response())
    timeout = APITimeoutError(httpx.Request("POST", "https://api.openai.com/v1/responses"))
    parse.side_effect = [timeout, timeout, response()]

    with pytest.raises(OpenAIUnavailable):
        await generate(service)

    assert parse.await_count == 2
    assert observer.failures[0].attempt_count == 2


@pytest.mark.asyncio
async def test_failed_retry_preserves_usage_from_the_preceding_response() -> None:
    service, parse, observer = service_for(response())
    timeout = APITimeoutError(httpx.Request("POST", "https://api.openai.com/v1/responses"))
    parse.side_effect = [
        response(
            status="incomplete",
            incomplete_reason="max_output_tokens",
            response_id="resp_first",
        ),
        timeout,
    ]

    with pytest.raises(OpenAIUnavailable):
        await generate(service)

    assert parse.await_count == 2
    usage = observer.usages[0]
    assert usage.response_id == "resp_first"
    assert usage.prior_response_id == "resp_first"
    assert usage.attempt_count == 2
    assert usage.retry_count == 1
    assert usage.prior_failure_reason == "max_output_tokens"
    assert usage.input_tokens == 20
    assert usage.output_tokens == 10
    assert observer.failures[0].attempt_count == 2


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried() -> None:
    service, parse, observer = service_for(response())
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    parse.side_effect = AuthenticationError(
        "unauthorized",
        response=httpx.Response(401, request=request),
        body=None,
    )

    with pytest.raises(OpenAIConfigurationError):
        await generate(service)

    assert parse.await_count == 1
    assert observer.failures[0].diagnostic_kind == "authentication"


@pytest.mark.asyncio
async def test_refusal_is_not_retried() -> None:
    service, parse, observer = service_for(response(refusal=True, include_search=False))

    with pytest.raises(OpenAIRefusal):
        await generate(service)

    assert parse.await_count == 1
    assert observer.failures[0].code == "openai_refusal"


@pytest.mark.asyncio
async def test_content_filter_is_not_retried() -> None:
    service, parse, observer = service_for(
        response(
            status="incomplete",
            incomplete_reason="content_filter",
            include_search=False,
        )
    )

    with pytest.raises(OpenAIIncompleteResponse):
        await generate(service)

    assert parse.await_count == 1
    assert observer.failures[0].diagnostic_kind == "content_filter"


@pytest.mark.asyncio
async def test_incomplete_response_retries_once_and_can_succeed() -> None:
    service, parse, observer = service_for(response())
    parse.side_effect = [
        response(
            status="incomplete",
            incomplete_reason="max_output_tokens",
            response_id="resp_incomplete",
        ),
        response(response_id="resp_completed"),
    ]

    content = await generate(service)

    assert content.endswith(NEWS_URL)
    assert parse.await_count == 2
    usage = observer.usages[0]
    assert usage.attempt_count == 2
    assert usage.retry_count == 1
    assert usage.prior_failure_reason == "max_output_tokens"
    assert usage.prior_response_id == "resp_incomplete"


@pytest.mark.parametrize("search_status", ["failed", "in_progress", "searching"])
@pytest.mark.asyncio
async def test_non_completed_web_search_retries_once_then_fails(search_status: str) -> None:
    service, parse, observer = service_for(response(search_status=search_status))

    with pytest.raises(OpenAIIncompleteResponse):
        await generate(service)

    assert parse.await_count == 2
    assert observer.failures[0].diagnostic_context == "web_search_status"


@pytest.mark.asyncio
async def test_unknown_output_union_member_does_not_block_valid_citation() -> None:
    service, _, observer = service_for(
        response(
            additional_output={
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "future_output",
                "arguments": "{}",
                "status": "completed",
            }
        )
    )

    content = await generate(service)

    assert content.endswith(NEWS_URL)
    assert observer.failures == []


@pytest.mark.asyncio
async def test_invalid_citation_is_ignored_and_causes_one_bounded_retry() -> None:
    credentialed_url = "https://user:pass" + "@" + "example.test/private"
    service, parse, observer = service_for(response(citations=(credentialed_url,)))

    with pytest.raises(OpenAIInvalidOutput):
        await generate(service)

    assert parse.await_count == 2
    assert observer.failures[0].url_citation_count == 0


@pytest.mark.asyncio
async def test_structured_output_validation_failure_retries_once() -> None:
    service, parse, observer = service_for(response())
    parse.side_effect = [ValueError("provider shape"), response()]

    content = await generate(service)

    assert content.endswith(NEWS_URL)
    assert parse.await_count == 2
    assert observer.usages[0].attempt_count == 2
    assert "provider shape" not in repr(observer.usages)


@pytest.mark.asyncio
async def test_cancelled_generation_is_not_recorded_as_a_failure() -> None:
    service, parse, observer = service_for(response())
    parse.side_effect = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await generate(service)

    assert observer.failures == []
