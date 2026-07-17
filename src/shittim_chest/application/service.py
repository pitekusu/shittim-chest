"""Application orchestration independent of Discord, OpenAI, and AWS SDKs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import replace
from typing import TypeVar

from shittim_chest.application.errors import (
    DebateNotFound,
    InvalidApplicationOperation,
    RequestNotAllowed,
    RequiredEvidenceUnavailable,
    RuntimeNotReady,
)
from shittim_chest.application.models import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    CancelDebateCommand,
    CancelledDebate,
    DebateSnapshot,
    MetricEvent,
    RetryDebateCommand,
)
from shittim_chest.application.ports import (
    CandidateOrderer,
    Clock,
    DebateRepository,
    DiscordGateway,
    EvidenceService,
    IdGenerator,
    Metrics,
    OpenAIService,
    RepositoryConflict,
)
from shittim_chest.domain import (
    PARTICIPANTS,
    DebateId,
    DebatePhase,
    DebateState,
    EvidenceBundle,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
    Vote,
    assess_escalation,
    select_winner,
)

_T = TypeVar("_T")


class _PhaseDeadlineExceeded(Exception):
    """Internal marker separating a phase timeout from the session deadline."""


class DebateApplication:
    """Coordinate deterministic debate use cases through injected Protocols."""

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdGenerator,
        metrics: Metrics,
        discord: DiscordGateway,
        evidence: EvidenceService,
        openai: OpenAIService,
        repository: DebateRepository,
        candidate_orderer: CandidateOrderer,
        lease_owner: str,
        session_timeout_seconds: float = 300.0,
        phase_timeout_seconds: float = 60.0,
        lease_renewal_seconds: float = 20.0,
    ) -> None:
        if session_timeout_seconds <= 0 or phase_timeout_seconds <= 0 or lease_renewal_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if not lease_owner.strip():
            raise ValueError("lease owner must not be empty")
        self._clock = clock
        self._ids = ids
        self._metrics = metrics
        self._discord = discord
        self._evidence = evidence
        self._openai = openai
        self._repository = repository
        self._candidate_orderer = candidate_orderer
        self._lease_owner = lease_owner
        self._session_timeout_seconds = session_timeout_seconds
        self._phase_timeout_seconds = phase_timeout_seconds
        self._lease_renewal_seconds = lease_renewal_seconds

    async def accept_debate(self, request: AcceptDebateRequest) -> AcceptedDebate:
        """Validate readiness and atomically persist a new accepted debate."""

        existing = await self._repository.get_operation_result(request.operation_id)
        if existing is not None:
            if (
                existing.requester_id != request.requester_id
                or existing.guild_id != request.guild_id
                or existing.channel_id != request.channel_id
                or existing.question != request.question
            ):
                raise InvalidApplicationOperation("operation ID is bound to another request")
            return AcceptedDebate(existing.state.debate_id, existing.state.attempt_id)

        if not await self._discord.all_identities_ready():
            raise RuntimeNotReady("all four Discord identities must be ready")
        if not await self._discord.request_is_allowed(request):
            raise RequestNotAllowed("the configured Guild/channel policy rejected the request")

        debate_id = self._ids.new_debate_id()
        attempt_id = self._ids.new_attempt_id()
        now = self._clock.now()
        snapshot = DebateSnapshot(
            state=DebateState.accepted(debate_id, attempt_id, at=now),
            question=request.question,
            requester_id=request.requester_id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            created_at=now,
            attempt_created_at=now,
        )
        persisted = await self._repository.create(
            snapshot,
            operation_id=request.operation_id,
            lease_owner=self._lease_owner,
        )
        self._metrics.increment(MetricEvent.ACCEPTED, debate_id=persisted.state.debate_id)
        return AcceptedDebate(
            debate_id=persisted.state.debate_id,
            attempt_id=persisted.state.attempt_id,
        )

    async def run_debate(self, debate_id: DebateId) -> None:
        """Run or continue one debate until it reaches a terminal state."""

        await self._require_snapshot(debate_id)
        try:
            async with asyncio.timeout(self._session_timeout_seconds):
                await self._run_with_lease_heartbeat(debate_id)
        except asyncio.CancelledError:
            await self._checkpoint_current(debate_id)
            raise
        except _PhaseDeadlineExceeded:
            await self._fail_current(debate_id, error_code="phase_deadline_exceeded")
        except TimeoutError:
            await self._fail_current(debate_id, error_code="session_deadline_exceeded")
        except RequiredEvidenceUnavailable:
            await self._fail_current(debate_id, error_code=RequiredEvidenceUnavailable.code)
        except Exception:
            await self._fail_current(debate_id, error_code="phase_failed")

    async def _run_with_lease_heartbeat(self, debate_id: DebateId) -> None:
        phase_task = asyncio.create_task(self._run_phases(debate_id), name=f"phases:{debate_id}")
        heartbeat_task = asyncio.create_task(
            self._renew_lease_until_stopped(debate_id),
            name=f"lease:{debate_id}",
        )
        try:
            done, _ = await asyncio.wait(
                (phase_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                phase_task.cancel()
                await asyncio.gather(phase_task, return_exceptions=True)
                await heartbeat_task
            else:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                await phase_task
        finally:
            for task in (phase_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(phase_task, heartbeat_task, return_exceptions=True)

    async def _renew_lease_until_stopped(self, debate_id: DebateId) -> None:
        while True:
            await asyncio.sleep(self._lease_renewal_seconds)
            snapshot = await self._require_snapshot(debate_id)
            if snapshot.state.phase.is_terminal:
                return
            try:
                await self._repository.renew_lease(expected=snapshot, at=self._clock.now())
            except RepositoryConflict:
                current = await self._require_snapshot(debate_id)
                if current.state.phase.is_terminal:
                    return
                raise

    async def cancel_debate(self, command: CancelDebateCommand) -> CancelledDebate:
        """Conditionally move an active debate to the cancelled terminal state."""

        operation_result = await self._repository.get_operation_result(command.operation_id)
        snapshot = operation_result or await self._require_snapshot(command.debate_id)
        if snapshot.state.debate_id != command.debate_id:
            raise InvalidApplicationOperation("operation ID is bound to another debate")
        self._authorize_actor(snapshot, command.actor_id, command.can_manage_messages)
        if snapshot.state.phase is DebatePhase.CANCELLED:
            return CancelledDebate(command.debate_id, snapshot.state.attempt_id)
        if snapshot.state.phase.is_terminal:
            raise InvalidApplicationOperation("only an active debate may be cancelled")

        updated = replace(
            snapshot,
            state=snapshot.state.transition_to(DebatePhase.CANCELLED, at=self._clock.now()),
        )
        persisted = await self._repository.replace(
            expected=snapshot,
            updated=updated,
            operation_id=command.operation_id,
        )
        self._metrics.increment(MetricEvent.CANCELLED, debate_id=command.debate_id)
        return CancelledDebate(command.debate_id, persisted.state.attempt_id)

    async def retry_debate(self, command: RetryDebateCommand) -> AcceptedRetry:
        """Create a new immutable attempt from a failed attempt."""

        operation_result = await self._repository.get_operation_result(command.operation_id)
        if operation_result is not None:
            if operation_result.state.debate_id != command.debate_id:
                raise InvalidApplicationOperation("operation ID is bound to another debate")
            self._authorize_actor(
                operation_result,
                command.actor_id,
                command.can_manage_messages,
            )
            if operation_result.state.retry_of is None:
                raise InvalidApplicationOperation("operation result is not a retry")
            return AcceptedRetry(
                debate_id=command.debate_id,
                attempt_id=operation_result.state.attempt_id,
                retry_of=operation_result.state.retry_of,
            )

        failed = await self._require_snapshot(command.debate_id)
        self._authorize_actor(failed, command.actor_id, command.can_manage_messages)
        if failed.state.phase is not DebatePhase.FAILED:
            raise InvalidApplicationOperation("only a failed debate may be retried")

        attempt_id = self._ids.new_attempt_id()
        retry_state = failed.state.new_retry_attempt(attempt_id, at=self._clock.now())
        retry = replace(
            failed,
            state=retry_state,
            attempt_created_at=retry_state.updated_at,
            lease=None,
            error_code=None,
            final_decision=None,
        )
        persisted = await self._repository.create_retry(
            expected_failed=failed,
            retry=retry,
            operation_id=command.operation_id,
            lease_owner=self._lease_owner,
        )
        self._metrics.increment(MetricEvent.RETRIED, debate_id=command.debate_id)
        if persisted.state.retry_of is None:
            raise InvalidApplicationOperation("persisted retry is missing its source attempt")
        return AcceptedRetry(
            debate_id=command.debate_id,
            attempt_id=persisted.state.attempt_id,
            retry_of=persisted.state.retry_of,
        )

    async def resume_recoverable(self) -> None:
        """Resume every repository-selected recoverable debate with owned tasks."""

        snapshots = await self._repository.claim_recoverable(
            lease_owner=self._lease_owner,
            at=self._clock.now(),
        )
        async with asyncio.TaskGroup() as task_group:
            for snapshot in snapshots:
                debate_id = snapshot.state.debate_id
                task_group.create_task(self.run_debate(debate_id), name=f"resume:{debate_id}")

    async def _run_phases(self, debate_id: DebateId) -> None:
        while True:
            snapshot = await self._require_snapshot(debate_id)
            if snapshot.state.phase.is_terminal:
                return
            if snapshot.state.recovery_state is RecoveryState.CHECKPOINTED:
                snapshot = await self._replace_state(
                    snapshot,
                    snapshot.state.resume(at=self._clock.now()),
                    metric_event=MetricEvent.RESUMED,
                )

            phase = snapshot.state.phase
            if phase is DebatePhase.ACCEPTED:
                await self._advance(snapshot, DebatePhase.PREPARING_EVIDENCE)
            elif phase is DebatePhase.PREPARING_EVIDENCE:
                await self._prepare_evidence(snapshot)
            elif phase is DebatePhase.COLLECTING_INITIAL_OPINIONS:
                await self._collect_initial_opinions(snapshot)
            elif phase is DebatePhase.DISCUSSING:
                await self._advance(snapshot, DebatePhase.COLLECTING_FINAL_PROPOSALS)
            elif phase is DebatePhase.COLLECTING_FINAL_PROPOSALS:
                await self._collect_final_proposals(snapshot)
            elif phase is DebatePhase.SELECTING_WINNER:
                await self._collect_votes(snapshot)
            elif phase is DebatePhase.GENERATING_DECISION:
                await self._generate_decision(snapshot)
            else:
                raise InvalidApplicationOperation(f"unsupported active phase: {phase.value}")

    async def _prepare_evidence(self, snapshot: DebateSnapshot) -> None:
        evidence = snapshot.evidence
        if evidence is None:
            evidence = await self._within_phase(
                self._evidence.prepare_evidence(question=snapshot.question)
            )
        await self._replace_with_phase(
            replace(snapshot, evidence=evidence),
            expected=snapshot,
            target=DebatePhase.COLLECTING_INITIAL_OPINIONS,
        )

    async def _collect_initial_opinions(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        opinions = snapshot.initial_opinions
        if not opinions:
            opinions = await self._within_phase(
                self._generate_initial_opinions(snapshot.question, evidence)
            )
        _validate_participant_outputs(opinions, label="initial opinions")
        await self._replace_with_phase(
            replace(snapshot, initial_opinions=opinions),
            expected=snapshot,
            target=DebatePhase.DISCUSSING,
        )

    async def _collect_final_proposals(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        _validate_participant_outputs(snapshot.initial_opinions, label="initial opinions")
        proposals = snapshot.final_proposals
        if not proposals:
            proposals = await self._within_phase(
                self._generate_final_proposals(
                    snapshot.question,
                    evidence,
                    snapshot.initial_opinions,
                )
            )
        _validate_participant_outputs(proposals, label="final proposals")
        await self._replace_with_phase(
            replace(snapshot, final_proposals=proposals),
            expected=snapshot,
            target=DebatePhase.SELECTING_WINNER,
        )

    async def _collect_votes(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        _validate_participant_outputs(snapshot.final_proposals, label="final proposals")
        votes = snapshot.votes
        if not votes:
            votes = await self._within_phase(
                self._generate_votes(snapshot.question, evidence, snapshot.final_proposals)
            )
        voting_result = select_winner(votes)
        assessment = snapshot.escalation_assessment or assess_escalation(
            voting_result,
            assessed_at=self._clock.now(),
        )
        await self._replace_with_phase(
            replace(
                snapshot,
                votes=voting_result.votes,
                escalation_assessment=assessment,
            ),
            expected=snapshot,
            target=DebatePhase.GENERATING_DECISION,
        )

    async def _generate_decision(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        voting_result = select_winner(snapshot.votes)
        decision = snapshot.final_decision
        if decision is None:
            decision = await self._within_phase(
                self._openai.generate_decision(
                    question=snapshot.question,
                    evidence=evidence,
                    proposals=snapshot.final_proposals,
                    voting_result=voting_result,
                )
            )
        if decision.winner is not voting_result.winner:
            raise ValueError("generated decision must preserve the mechanically selected winner")
        completed = await self._replace_with_phase(
            replace(snapshot, final_decision=decision),
            expected=snapshot,
            target=DebatePhase.COMPLETED,
        )
        if completed.state.phase is DebatePhase.COMPLETED:
            self._metrics.increment(MetricEvent.COMPLETED, debate_id=snapshot.state.debate_id)

    async def _generate_initial_opinions(
        self,
        question: str,
        evidence: EvidenceBundle,
    ) -> tuple[InitialOpinion, ...]:
        tasks: dict[ParticipantSlot, asyncio.Task[InitialOpinion]] = {}
        async with asyncio.TaskGroup() as task_group:
            for participant in PARTICIPANTS:
                tasks[participant] = task_group.create_task(
                    self._openai.generate_initial_opinion(
                        participant=participant,
                        question=question,
                        evidence=evidence,
                    ),
                    name=f"initial:{participant.value}",
                )
        return tuple(tasks[participant].result() for participant in PARTICIPANTS)

    async def _generate_final_proposals(
        self,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
    ) -> tuple[FinalProposal, ...]:
        tasks: dict[ParticipantSlot, asyncio.Task[FinalProposal]] = {}
        async with asyncio.TaskGroup() as task_group:
            for participant in PARTICIPANTS:
                tasks[participant] = task_group.create_task(
                    self._openai.generate_final_proposal(
                        participant=participant,
                        question=question,
                        evidence=evidence,
                        initial_opinions=initial_opinions,
                    ),
                    name=f"proposal:{participant.value}",
                )
        return tuple(tasks[participant].result() for participant in PARTICIPANTS)

    async def _generate_votes(
        self,
        question: str,
        evidence: EvidenceBundle,
        proposals: tuple[FinalProposal, ...],
    ) -> tuple[Vote, ...]:
        ordered_candidates = {
            voter: self._candidate_orderer.order_candidates(
                voter=voter,
                candidates=tuple(
                    proposal for proposal in proposals if proposal.participant is not voter
                ),
            )
            for voter in PARTICIPANTS
        }
        for voter, candidates in ordered_candidates.items():
            if len(candidates) != 2 or {candidate.participant for candidate in candidates} != set(
                PARTICIPANTS
            ) - {voter}:
                raise ValueError("candidate orderer must preserve both eligible candidates")

        tasks: dict[ParticipantSlot, asyncio.Task[Vote]] = {}
        async with asyncio.TaskGroup() as task_group:
            for voter in PARTICIPANTS:
                tasks[voter] = task_group.create_task(
                    self._openai.cast_vote(
                        voter=voter,
                        question=question,
                        evidence=evidence,
                        candidates=ordered_candidates[voter],
                    ),
                    name=f"vote:{voter.value}",
                )
        votes = tuple(tasks[voter].result() for voter in PARTICIPANTS)
        if any(
            vote.voter is not expected for vote, expected in zip(votes, PARTICIPANTS, strict=True)
        ):
            raise ValueError("vote response voter does not match the requested participant")
        return votes

    async def _within_phase(self, operation: Awaitable[_T]) -> _T:
        try:
            async with asyncio.timeout(self._phase_timeout_seconds):
                return await operation
        except TimeoutError as error:
            raise _PhaseDeadlineExceeded from error

    async def _advance(
        self,
        snapshot: DebateSnapshot,
        target: DebatePhase,
    ) -> DebateSnapshot:
        return await self._replace_state(
            snapshot,
            snapshot.state.transition_to(target, at=self._clock.now()),
        )

    async def _replace_with_phase(
        self,
        updated: DebateSnapshot,
        *,
        expected: DebateSnapshot,
        target: DebatePhase,
    ) -> DebateSnapshot:
        return await self._replace_snapshot(
            expected=expected,
            updated=replace(
                updated,
                state=expected.state.transition_to(target, at=self._clock.now()),
            ),
        )

    async def _replace_state(
        self,
        snapshot: DebateSnapshot,
        state: DebateState,
        *,
        metric_event: MetricEvent = MetricEvent.PHASE_COMPLETED,
    ) -> DebateSnapshot:
        updated = replace(snapshot, state=state)
        return await self._replace_snapshot(
            expected=snapshot,
            updated=updated,
            metric_event=metric_event,
        )

    async def _replace_snapshot(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        metric_event: MetricEvent = MetricEvent.PHASE_COMPLETED,
    ) -> DebateSnapshot:
        try:
            persisted = await self._repository.replace(expected=expected, updated=updated)
        except RepositoryConflict:
            current = await self._require_snapshot(expected.state.debate_id)
            if current.state.phase.is_terminal:
                return current
            raise
        self._metrics.increment(
            metric_event,
            debate_id=updated.state.debate_id,
        )
        return persisted

    async def _checkpoint_current(self, debate_id: DebateId) -> None:
        current = await self._require_snapshot(debate_id)
        if (
            current.state.phase.is_terminal
            or current.state.recovery_state is RecoveryState.CHECKPOINTED
        ):
            return
        checkpointed = replace(current, state=current.state.checkpoint(at=self._clock.now()))
        try:
            await self._repository.replace(expected=current, updated=checkpointed)
        except RepositoryConflict:
            return
        self._metrics.increment(MetricEvent.CHECKPOINTED, debate_id=debate_id)

    async def _fail_current(self, debate_id: DebateId, *, error_code: str) -> None:
        current = await self._require_snapshot(debate_id)
        if current.state.phase.is_terminal:
            return
        failed = replace(
            current,
            state=current.state.transition_to(DebatePhase.FAILED, at=self._clock.now()),
            error_code=error_code,
        )
        try:
            await self._repository.replace(expected=current, updated=failed)
        except RepositoryConflict:
            return
        self._metrics.increment(MetricEvent.FAILED, debate_id=debate_id)

    async def _require_snapshot(self, debate_id: DebateId) -> DebateSnapshot:
        snapshot = await self._repository.get(debate_id)
        if snapshot is None:
            raise DebateNotFound(f"debate not found: {debate_id}")
        return snapshot

    @staticmethod
    def _require_evidence(snapshot: DebateSnapshot) -> EvidenceBundle:
        if snapshot.evidence is None:
            raise InvalidApplicationOperation("the evidence phase has not completed")
        return snapshot.evidence

    @staticmethod
    def _authorize_actor(
        snapshot: DebateSnapshot,
        actor_id: str,
        can_manage_messages: bool,
    ) -> None:
        if actor_id != snapshot.requester_id and not can_manage_messages:
            raise RequestNotAllowed("only the requester or a moderator may perform this operation")


def _validate_participant_outputs(
    outputs: tuple[InitialOpinion, ...] | tuple[FinalProposal, ...],
    *,
    label: str,
) -> None:
    if len(outputs) != len(PARTICIPANTS):
        raise ValueError(f"{label} must contain exactly one item per participant")
    if {output.participant for output in outputs} != set(PARTICIPANTS):
        raise ValueError(f"{label} contain a duplicate or unknown participant")
