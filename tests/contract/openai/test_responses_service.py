"""Contract tests against the real OpenAI SDK and an in-memory HTTP transport."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from shittim_chest.adapters.openai import (
    OpenAIAdapterConfig,
    OpenAIConfigurationError,
    OpenAIFailureRecord,
    OpenAIIncompleteResponse,
    OpenAIInvalidOutput,
    OpenAIRateLimited,
    OpenAIRefusal,
    OpenAIResponsesService,
    OpenAIUnavailable,
    OpenAIUsageRecord,
    PersonaPrompts,
    create_openai_client,
)
from shittim_chest.domain import (
    PARTICIPANTS,
    EvidenceBundle,
    FinalProposal,
    InitialOpinion,
    InvalidVote,
    ParticipantSlot,
    Vote,
    select_winner,
)


@dataclass(slots=True)
class RecordingObserver:
    usages: list[OpenAIUsageRecord] = field(default_factory=list)
    failures: list[OpenAIFailureRecord] = field(default_factory=list)

    def record_usage(self, record: OpenAIUsageRecord) -> None:
        self.usages.append(record)

    def record_failure(self, record: OpenAIFailureRecord) -> None:
        self.failures.append(record)


@dataclass(slots=True)
class ResponseServer:
    bodies: deque[dict[str, Any]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[httpx.Headers] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        body = self.bodies.popleft()
        status_code = int(body.pop("_status_code", 200))
        return httpx.Response(status_code, json=body, request=request)


def personas() -> PersonaPrompts:
    return PersonaPrompts({slot: f"persona for {slot.value}" for slot in PARTICIPANTS})


def response_with(output: dict[str, object], *, response_id: str = "resp_test") -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_752_710_400,
        "status": "completed",
        "completed_at": 1_752_710_401,
        "error": None,
        "incomplete_details": None,
        "model": "gpt-5.6-luna",
        "output": [
            {
                "id": f"msg_{response_id}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(output),
                        "annotations": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "none",
        "tools": [],
        "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 130,
        },
    }


async def service_for(
    bodies: list[dict[str, Any]],
    *,
    max_retries: int = 0,
) -> tuple[OpenAIResponsesService, ResponseServer, RecordingObserver, httpx.AsyncClient]:
    server = ResponseServer(deque(bodies))
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=max_retries)
    observer = RecordingObserver()
    return (
        OpenAIResponsesService(client, personas(), recorder=observer),
        server,
        observer,
        http_client,
    )


@pytest.mark.asyncio
async def test_structured_phases_map_to_domain_and_never_enable_multi_agent() -> None:
    bodies = [
        response_with({"summary": "summary", "proposal": "proposal"}, response_id="resp_1"),
        response_with({"title": "title", "proposal": "revised"}, response_id="resp_2"),
        response_with(
            {
                "candidate_id": "participant-b",
                "accuracy_score": 4,
                "usefulness_score": 5,
                "safety_score": 3,
                "reason": "reason",
            },
            response_id="resp_3",
        ),
        response_with(
            {"decision": "decision", "actions": ["action"], "caveats": ["caveat"]},
            response_id="resp_4",
        ),
    ]
    service, server, observer, http_client = await service_for(bodies)
    evidence = EvidenceBundle()
    try:
        opinion = await service.generate_initial_opinion(
            participant=ParticipantSlot.PARTICIPANT_A,
            question="question",
            evidence=evidence,
        )
        proposal = await service.generate_final_proposal(
            participant=ParticipantSlot.PARTICIPANT_A,
            question="question",
            evidence=evidence,
            initial_opinions=(opinion,),
        )
        vote = await service.cast_vote(
            voter=ParticipantSlot.PARTICIPANT_A,
            question="question",
            evidence=evidence,
            candidates=(
                FinalProposal(ParticipantSlot.PARTICIPANT_B, "b", "proposal-b"),
                FinalProposal(ParticipantSlot.PARTICIPANT_C, "c", "proposal-c"),
            ),
        )
        ballot = (
            vote,
            Vote(
                ParticipantSlot.PARTICIPANT_B,
                ParticipantSlot.PARTICIPANT_A,
                3,
                3,
                3,
                "reason",
            ),
            Vote(
                ParticipantSlot.PARTICIPANT_C,
                ParticipantSlot.PARTICIPANT_A,
                3,
                3,
                3,
                "reason",
            ),
        )
        voting_result = select_winner(ballot)
        decision = await service.generate_decision(
            question="question",
            evidence=evidence,
            proposals=(
                proposal,
                FinalProposal(ParticipantSlot.PARTICIPANT_B, "b", "proposal-b"),
                FinalProposal(ParticipantSlot.PARTICIPANT_C, "c", "proposal-c"),
            ),
            voting_result=voting_result,
        )
    finally:
        await http_client.aclose()

    assert opinion == InitialOpinion(ParticipantSlot.PARTICIPANT_A, "summary", "proposal")
    assert proposal == FinalProposal(ParticipantSlot.PARTICIPANT_A, "title", "revised")
    assert vote.candidate is ParticipantSlot.PARTICIPANT_B
    assert decision.decision == "decision"
    assert decision.winner is ParticipantSlot.PARTICIPANT_A
    assert [record.operation for record in observer.usages] == [
        "initial_opinion",
        "final_proposal",
        "vote",
        "decision",
    ]
    assert observer.usages[0].input_tokens == 100
    assert observer.usages[0].cached_input_tokens == 20
    assert observer.usages[0].reasoning_tokens == 10
    assert observer.failures == []

    for request, headers in zip(server.requests, server.headers, strict=True):
        assert request["model"] == "gpt-5.6-luna"
        assert request["store"] is False
        assert request["tools"] == []
        assert request["tool_choice"] == "none"
        assert request["parallel_tool_calls"] is False
        assert "multi_agent" not in request
        assert "previous_response_id" not in request
        assert "OpenAI-Beta" not in headers
        assert "test-key" not in json.dumps(request)
    assert server.requests[2]["reasoning"] == {"effort": "low"}
    assert server.requests[0]["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_refusal_is_detected_without_recording_raw_provider_text() -> None:
    body = response_with({})
    body["output"][0]["content"] = [{"type": "refusal", "refusal": "raw provider refusal text"}]
    service, _, observer, http_client = await service_for([body])
    try:
        with pytest.raises(OpenAIRefusal) as raised:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert raised.value.code == "openai_refusal"
    assert "raw provider" not in str(raised.value)
    assert observer.usages == []
    assert observer.failures[0].code == "openai_refusal"


@pytest.mark.asyncio
async def test_incomplete_and_missing_parsed_output_are_distinct() -> None:
    incomplete = response_with({"summary": "partial", "proposal": "partial"})
    incomplete["status"] = "incomplete"
    incomplete["completed_at"] = None
    incomplete["incomplete_details"] = {"reason": "max_output_tokens"}
    missing = response_with({})
    missing["output"] = []
    service, _, observer, http_client = await service_for([incomplete, missing])
    try:
        with pytest.raises(OpenAIIncompleteResponse):
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
        with pytest.raises(OpenAIInvalidOutput):
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert [record.code for record in observer.failures] == [
        "openai_incomplete",
        "openai_invalid_output",
    ]


@pytest.mark.asyncio
async def test_schema_validation_error_is_mapped_without_exposing_output() -> None:
    body = response_with({"summary": "valid", "proposal": 123})
    service, _, observer, http_client = await service_for([body])
    try:
        with pytest.raises(OpenAIInvalidOutput) as raised:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert raised.value.code == "openai_invalid_output"
    assert "123" not in str(raised.value)
    assert observer.failures[0].code == "openai_invalid_output"


@pytest.mark.asyncio
async def test_rate_limit_and_authentication_errors_map_to_stable_codes() -> None:
    rate_limit = {
        "_status_code": 429,
        "error": {"message": "limited", "type": "rate_limit_error", "code": "rate_limit"},
    }
    authentication = {
        "_status_code": 401,
        "error": {
            "message": "invalid key",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        },
    }
    service, _, observer, http_client = await service_for([rate_limit, authentication])
    try:
        with pytest.raises(OpenAIRateLimited) as limited:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
        with pytest.raises(OpenAIConfigurationError) as invalid:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert limited.value.retryable is True
    assert invalid.value.retryable is False
    assert [record.code for record in observer.failures] == [
        "openai_rate_limited",
        "openai_configuration",
    ]


@pytest.mark.parametrize("status_code", [403, 404])
@pytest.mark.asyncio
async def test_permission_and_model_errors_are_configuration_failures(status_code: int) -> None:
    body = {
        "_status_code": status_code,
        "error": {
            "message": "provider detail",
            "type": "invalid_request_error",
            "code": "provider_code",
        },
    }
    service, _, observer, http_client = await service_for([body])
    try:
        with pytest.raises(OpenAIConfigurationError):
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert observer.failures[0].code == "openai_configuration"


@pytest.mark.asyncio
async def test_server_error_is_a_retryable_unavailable_failure() -> None:
    body = {
        "_status_code": 500,
        "error": {
            "message": "provider detail",
            "type": "server_error",
            "code": "server_error",
        },
    }
    service, _, observer, http_client = await service_for([body])
    try:
        with pytest.raises(OpenAIUnavailable) as raised:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert raised.value.code == "openai_unavailable"
    assert raised.value.retryable is True
    assert observer.failures[0].code == "openai_unavailable"


@pytest.mark.parametrize("transport_error", ["connect", "timeout"])
@pytest.mark.asyncio
async def test_transport_errors_are_retryable_unavailable_failures(
    transport_error: str,
) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if transport_error == "connect":
            raise httpx.ConnectError("transport detail", request=request)
        raise httpx.ReadTimeout("transport detail", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0)
    observer = RecordingObserver()
    service = OpenAIResponsesService(client, personas(), recorder=observer)
    try:
        with pytest.raises(OpenAIUnavailable) as raised:
            await service.generate_initial_opinion(
                participant=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
            )
    finally:
        await http_client.aclose()

    assert raised.value.code == "openai_unavailable"
    assert raised.value.retryable is True
    assert observer.failures[0].code == "openai_unavailable"


@pytest.mark.asyncio
async def test_process_concurrency_never_exceeds_configured_limit() -> None:
    active = 0
    maximum_active = 0
    calls = 0
    six_started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, calls, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 6:
            six_started.set()
        try:
            await release.wait()
            return httpx.Response(
                200,
                json=response_with({"summary": "summary", "proposal": "proposal"}),
                request=request,
            )
        finally:
            active -= 1

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(blocking_handler))
    client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0)
    service = OpenAIResponsesService(client, personas())
    try:
        async with asyncio.TaskGroup() as group:
            for _ in range(7):
                group.create_task(
                    service.generate_initial_opinion(
                        participant=ParticipantSlot.PARTICIPANT_A,
                        question="question",
                        evidence=EvidenceBundle(),
                    )
                )
            await asyncio.wait_for(six_started.wait(), timeout=1)
            await asyncio.sleep(0)
            assert calls == 6
            release.set()
    finally:
        await http_client.aclose()

    assert calls == 7
    assert maximum_active == 6


@pytest.mark.asyncio
async def test_cancellation_is_rethrown_without_failure_telemetry() -> None:
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await never_release.wait()
        return httpx.Response(500, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(blocking_handler))
    client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=0)
    observer = RecordingObserver()
    service = OpenAIResponsesService(client, personas(), recorder=observer)
    task = asyncio.create_task(
        service.generate_initial_opinion(
            participant=ParticipantSlot.PARTICIPANT_A,
            question="question",
            evidence=EvidenceBundle(),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await http_client.aclose()

    assert observer.usages == []
    assert observer.failures == []


@pytest.mark.asyncio
async def test_domain_rejects_self_vote_after_schema_parsing() -> None:
    body = response_with(
        {
            "candidate_id": "participant-a",
            "accuracy_score": 3,
            "usefulness_score": 3,
            "safety_score": 3,
            "reason": "reason",
        }
    )
    service, _, _, http_client = await service_for([body])
    try:
        with pytest.raises(InvalidVote) as raised:
            await service.cast_vote(
                voter=ParticipantSlot.PARTICIPANT_A,
                question="question",
                evidence=EvidenceBundle(),
                candidates=(
                    FinalProposal(ParticipantSlot.PARTICIPANT_B, "b", "proposal-b"),
                    FinalProposal(ParticipantSlot.PARTICIPANT_C, "c", "proposal-c"),
                ),
            )
    finally:
        await http_client.aclose()

    assert raised.value.code == "self_vote"


def test_config_and_persona_prompts_fail_closed() -> None:
    with pytest.raises(ValueError, match="exactly"):
        PersonaPrompts({ParticipantSlot.PARTICIPANT_A: "only one"})
    with pytest.raises(ValueError, match="3,500"):
        PersonaPrompts({slot: "あ" * 1_167 for slot in PARTICIPANTS})
    with pytest.raises(ValueError, match="empty"):
        PersonaPrompts({slot: " " for slot in PARTICIPANTS})
    with pytest.raises(ValueError, match="concurrency"):
        OpenAIAdapterConfig(max_concurrency=7)
    with pytest.raises(ValueError, match="model"):
        OpenAIAdapterConfig(model=" ")


@pytest.mark.asyncio
async def test_client_factory_uses_bounded_sdk_retries() -> None:
    def unused_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(unused_handler))
    client = create_openai_client(api_key="test-key", http_client=http_client)
    try:
        assert client.max_retries == 2
        timeout = client.timeout
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect == 5.0
        assert timeout.write == 30.0
        assert timeout.pool == 5.0
    finally:
        await client.close()
