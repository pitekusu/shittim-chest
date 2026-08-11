"""Contract tests for the source-backed farewell Responses request."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from openai.types.responses.response import Response

from shittim_chest.adapters.openai import (
    OpenAIFailureRecord,
    OpenAIFarewellGenerator,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRequestLimiter,
    OpenAIUsageRecord,
    PersonaPrompts,
)
from shittim_chest.adapters.openai.schemas import FarewellOutputV1
from shittim_chest.application.farewell import FarewellTimeContext
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

WEATHER_URL = "https://weather.example.test/tokyo"
NEWS_URL = "https://news.example.test/fun"
MESSAGE = (
    "東京の夏空と今日の楽しい科学ニュースに元気をもらいました。"
    "みんなと過ごせて本当にうれしいです!それでは素敵な夜を、"
    "また元気に集まりましょう!"
)


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
    cite_news: bool = True,
    status: str = "completed",
    search_status: str = "completed",
    message_status: str = "completed",
    additional_output: dict[str, object] | None = None,
    additional_message_content: object | None = None,
    incomplete_reason: str | None = None,
) -> SimpleNamespace:
    annotations = [
        {
            "type": "url_citation",
            "start_index": 0,
            "end_index": 5,
            "title": "Tokyo weather",
            "url": WEATHER_URL,
        }
    ]
    if cite_news:
        annotations.append(
            {
                "type": "url_citation",
                "start_index": 6,
                "end_index": 10,
                "title": "Fun news",
                "url": NEWS_URL,
            }
        )
    output: list[dict[str, object]] = [
        {
            "id": "ws_1",
            "type": "web_search_call",
            "status": search_status,
            "action": {
                "type": "search",
                "query": "東京 今日 天気 楽しいニュース",
                "sources": [
                    {"type": "url", "url": WEATHER_URL},
                    {"type": "url", "url": NEWS_URL},
                ],
            },
        }
    ]
    if additional_output is not None:
        output.append(additional_output)
    message_content: list[object] = [
        {
            "type": "output_text",
            "text": "{}",
            "annotations": annotations,
        }
    ]
    output.append(
        {
            "id": "msg_1",
            "type": "message",
            "status": message_status,
            "role": "assistant",
            "content": message_content,
        }
    )
    typed = Response.model_validate(
        {
            "id": "resp_farewell",
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
    if additional_message_content is not None:
        message_output = typed.output[-1]
        assert message_output.type == "message"
        cast(Any, message_output.content).append(additional_message_content)
    return SimpleNamespace(
        id=typed.id,
        model=typed.model,
        status=status,
        incomplete_details=(
            SimpleNamespace(reason=incomplete_reason) if incomplete_reason is not None else None
        ),
        output=typed.output,
        usage=typed.usage,
        output_parsed=FarewellOutputV1(
            message=MESSAGE,
            weather_source_url=WEATHER_URL,
            news_source_url=NEWS_URL,
        ),
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


@pytest.mark.asyncio
async def test_request_requires_tokyo_web_search_and_returns_only_display_text() -> None:
    service, parse, observer = service_for(response())

    content = await service.generate(
        participant=ParticipantSlot.PARTICIPANT_B,
        time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
    )

    assert content
    assert WEATHER_URL not in content
    assert parse.await_args is not None
    request = parse.await_args.kwargs
    assert request["store"] is False
    assert request["tool_choice"] == "required"
    assert request["max_tool_calls"] == 4
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["max_output_tokens"] == 4_000
    assert request["reasoning"] == {"effort": "medium"}
    assert request["tools"] == [
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
    ]
    assert observer.usages[0].web_search_source_count == 2
    assert observer.usages[0].url_citation_count == 2
    assert "private prompt" not in repr(observer.usages)
    assert observer.usages[0].retry_count == 0
    assert observer.usages[0].prior_incomplete_reason is None


@pytest.mark.asyncio
async def test_missing_weather_or_news_citation_fails_closed() -> None:
    service, _, observer = service_for(response(cite_news=False))

    with pytest.raises(OpenAIInvalidOutput):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_A,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_invalid_output"]


@pytest.mark.asyncio
async def test_reasoning_output_is_allowed() -> None:
    service, _, observer = service_for(
        response(additional_output={"id": "rs_1", "type": "reasoning", "summary": []})
    )

    content = await service.generate(
        participant=ParticipantSlot.PARTICIPANT_A,
        time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
    )

    assert content
    assert observer.failures == []


@pytest.mark.asyncio
async def test_unexpected_output_union_member_fails_closed() -> None:
    service, _, observer = service_for(
        response(
            additional_output={
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "unexpected",
                "arguments": "{}",
                "status": "completed",
            }
        )
    )

    with pytest.raises(OpenAIInvalidOutput):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_A,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_invalid_output"]


@pytest.mark.asyncio
async def test_unexpected_output_message_content_fails_closed() -> None:
    service, _, observer = service_for(
        response(additional_message_content=SimpleNamespace(type="future_content"))
    )

    with pytest.raises(OpenAIInvalidOutput):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_A,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_invalid_output"]


@pytest.mark.parametrize("status", ["cancelled", "failed", "in_progress", "incomplete", "queued"])
@pytest.mark.asyncio
async def test_non_completed_response_status_fails_closed(status: str) -> None:
    service, parse, observer = service_for(response(status=status))

    with pytest.raises(OpenAIIncompleteResponse):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_C,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_incomplete"]
    assert parse.await_count == 1
    assert observer.failures[0].diagnostic_context == "response_status"
    assert observer.failures[0].diagnostic_kind == ("missing" if status == "incomplete" else status)


@pytest.mark.asyncio
async def test_max_output_tokens_incomplete_retries_once_with_larger_budget() -> None:
    service, parse, observer = service_for(response())
    parse.side_effect = [
        response(status="incomplete", incomplete_reason="max_output_tokens"),
        response(),
    ]

    content = await service.generate(
        participant=ParticipantSlot.PARTICIPANT_C,
        time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
    )

    assert content
    assert parse.await_count == 2
    assert [call.kwargs["max_output_tokens"] for call in parse.await_args_list] == [4_000, 8_000]
    assert observer.failures == []
    assert observer.usages[0].retry_count == 1
    assert observer.usages[0].prior_incomplete_reason == "max_output_tokens"


@pytest.mark.asyncio
async def test_content_filter_incomplete_is_not_retried() -> None:
    service, parse, observer = service_for(
        response(status="incomplete", incomplete_reason="content_filter")
    )

    with pytest.raises(OpenAIIncompleteResponse):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_C,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert parse.await_count == 1
    assert observer.failures[0].diagnostic_context == "response_status"
    assert observer.failures[0].diagnostic_kind == "content_filter"


@pytest.mark.asyncio
async def test_repeated_max_output_tokens_incomplete_stops_after_one_retry() -> None:
    incomplete = response(status="incomplete", incomplete_reason="max_output_tokens")
    service, parse, observer = service_for(incomplete)

    with pytest.raises(OpenAIIncompleteResponse):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_C,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert parse.await_count == 2
    assert [failure.code for failure in observer.failures] == ["openai_incomplete"]
    assert observer.failures[0].diagnostic_context == "response_status"
    assert observer.failures[0].diagnostic_kind == "max_output_tokens"


@pytest.mark.parametrize("search_status", ["failed", "in_progress", "searching"])
@pytest.mark.asyncio
async def test_non_completed_web_search_status_fails_closed(search_status: str) -> None:
    service, _, observer = service_for(response(search_status=search_status))

    with pytest.raises(OpenAIIncompleteResponse):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_C,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_incomplete"]
    assert observer.failures[0].diagnostic_context == "web_search_status"
    assert observer.failures[0].diagnostic_kind == search_status


@pytest.mark.parametrize("message_status", ["in_progress", "incomplete"])
@pytest.mark.asyncio
async def test_non_completed_output_message_status_fails_closed(
    message_status: str,
) -> None:
    service, _, observer = service_for(response(message_status=message_status))

    with pytest.raises(OpenAIIncompleteResponse):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_C,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_incomplete"]
    assert observer.failures[0].diagnostic_context == "message_status"
    assert observer.failures[0].diagnostic_kind == message_status
