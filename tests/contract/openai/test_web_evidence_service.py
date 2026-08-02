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
from pydantic import ValidationError

import shittim_chest.adapters.openai.evidence as evidence_module
from shittim_chest.adapters.openai import (
    OpenAIFailureRecord,
    OpenAIRequestLimiter,
    OpenAIUnavailable,
    OpenAIUsageRecord,
    OpenAIWebEvidenceService,
    RequiredEvidenceUnavailable,
)
from shittim_chest.adapters.openai.schemas import EvidenceDigestOutputV1
from shittim_chest.application.question_router import DeterministicQuestionRouter
from shittim_chest.domain import EvidenceSearchStatus, SearchRequirement


@dataclass(slots=True)
class Observer:
    usages: list[OpenAIUsageRecord] = field(default_factory=list)
    failures: list[OpenAIFailureRecord] = field(default_factory=list)

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self.usages.append(record)

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self.failures.append(record)


def searched_response(
    *,
    url: str = "https://example.test/weather",
    title: str = "Weather source",
) -> SimpleNamespace:
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
                                    "title": title,
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


@pytest.mark.asyncio
async def test_structured_output_validation_is_safely_classified() -> None:
    with pytest.raises(ValidationError) as captured:
        EvidenceDigestOutputV1.model_validate({"unexpected": "field"})
    service, _, observer = service_for(captured.value)

    bundle = await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert bundle.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert bundle.required_search_satisfied is False
    assert [record.code for record in observer.failures] == ["openai_invalid_output"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.provider_response"
    ]
    assert "unexpected" not in repr(observer.failures)


@pytest.mark.asyncio
async def test_invalid_source_is_safely_classified_and_optional_search_continues() -> None:
    service, _, observer = service_for(searched_response(url="", title=""))

    bundle = await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert bundle.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert bundle.required_search_satisfied is False
    assert [record.code for record in observer.failures] == ["openai_invalid_output"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.source_extraction"
    ]
    assert observer.failures[0].diagnostic_context == "web_search_source_url"
    assert observer.failures[0].diagnostic_kind == "empty_string"


@pytest.mark.asyncio
async def test_missing_search_source_url_is_safely_classified() -> None:
    response = searched_response()
    delattr(response.output[0].action.sources[0], "url")
    service, _, observer = service_for(response)

    bundle = await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert bundle.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert [record.code for record in observer.failures] == ["openai_invalid_output"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.source_extraction"
    ]
    assert observer.failures[0].diagnostic_context == "web_search_source_url"
    assert observer.failures[0].diagnostic_kind == "missing"


@pytest.mark.asyncio
async def test_non_string_annotation_url_is_safely_classified() -> None:
    response = searched_response()
    object.__setattr__(
        response.output[1].content[0].annotations[0],
        "url",
        {"private": "provider output must not be recorded"},
    )
    service, _, observer = service_for(response)

    bundle = await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert bundle.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert [record.code for record in observer.failures] == ["openai_invalid_output"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.source_extraction"
    ]
    assert observer.failures[0].diagnostic_context == "url_citation_url"
    assert observer.failures[0].diagnostic_kind == "object"
    assert "provider output" not in repr(observer.failures)


@pytest.mark.asyncio
async def test_missing_annotation_title_uses_url_without_recording_provider_output() -> None:
    response = searched_response()
    delattr(response.output[1].content[0].annotations[0], "title")
    service, _, observer = service_for(response)

    bundle = await service.prepare_evidence(question="今日の天気は?")

    assert bundle.items[0].title == "https://example.test/weather"
    assert observer.failures == []
    assert observer.usages[0].web_search_source_count == 1
    assert observer.usages[0].url_citation_count == 1
    assert observer.usages[0].evidence_source_count == 1
    assert observer.usages[0].title_fallback_count == 1
    assert observer.usages[0].title_fallback_kinds == "missing"


@pytest.mark.asyncio
async def test_null_annotations_are_safely_classified() -> None:
    response = searched_response()
    object.__setattr__(response.output[1].content[0], "annotations", None)
    service, _, observer = service_for(response)

    bundle = await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert bundle.search_status is EvidenceSearchStatus.OPTIONAL_UNAVAILABLE
    assert [record.code for record in observer.failures] == ["openai_invalid_output"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.source_extraction"
    ]
    assert observer.failures[0].diagnostic_context == "output_text_annotations"
    assert observer.failures[0].diagnostic_kind == "null"


@pytest.mark.asyncio
async def test_unexpected_source_failure_records_only_stable_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, observer = service_for(searched_response())

    def fail_source_extraction(*_: object) -> tuple[()]:
        raise RuntimeError("provider output must not be recorded")

    monkeypatch.setattr(evidence_module, "_extract_sources", fail_source_extraction)

    with pytest.raises(RuntimeError, match="provider output must not be recorded"):
        await service.prepare_evidence(question="今日の夕食は何がいい?")

    assert [record.code for record in observer.failures] == ["openai_unclassified"]
    assert [record.operation for record in observer.failures] == [
        "evidence_search.source_extraction"
    ]
    assert observer.failures[0].diagnostic_context == "unexpected_exception"
    assert observer.failures[0].diagnostic_kind == "runtime_error"
    assert "provider output" not in repr(observer.failures)


@pytest.mark.asyncio
async def test_unexpected_router_failure_records_only_stable_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, parse, observer = service_for()

    def fail_routing(self: DeterministicQuestionRouter, question: str) -> None:
        del self, question
        raise RuntimeError("question must not be recorded")

    monkeypatch.setattr(DeterministicQuestionRouter, "route", fail_routing)

    with pytest.raises(RuntimeError, match="question must not be recorded"):
        await service.prepare_evidence(question="private question")

    parse.assert_not_awaited()
    assert [record.code for record in observer.failures] == ["openai_unclassified"]
    assert [record.operation for record in observer.failures] == ["evidence_search.routing"]
    assert observer.failures[0].diagnostic_context == "unexpected_exception"
    assert observer.failures[0].diagnostic_kind == "runtime_error"
    assert "question" not in repr(observer.failures)
