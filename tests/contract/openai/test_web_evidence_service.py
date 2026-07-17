"""Contract tests for the hosted Responses API web-search boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI
from openai.types.responses.response import Response

from shittim_chest.adapters.openai import (
    OpenAIFailureRecord,
    OpenAIRequestLimiter,
    OpenAIUnavailable,
    OpenAIUsageRecord,
    OpenAIWebEvidenceService,
    RequiredEvidenceUnavailable,
)
from shittim_chest.adapters.openai.schemas import EvidenceDigestOutputV1
from shittim_chest.domain import EvidenceSearchStatus, SearchRequirement


@dataclass(slots=True)
class Observer:
    usages: list[OpenAIUsageRecord] = field(default_factory=list)
    failures: list[OpenAIFailureRecord] = field(default_factory=list)

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self.usages.append(record)

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self.failures.append(record)


def searched_response() -> SimpleNamespace:
    url = "https://example.test/weather"
    typed = Response.model_validate(
        {
            "id": "resp_evidence",
            "object": "response",
            "created_at": 1_752_710_400,
            "status": "completed",
            "completed_at": 1_752_710_401,
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
                        "query": "東京 今日 天気",
                        "sources": [{"type": "url", "url": url}],
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
                            "text": '{"summary":"今日は晴れです。"}',
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "start_index": 0,
                                    "end_index": 8,
                                    "title": "Weather source",
                                    "url": url,
                                }
                            ],
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
        output=typed.output,
        usage=typed.usage,
        output_parsed=EvidenceDigestOutputV1(summary="今日は晴れです。"),
    )


def service_for(*responses: object) -> tuple[OpenAIWebEvidenceService, AsyncMock, Observer]:
    parse = AsyncMock(side_effect=responses)
    client = cast(AsyncOpenAI, SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    observer = Observer()
    service = OpenAIWebEvidenceService(
        client,
        OpenAIRequestLimiter(),
        recorder=observer,
        clock=lambda: datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC),
    )
    return service, parse, observer


@pytest.mark.asyncio
async def test_required_search_persists_digest_sources_and_safe_request_shape() -> None:
    service, parse, observer = service_for(searched_response())

    bundle = await service.prepare_evidence(question="東京の今日の天気は?")

    assert bundle.search_requirement is SearchRequirement.REQUIRED
    assert bundle.search_status is EvidenceSearchStatus.COMPLETED
    assert bundle.summary == "今日は晴れです。"
    assert bundle.search_response_id == "resp_evidence"
    assert bundle.router_rules_version == "question-router-v2"
    assert bundle.routing_reason == "current_fact"
    assert bundle.items[0].title == "Weather source"
    assert bundle.items[0].retrieved_at == "2026-07-17T01:02:03Z"
    assert len(bundle.items[0].content_hash) == 64
    assert parse.await_args is not None
    request = parse.await_args.kwargs
    assert request["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert request["tool_choice"] == "required"
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["max_tool_calls"] == 4
    assert request["store"] is False
    assert observer.usages[0].operation == "evidence_search"


@pytest.mark.asyncio
async def test_none_route_avoids_provider_call() -> None:
    service, parse, _ = service_for()

    bundle = await service.prepare_evidence(question="パンケーキを比較して")

    assert bundle.search_requirement is SearchRequirement.NONE
    assert bundle.routing_reason == "explicitly_timeless"
    parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_failure_continues_but_required_failure_stops() -> None:
    service, _, _ = service_for(OpenAIUnavailable(), OpenAIUnavailable())

    optional = await service.prepare_evidence(
        question="今日の朝ごはんは何がいい?甘いものが食べたい"
    )
    with pytest.raises(RequiredEvidenceUnavailable):
        await service.prepare_evidence(question="今日の天気は?")

    assert optional.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert optional.required_search_satisfied is False
