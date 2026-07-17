from __future__ import annotations

import json
import logging
import random

import pytest

from shittim_chest.adapters.openai import OpenAIFailureRecord, OpenAIUsageRecord
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
            )
        )
        subject.record_failure(OpenAIFailureRecord("vote", "rate_limited", "policy", 8))

    payloads = [json.loads(record.message) for record in caplog.records]
    assert [payload["event"] for payload in payloads] == [
        "debate_accepted",
        "openai_request_completed",
        "openai_request_failed",
    ]
    assert all(payload["environment"] == "production" for payload in payloads)
    encoded = json.dumps(payloads)
    assert "question" not in encoded
    assert "prompt" not in encoded
