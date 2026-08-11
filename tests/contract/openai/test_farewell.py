"""Contract tests for the source-backed farewell Responses request."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from openai.types.responses.response import Response

from shittim_chest.adapters.openai import (
    OpenAIFailureRecord,
    OpenAIFarewellGenerator,
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


def response(*, cite_news: bool = True) -> SimpleNamespace:
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
            "output": [
                {
                    "id": "ws_1",
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "東京 今日 天気 楽しいニュース",
                        "sources": [
                            {"type": "url", "url": WEATHER_URL},
                            {"type": "url", "url": NEWS_URL},
                        ],
                    },
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "{}",
                            "annotations": annotations,
                        }
                    ],
                },
            ],
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
    return SimpleNamespace(
        id=typed.id,
        model=typed.model,
        status=typed.status,
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


@pytest.mark.asyncio
async def test_missing_weather_or_news_citation_fails_closed() -> None:
    service, _, observer = service_for(response(cite_news=False))

    with pytest.raises(OpenAIInvalidOutput):
        await service.generate(
            participant=ParticipantSlot.PARTICIPANT_A,
            time_context=FarewellTimeContext("2026-08-11T21:00+09:00", "夜", "夏"),
        )

    assert [failure.code for failure in observer.failures] == ["openai_invalid_output"]
