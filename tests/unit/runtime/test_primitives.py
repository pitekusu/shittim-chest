from __future__ import annotations

import json
import logging
import random
from typing import Literal

import pytest

from shittim_chest.adapters.openai import OpenAIFailureRecord, OpenAIUsageRecord
from shittim_chest.adapters.openai.observability import AffectionReasonKind
from shittim_chest.application.models import MetricEvent
from shittim_chest.domain import DebateId, FinalProposal, ParticipantSlot
from shittim_chest.runtime import ContentFreeTelemetry, SecureCandidateOrderer


def test_secure_candidate_orderer_returns_each_candidate_once() -> None:
    candidates = tuple(
        FinalProposal(participant, participant.value, "Generic proposal")
        for participant in ParticipantSlot
    )
    subject = SecureCandidateOrderer(random.Random(7))  # noqa: S311 - deterministic test

    ordered = subject.order_candidates(
        voter=ParticipantSlot.PARTICIPANT_A,
        candidates=candidates,
    )

    assert len(ordered) == len(candidates)
    assert {candidate.participant for candidate in ordered} == set(ParticipantSlot)


def test_content_free_telemetry_emits_only_explicit_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test-content-free-telemetry")
    subject = ContentFreeTelemetry(logger=logger, environment="production")
    debate_id = DebateId.new()

    with caplog.at_level(logging.INFO, logger=logger.name):
        subject.increment(MetricEvent.ACCEPTED, debate_id=debate_id)
        subject.record_usage(
            OpenAIUsageRecord(
                operation="vote",
                response_id="response-placeholder",
                model="model-placeholder",
                policy_id="luna_standard",
                reasoning_mode="standard",
                latency_ms=12,
                input_tokens=10,
                output_tokens=5,
                cached_input_tokens=0,
                reasoning_tokens=1,
                web_search_source_count=3,
                web_search_source_rejected_count=2,
                web_search_source_rejected_kinds="missing,null",
                realtime_feed_count=1,
                realtime_feed_kinds="weather",
                url_citation_count=2,
                evidence_source_count=2,
                title_fallback_count=1,
                title_fallback_kinds="missing",
                retry_count=1,
                attempt_count=2,
                prior_failure_reason="rate_limited",
                prior_incomplete_reason="max_output_tokens",
                prior_response_id="response-incomplete",
            )
        )
        subject.record_failure(
            OpenAIFailureRecord(
                operation="vote",
                code="rate_limited",
                policy_id="policy",
                latency_ms=8,
                diagnostic_context="url_citation_url",
                diagnostic_kind="object",
                response_id="response-failed",
                model="model-placeholder",
                reasoning_mode="standard",
                max_output_tokens=4_000,
                input_tokens=100,
                output_tokens=40,
                cached_input_tokens=20,
                reasoning_tokens=10,
                attempt_count=2,
                web_search_source_count=3,
                realtime_feed_count=1,
                url_citation_count=0,
            )
        )

    payloads = [json.loads(record.message) for record in caplog.records]
    assert [payload["event"] for payload in payloads] == [
        "debate_accepted",
        "openai_request_completed",
        "openai_request_failed",
    ]
    assert all(payload["environment"] == "production" for payload in payloads)
    assert payloads[1]["web_search_source_count"] == 3
    assert payloads[1]["web_search_source_rejected_count"] == 2
    assert payloads[1]["web_search_source_rejected_kinds"] == "missing,null"
    assert payloads[1]["realtime_feed_count"] == 1
    assert payloads[1]["realtime_feed_kinds"] == "weather"
    assert payloads[1]["url_citation_count"] == 2
    assert payloads[1]["evidence_source_count"] == 2
    assert payloads[1]["title_fallback_count"] == 1
    assert payloads[1]["title_fallback_kinds"] == "missing"
    assert payloads[1]["retry_count"] == 1
    assert payloads[1]["attempt_count"] == 2
    assert payloads[1]["prior_failure_reason"] == "rate_limited"
    assert payloads[1]["prior_incomplete_reason"] == "max_output_tokens"
    assert payloads[1]["prior_response_id"] == "response-incomplete"
    assert payloads[2]["diagnostic_context"] == "url_citation_url"
    assert payloads[2]["diagnostic_kind"] == "object"
    assert payloads[2]["response_id"] == "response-failed"
    assert payloads[2]["model"] == "model-placeholder"
    assert payloads[2]["reasoning_mode"] == "standard"
    assert payloads[2]["max_output_tokens"] == 4_000
    assert payloads[2]["input_tokens"] == 100
    assert payloads[2]["output_tokens"] == 40
    assert payloads[2]["cached_input_tokens"] == 20
    assert payloads[2]["reasoning_tokens"] == 10
    assert payloads[2]["attempt_count"] == 2
    assert payloads[2]["web_search_source_count"] == 3
    assert payloads[2]["realtime_feed_count"] == 1
    assert payloads[2]["url_citation_count"] == 0
    encoded = json.dumps(payloads)
    assert "question" not in encoded
    assert "prompt" not in encoded


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        ("available", "available"),
        ("unavailable", "null"),
        ("unavailable", "blank"),
        ("unavailable", "too_long"),
        ("unavailable", "prompt_disclosure"),
    ],
)
def test_affection_reaction_outcome_is_content_free_success_metadata(
    status: Literal["available", "unavailable"],
    kind: AffectionReasonKind,
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test-affection-reaction-telemetry")
    subject = ContentFreeTelemetry(logger=logger, environment="production")
    with caplog.at_level(logging.INFO, logger=logger.name):
        subject.record_usage(
            OpenAIUsageRecord(
                operation="affection_score",
                response_id="response-placeholder",
                model="model-placeholder",
                policy_id="luna_standard",
                reasoning_mode="standard",
                latency_ms=12,
                input_tokens=10,
                output_tokens=5,
                cached_input_tokens=0,
                reasoning_tokens=1,
                affection_reason_status=status,
                affection_reason_kind=kind,
            )
        )

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload["event"] == "openai_request_completed"
    assert payload["affection_reason_status"] == status
    assert payload["affection_reason_kind"] == kind
    assert set(payload) == {
        "timestamp",
        "severity",
        "service",
        "event",
        "environment",
        "operation",
        "response_id",
        "model",
        "policy_id",
        "reasoning_mode",
        "latency_ms",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "affection_reason_status",
        "affection_reason_kind",
    }
