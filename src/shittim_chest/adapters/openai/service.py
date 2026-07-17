"""Async OpenAI Responses API implementation of the application Protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic
from typing import TypeVar

import httpx
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
from pydantic import BaseModel, ValidationError

from shittim_chest.adapters.openai.config import (
    OpenAIAdapterConfig,
    PersonaPrompts,
    PhaseSettings,
)
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
from shittim_chest.adapters.openai.prompts import (
    decision_input,
    final_proposal_input,
    initial_opinion_input,
    moderator_instructions,
    participant_instructions,
    vote_input,
)
from shittim_chest.adapters.openai.schemas import (
    DecisionOutputV1,
    FinalProposalOutputV1,
    OpinionOutputV1,
    VoteOutputV1,
)
from shittim_chest.domain import (
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
    VotingResult,
)

_OutputT = TypeVar("_OutputT", bound=BaseModel)


def create_openai_client(
    *,
    api_key: str,
    http_client: httpx.AsyncClient | None = None,
) -> AsyncOpenAI:
    """Create the one process-level client with three total SDK attempts."""

    if not api_key.strip():
        raise ValueError("OpenAI API key must not be empty")
    timeout = httpx.Timeout(60.0, connect=5.0, write=30.0, pool=5.0)
    return AsyncOpenAI(
        api_key=api_key,
        max_retries=2,
        timeout=timeout,
        http_client=http_client,
    )


@dataclass(slots=True)
class OpenAIResponsesService:
    """Generate domain models through stable, non-beta Responses API calls."""

    client: AsyncOpenAI
    personas: PersonaPrompts
    limiter: OpenAIRequestLimiter
    config: OpenAIAdapterConfig = field(default_factory=OpenAIAdapterConfig)
    recorder: OpenAIUsageRecorder = field(default_factory=NullOpenAIUsageRecorder)

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
    ) -> InitialOpinion:
        output = await self._parse(
            operation="initial_opinion",
            schema=OpinionOutputV1,
            instructions=participant_instructions(self.personas.for_participant(participant)),
            input_text=initial_opinion_input(question, evidence),
            settings=self.config.initial_opinion,
        )
        return InitialOpinion(participant, output.summary, output.proposal)

    async def generate_final_proposal(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
    ) -> FinalProposal:
        output = await self._parse(
            operation="final_proposal",
            schema=FinalProposalOutputV1,
            instructions=participant_instructions(self.personas.for_participant(participant)),
            input_text=final_proposal_input(question, evidence, initial_opinions),
            settings=self.config.final_proposal,
        )
        return FinalProposal(participant, output.title, output.proposal)

    async def cast_vote(
        self,
        *,
        voter: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        candidates: tuple[FinalProposal, ...],
    ) -> Vote:
        output = await self._parse(
            operation="vote",
            schema=VoteOutputV1,
            instructions=participant_instructions(self.personas.for_participant(voter)),
            input_text=vote_input(question, evidence, candidates),
            settings=self.config.vote,
        )
        return Vote(
            voter,
            output.candidate_id,
            output.accuracy_score,
            output.usefulness_score,
            output.safety_score,
            output.reason,
        )

    async def generate_decision(
        self,
        *,
        question: str,
        evidence: EvidenceBundle,
        proposals: tuple[FinalProposal, ...],
        voting_result: VotingResult,
    ) -> FinalDecision:
        output = await self._parse(
            operation="decision",
            schema=DecisionOutputV1,
            instructions=moderator_instructions(),
            input_text=decision_input(question, evidence, proposals, voting_result),
            settings=self.config.decision,
        )
        return FinalDecision(
            voting_result.winner,
            output.decision,
            output.actions,
            output.caveats,
        )

    async def _parse(
        self,
        *,
        operation: str,
        schema: type[_OutputT],
        instructions: str,
        input_text: str,
        settings: PhaseSettings,
    ) -> _OutputT:
        started = monotonic()
        try:
            async with self.limiter.slot():
                response = await self.client.responses.parse(
                    model=self.config.model,
                    instructions=instructions,
                    input=input_text,
                    text_format=schema,
                    max_output_tokens=settings.max_output_tokens,
                    reasoning={"effort": settings.reasoning_effort},
                    store=False,
                    tools=[],
                    tool_choice="none",
                    parallel_tool_calls=False,
                    truncation="disabled",
                )
            parsed = _extract_parsed(response)
        except asyncio.CancelledError:
            raise
        except OpenAIAdapterError as error:
            self._record_failure(operation, error, started)
            raise
        except ValidationError as error:
            invalid_output = OpenAIInvalidOutput()
            self._record_failure(operation, invalid_output, started)
            raise invalid_output from error
        except RateLimitError as error:
            rate_limited = OpenAIRateLimited()
            self._record_failure(operation, rate_limited, started)
            raise rate_limited from error
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as error:
            configuration_error = OpenAIConfigurationError()
            self._record_failure(operation, configuration_error, started)
            raise configuration_error from error
        except (APIConnectionError, APITimeoutError) as error:
            unavailable = OpenAIUnavailable()
            self._record_failure(operation, unavailable, started)
            raise unavailable from error
        except APIStatusError as error:
            status_error: OpenAIAdapterError = (
                OpenAIUnavailable() if error.status_code >= 500 else OpenAIConfigurationError()
            )
            self._record_failure(operation, status_error, started)
            raise status_error from error
        self._record_usage(operation, response, started)
        return parsed

    def _record_usage[OutputT: BaseModel](
        self,
        operation: str,
        response: ParsedResponse[OutputT],
        started: float,
    ) -> None:
        usage = response.usage
        self.recorder.record_usage(
            OpenAIUsageRecord(
                operation=operation,
                response_id=response.id,
                model=str(response.model),
                latency_ms=_elapsed_ms(started),
                input_tokens=usage.input_tokens if usage is not None else 0,
                output_tokens=usage.output_tokens if usage is not None else 0,
                cached_input_tokens=(
                    usage.input_tokens_details.cached_tokens if usage is not None else 0
                ),
                reasoning_tokens=(
                    usage.output_tokens_details.reasoning_tokens if usage is not None else 0
                ),
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
                latency_ms=_elapsed_ms(started),
            )
        )


def _extract_parsed[OutputT: BaseModel](response: ParsedResponse[OutputT]) -> OutputT:
    if response.status == "incomplete":
        raise OpenAIIncompleteResponse()
    for output in response.output:
        if output.type != "message":
            continue
        if output.status == "incomplete":
            raise OpenAIIncompleteResponse()
        for content in output.content:
            if content.type == "refusal":
                raise OpenAIRefusal()
    parsed = response.output_parsed
    if parsed is None:
        raise OpenAIInvalidOutput()
    return parsed


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1_000))
