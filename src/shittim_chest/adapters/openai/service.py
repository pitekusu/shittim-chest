"""Async OpenAI Responses API implementation of the application Protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import TypeVar

import httpx2
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
from openai.types.responses import Response
from openai.types.shared_params.reasoning import Reasoning
from pydantic import BaseModel, ValidationError

from shittim_chest.adapters.openai import reconsideration
from shittim_chest.adapters.openai.composite_ballot import score_composite_ballot
from shittim_chest.adapters.openai.config import (
    OpenAIAdapterConfig,
    ParticipantProfiles,
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
    affection_response_instructions,
    affection_scoring_input,
    affection_scoring_instructions,
    candidates_input,
    decision_input,
    deliberation_instructions,
    final_proposal_input,
    final_proposal_instructions,
    initial_opinion_input,
    participant_instructions,
    preferences_input,
    private_participant_instructions,
    vote_input,
    winner_decision_instructions,
)
from shittim_chest.adapters.openai.schemas import (
    AffectionScoreOutputV1,
    CandidatePlanOutputV1,
    DecisionOutputV1,
    FinalProposalOutputV1,
    OpinionOutputV1,
    PreferenceFrameOutputV1,
    VoteOutputV1,
)
from shittim_chest.application.generation_policy import ReasoningMode
from shittim_chest.domain import (
    DEFAULT_AFFECTION_SCORE,
    Candidate,
    CandidatePlan,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    PreferenceFrame,
    Vote,
    VotingResult,
)
from shittim_chest.domain.composite_voting import (
    COMPOSITE_VOTING_VERSION,
    ResolvedVote,
    resolve_composite_ballot,
)

_OutputT = TypeVar("_OutputT", bound=BaseModel)


def _validate_frame_owner(
    frame: PreferenceFrame | None,
    participant: ParticipantSlot,
    candidate_plan: CandidatePlan | None = None,
) -> None:
    if frame is not None and frame.participant is not participant:
        raise ValueError("preference frame belongs to another participant")
    if candidate_plan is not None and candidate_plan.participant is not participant:
        raise ValueError("candidate plan belongs to another participant")


def create_openai_client(
    *,
    api_key: str,
    http_client: httpx2.AsyncClient | None = None,
) -> AsyncOpenAI:
    """Create the one process-level client with three total SDK attempts."""

    if not api_key.strip():
        raise ValueError("OpenAI API key must not be empty")
    timeout = httpx2.Timeout(60.0, connect=5.0, write=30.0, pool=5.0)
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
    profiles: ParticipantProfiles
    limiter: OpenAIRequestLimiter
    config: OpenAIAdapterConfig = field(default_factory=OpenAIAdapterConfig)
    recorder: OpenAIUsageRecorder = field(default_factory=NullOpenAIUsageRecorder)
    system_prompt: str | None = field(default=None, repr=False)

    async def find_overlap_targets(
        self,
        *,
        question: str,
        positions: tuple[InitialOpinion, ...],
        rotation: int,
        coordination: bool,
    ) -> tuple[ParticipantSlot, ...]:
        return await reconsideration.classify(self, question, positions, rotation, coordination)

    async def explore_alternatives(
        self,
        *,
        question: str,
        frame: PreferenceFrame,
        plan: CandidatePlan,
        peers: tuple[InitialOpinion, ...],
        evidence: EvidenceBundle,
    ) -> tuple[Candidate, ...]:
        _validate_frame_owner(frame, plan.participant, plan)
        return await reconsideration.explore(self, question, frame, plan, peers, evidence)

    async def select_alternative(
        self,
        *,
        question: str,
        frame: PreferenceFrame,
        plan: CandidatePlan,
        peers: tuple[InitialOpinion, ...],
        evidence: EvidenceBundle,
        alternatives: tuple[Candidate, ...],
    ) -> CandidatePlan:
        _validate_frame_owner(frame, plan.participant, plan)
        return await reconsideration.select(
            self, question, frame, plan, peers, evidence, alternatives
        )

    async def revise_initial_opinion(
        self,
        *,
        question: str,
        frame: PreferenceFrame,
        plan: CandidatePlan,
        opinions: tuple[InitialOpinion, ...],
        evidence: EvidenceBundle,
        affection_score: int,
    ) -> InitialOpinion:
        _validate_frame_owner(frame, plan.participant, plan)
        return await reconsideration.revise_opinion(
            self,
            question,
            frame,
            plan,
            {x.participant: x for x in opinions},
            evidence,
            affection_score,
        )

    async def score_affection(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
    ) -> int:
        """Score one untrusted question independently in one participant persona."""

        output = await self._parse(
            operation="affection_score",
            schema=AffectionScoreOutputV1,
            instructions=affection_scoring_instructions(
                self.profiles.for_participant(participant).system_prompt,
            ),
            input_text=affection_scoring_input(question),
            settings=self.config.affection,
        )
        return output.score

    async def form_preferences(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
    ) -> PreferenceFrame:
        output = await self._parse_preparation(
            operation="preferences",
            schema=PreferenceFrameOutputV1,
            instructions=deliberation_instructions(
                self.profiles.for_participant(participant).system_prompt,
                selecting=False,
                system_prompt=self.system_prompt,
            ),
            input_text=preferences_input(question),
            settings=self.config.policy.preferences,
        )
        return PreferenceFrame(
            participant, output.priorities, output.avoidances, output.compromise_condition
        )

    async def select_candidates(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        preference_frame: PreferenceFrame | None = None,
    ) -> CandidatePlan:
        _validate_frame_owner(preference_frame, participant)
        output = await self._parse_preparation(
            operation="candidates",
            schema=CandidatePlanOutputV1,
            instructions=deliberation_instructions(
                self.profiles.for_participant(participant).system_prompt,
                selecting=True,
                system_prompt=self.system_prompt,
                use_frame=preference_frame is not None,
            ),
            input_text=candidates_input(question, evidence, preference_frame),
            settings=self.config.policy.candidates,
        )
        return CandidatePlan(
            participant,
            tuple(Candidate(item.proposal, item.fit, item.tradeoff) for item in output.candidates),
        )

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        affection_score: int = DEFAULT_AFFECTION_SCORE,
        preference_frame: PreferenceFrame | None = None,
        candidate_plan: CandidatePlan | None = None,
    ) -> InitialOpinion:
        _validate_frame_owner(preference_frame, participant, candidate_plan)
        output = await self._parse(
            operation="initial_opinion",
            schema=OpinionOutputV1,
            instructions=(
                participant_instructions(
                    self.profiles,
                    participant,
                    system_prompt=self.system_prompt,
                )
                + affection_response_instructions(affection_score)
            ),
            input_text=initial_opinion_input(question, evidence, preference_frame, candidate_plan),
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
        affection_score: int = DEFAULT_AFFECTION_SCORE,
        preference_frame: PreferenceFrame | None = None,
        candidate_plan: CandidatePlan | None = None,
    ) -> FinalProposal:
        _validate_frame_owner(preference_frame, participant, candidate_plan)
        output = await self._parse(
            operation="final_proposal",
            schema=FinalProposalOutputV1,
            instructions=(
                final_proposal_instructions(
                    self.profiles,
                    participant,
                    system_prompt=self.system_prompt,
                )
                + affection_response_instructions(affection_score)
            ),
            input_text=final_proposal_input(
                question,
                evidence,
                initial_opinions,
                preference_frame,
                candidate_plan,
                participant=participant,
            ),
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
        preference_frame: PreferenceFrame | None = None,
        voting_rules_version: str = "legacy-v1",
        debate_key: str | None = None,
        initial_opinions: tuple[InitialOpinion, ...] = (),
    ) -> Vote | ResolvedVote:
        _validate_frame_owner(preference_frame, voter)
        if voting_rules_version == COMPOSITE_VOTING_VERSION:
            if not debate_key:
                raise ValueError("composite voting requires a stable debate key")
            ballot = await score_composite_ballot(
                self,
                voter=voter,
                question=question,
                evidence=evidence,
                candidates=candidates,
                initial_opinions=initial_opinions,
            )
            return resolve_composite_ballot(ballot, debate_key=debate_key)
        if voting_rules_version != "legacy-v1":
            raise ValueError("unknown voting rules")
        output = await self._parse(
            operation="vote",
            schema=VoteOutputV1,
            instructions=private_participant_instructions(
                self.profiles.for_participant(voter).system_prompt,
                system_prompt=self.system_prompt,
            ),
            input_text=vote_input(question, evidence, candidates, preference_frame),
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
        affection_score: int = DEFAULT_AFFECTION_SCORE,
        preference_frame: PreferenceFrame | None = None,
    ) -> FinalDecision:
        _validate_frame_owner(preference_frame, voting_result.winner)
        output = await self._parse(
            operation="decision",
            schema=DecisionOutputV1,
            instructions=(
                winner_decision_instructions(
                    self.profiles,
                    voting_result.winner,
                    system_prompt=self.system_prompt,
                )
                + affection_response_instructions(affection_score)
            ),
            input_text=decision_input(
                question, evidence, proposals, voting_result, preference_frame
            ),
            settings=self.config.decision,
        )
        return FinalDecision(
            voting_result.winner,
            output.decision,
            output.actions,
            output.caveats,
            output.victory_message,
        )

    async def _parse_preparation(
        self,
        *,
        operation: str,
        schema: type[_OutputT],
        instructions: str,
        input_text: str,
        settings: PhaseSettings,
    ) -> _OutputT:
        """Retry only a confirmed token limit, once, inside the caller's deadline.

        Partial output is discarded. Other participants' calls and saved checkpoints
        are untouched; refusals, filters and unknown incomplete reasons propagate.
        Each request keeps the existing content-free success/failure telemetry.
        """
        try:
            return await self._parse(
                operation=operation,
                schema=schema,
                instructions=instructions,
                input_text=input_text,
                settings=settings,
            )
        except OpenAIIncompleteResponse as error:
            if (
                error.diagnostic_context != "response_status"
                or error.diagnostic_kind != "max_output_tokens"
            ):
                raise
        return await self._parse(
            operation=operation,
            schema=schema,
            instructions=instructions,
            input_text=input_text,
            settings=replace(settings, max_output_tokens=settings.max_output_tokens * 2),
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
        response: Response | None = None
        try:
            async with self.limiter.slot():
                reasoning: Reasoning = {"effort": settings.reasoning_effort.value}
                if self.config.policy.reasoning_mode is ReasoningMode.PRO:
                    reasoning["mode"] = "pro"
                response = await self.client.responses.create(
                    model=self.config.model,
                    instructions=instructions,
                    input=input_text,
                    text={
                        "format": {
                            "type": "json_schema",
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        }
                    },
                    max_output_tokens=settings.max_output_tokens,
                    reasoning=reasoning,
                    store=False,
                    tools=[],
                    tool_choice="none",
                    parallel_tool_calls=False,
                    truncation="disabled",
                )
            parsed = _extract_parsed(response, schema)
        except asyncio.CancelledError:
            raise
        except OpenAIAdapterError as error:
            self._record_failure(operation, error, started, response=response, settings=settings)
            raise
        except ValidationError as error:
            invalid_output = _validation_error(error, schema)
            self._record_failure(
                operation,
                invalid_output,
                started,
                response=response,
                settings=settings,
            )
            raise invalid_output from error
        except RateLimitError as error:
            rate_limited = OpenAIRateLimited()
            self._record_failure(
                operation,
                rate_limited,
                started,
                response=response,
                settings=settings,
            )
            raise rate_limited from error
        except (AuthenticationError, PermissionDeniedError, NotFoundError) as error:
            configuration_error = OpenAIConfigurationError()
            self._record_failure(
                operation,
                configuration_error,
                started,
                response=response,
                settings=settings,
            )
            raise configuration_error from error
        except (APIConnectionError, APITimeoutError) as error:
            unavailable = OpenAIUnavailable()
            self._record_failure(
                operation,
                unavailable,
                started,
                response=response,
                settings=settings,
            )
            raise unavailable from error
        except APIStatusError as error:
            status_error: OpenAIAdapterError = (
                OpenAIUnavailable() if error.status_code >= 500 else OpenAIConfigurationError()
            )
            self._record_failure(
                operation,
                status_error,
                started,
                response=response,
                settings=settings,
            )
            raise status_error from error
        self._record_usage(operation, response, started)
        return parsed

    def _record_usage(
        self,
        operation: str,
        response: Response,
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
        *,
        response: Response | None,
        settings: PhaseSettings,
    ) -> None:
        usage = response.usage if response is not None else None
        self.recorder.record_failure(
            OpenAIFailureRecord(
                operation=operation,
                code=error.code,
                policy_id=self.config.policy.policy_id.value,
                latency_ms=_elapsed_ms(started),
                diagnostic_context=error.diagnostic_context,
                diagnostic_kind=error.diagnostic_kind,
                response_id=response.id if response is not None else None,
                model=str(response.model) if response is not None else self.config.model,
                reasoning_mode=self.config.policy.reasoning_mode.value,
                max_output_tokens=settings.max_output_tokens,
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                cached_input_tokens=(
                    usage.input_tokens_details.cached_tokens if usage is not None else None
                ),
                reasoning_tokens=(
                    usage.output_tokens_details.reasoning_tokens if usage is not None else None
                ),
            )
        )


def _extract_parsed[OutputT: BaseModel](response: Response, schema: type[OutputT]) -> OutputT:
    # Check the envelope before parsing potentially truncated structured text.
    if response.status != "completed":
        reason = response.status
        if response.status == "incomplete":
            reason = (
                response.incomplete_details.reason
                if response.incomplete_details is not None
                and response.incomplete_details.reason is not None
                else "missing"
            )
        raise OpenAIIncompleteResponse(
            diagnostic_context="response_status",
            diagnostic_kind=reason,
        )
    for output in response.output:
        if output.type != "message":
            continue
        if output.status != "completed":
            raise OpenAIIncompleteResponse(
                diagnostic_context="message_status",
                diagnostic_kind=output.status,
            )
        for content in output.content:
            if content.type == "refusal":
                raise OpenAIRefusal()
    text = response.output_text
    if not text:
        raise OpenAIInvalidOutput(
            diagnostic_context="structured_output",
            diagnostic_kind="missing",
        )
    return schema.model_validate_json(text)


def _validation_error(
    error: ValidationError,
    schema: type[BaseModel],
) -> OpenAIInvalidOutput:
    """Reduce Pydantic details to a content-free field and stable error type."""

    details = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    if not details:
        return OpenAIInvalidOutput(
            diagnostic_context="structured_output",
            diagnostic_kind="validation_error",
        )
    first = details[0]
    location = first.get("loc", ())
    allowed_fields = frozenset(schema.model_fields)
    field = next(
        (
            component
            for component in location
            if isinstance(component, str) and component in allowed_fields
        ),
        None,
    )
    raw_kind = first.get("type")
    kind = (
        raw_kind
        if isinstance(raw_kind, str)
        and 1 <= len(raw_kind) <= 64
        and raw_kind.isascii()
        and all(
            character.islower() or character.isdigit() or character in "_."
            for character in raw_kind
        )
        else "validation_error"
    )
    return OpenAIInvalidOutput(
        diagnostic_context=(
            f"structured_output.{field}" if field is not None else "structured_output"
        ),
        diagnostic_kind=kind,
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((monotonic() - started) * 1_000))
