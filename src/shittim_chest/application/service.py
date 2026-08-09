"""Application orchestration independent of Discord, OpenAI, and AWS SDKs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TypeVar

from shittim_chest.application.discord import (
    DiscordBotSlot,
    prepare_final_proposal_outbox_operations,
    prepare_initial_opinion_outbox_operations,
    prepare_terminal_outbox_operations,
)
from shittim_chest.application.errors import (
    DebateNotFound,
    GenerationProviderError,
    InvalidApplicationOperation,
    OutboxRecoveryAbandoned,
    OutboxRecoveryFailed,
    RequestNotAllowed,
    RequiredEvidenceUnavailable,
    RuntimeNotReady,
)
from shittim_chest.application.models import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    BindDiscordContextCommand,
    BoundDiscordContext,
    CancelDebateCommand,
    CancelledDebate,
    DebateSnapshot,
    DeliveryAbandonReason,
    GenerationCheckpoint,
    GenerationStatus,
    LeaseGrant,
    MetricEvent,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
    RetryDebateCommand,
)
from shittim_chest.application.ports import (
    CandidateOrderer,
    Clock,
    DebateRepository,
    DiscordGateway,
    DiscordOutboxDrainer,
    EvidenceService,
    IdGenerator,
    Metrics,
    OpenAIService,
    RepositoryClaimLost,
    RepositoryConflict,
    RepositoryTransactionConflict,
)
from shittim_chest.application.scale_to_zero import IngressClaimFence, IngressKind
from shittim_chest.domain import (
    PARTICIPANTS,
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
    Vote,
    assess_escalation,
    select_winner,
)

_T = TypeVar("_T")

DEFAULT_TERMINAL_DELIVERY_CONFLICT_RETRIES = 3
DEFAULT_TERMINAL_DELIVERY_CONFLICT_RETRY_SECONDS = 1.0
_LOGGER = logging.getLogger("shittim_chest")


class _PhaseDeadlineExceeded(Exception):
    """Internal marker separating a phase timeout from the session deadline."""


class _TerminalDeliveryConflictExhausted(RuntimeError):
    """Signal that bounded terminal-delivery conflict recovery was exhausted."""


class _TerminalDeliveryConflict(RuntimeError):
    """Content-free terminal-delivery conflict with an explicit retry decision."""

    __slots__ = ("actions", "codes", "reasons_complete", "retryable", "stage")

    def __init__(
        self,
        *,
        stage: str,
        actions: tuple[str, ...],
        codes: tuple[str, ...],
        reasons_complete: bool,
        retryable: bool,
    ) -> None:
        self.stage = stage
        self.actions = actions
        self.codes = codes
        self.reasons_complete = reasons_complete
        self.retryable = retryable
        super().__init__("terminal delivery conflict")


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
        outbox_recovery: DiscordOutboxDrainer,
        lease_owner: str,
        session_timeout_seconds: float = 300.0,
        phase_timeout_seconds: float = 60.0,
        lease_renewal_seconds: float = 20.0,
        terminal_delivery_conflict_retries: int = DEFAULT_TERMINAL_DELIVERY_CONFLICT_RETRIES,
        terminal_delivery_conflict_retry_seconds: float = (
            DEFAULT_TERMINAL_DELIVERY_CONFLICT_RETRY_SECONDS
        ),
    ) -> None:
        if session_timeout_seconds <= 0 or phase_timeout_seconds <= 0 or lease_renewal_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if (
            isinstance(terminal_delivery_conflict_retries, bool)
            or not isinstance(terminal_delivery_conflict_retries, int)
            or not 1 <= terminal_delivery_conflict_retries <= 10
        ):
            raise ValueError("terminal delivery conflict retries must be between 1 and 10")
        if terminal_delivery_conflict_retry_seconds < 0:
            raise ValueError("terminal delivery conflict retry delay must not be negative")
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
        self._outbox_recovery = outbox_recovery
        self._lease_owner = lease_owner
        self._session_timeout_seconds = session_timeout_seconds
        self._phase_timeout_seconds = phase_timeout_seconds
        self._lease_renewal_seconds = lease_renewal_seconds
        self._terminal_delivery_conflict_retries = terminal_delivery_conflict_retries
        self._terminal_delivery_conflict_retry_seconds = terminal_delivery_conflict_retry_seconds

    async def accept_debate(
        self,
        request: AcceptDebateRequest,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedDebate:
        """Validate readiness and atomically persist a new accepted debate."""

        replay_claim = (
            None if ingress_claim is None else _claim_for_write(ingress_claim, at=self._clock.now())
        )
        existing = await self._repository.get_operation_result(
            request.operation_id,
            ingress_claim=replay_claim,
        )
        if existing is not None:
            if (
                existing.requester_id != request.requester_id
                or existing.requester_username != request.requester_username
                or existing.requester_display_name != request.requester_display_name
                or existing.guild_id != request.guild_id
                or existing.channel_id != request.channel_id
                or existing.question != request.question
            ):
                raise InvalidApplicationOperation("operation ID is bound to another request")
            existing = await self._reclaim_ingress_replay(existing, ingress_claim=ingress_claim)
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
            requester_username=request.requester_username,
            requester_display_name=request.requester_display_name,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            created_at=now,
            attempt_created_at=now,
            origin_ingress_interaction_id=(
                None if ingress_claim is None else ingress_claim.interaction_id
            ),
        )
        persisted = await self._repository.create(
            snapshot,
            operation_id=request.operation_id,
            lease_owner=self._lease_owner,
            ingress_claim=_claim_for_write(ingress_claim, at=now),
        )
        self._metrics.increment(MetricEvent.ACCEPTED, debate_id=persisted.state.debate_id)
        return AcceptedDebate(
            debate_id=persisted.state.debate_id,
            attempt_id=persisted.state.attempt_id,
        )

    async def run_debate(self, debate_id: DebateId) -> None:
        """Run or continue one debate until it reaches a terminal state."""

        try:
            snapshot = await self._require_snapshot(debate_id)
            self._require_owned_active_lease(snapshot, at=self._clock.now())
            await self._run_with_lease_heartbeat(debate_id)
        except asyncio.CancelledError:
            await self._checkpoint_current(debate_id)
            raise
        except RepositoryConflict:
            return
        except OutboxRecoveryFailed:
            # Required delivery remains durable and recoverable.  Releasing the
            # lease here would incorrectly make the attempt look complete.
            return

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot:
        """Return the current persisted snapshot through an application read boundary."""

        return await self._require_snapshot(debate_id)

    async def bind_discord_context(
        self,
        command: BindDiscordContextCommand,
    ) -> BoundDiscordContext:
        """Conditionally bind starter, thread, and panel IDs without allowing rebinding."""

        snapshot = await self._require_snapshot(command.debate_id)
        requested = (
            command.starter_message_id,
            command.thread_id,
            command.control_panel_message_id,
        )
        existing = (
            snapshot.starter_message_id,
            snapshot.thread_id,
            snapshot.control_panel_message_id,
        )
        if existing == requested:
            return BoundDiscordContext(command.debate_id, *requested)
        if any(value is not None for value in existing):
            raise InvalidApplicationOperation("Discord context is already bound")
        if snapshot.state.phase is not DebatePhase.ACCEPTED:
            raise InvalidApplicationOperation("Discord context must be bound before debate work")
        now = self._clock.now()
        self._require_owned_active_lease(snapshot, at=now)

        updated = replace(
            snapshot,
            starter_message_id=command.starter_message_id,
            thread_id=command.thread_id,
            control_panel_message_id=command.control_panel_message_id,
        )
        persisted = await self._repository.replace(expected=snapshot, updated=updated)
        if (
            persisted.starter_message_id is None
            or persisted.thread_id is None
            or persisted.control_panel_message_id is None
        ):
            raise InvalidApplicationOperation("repository did not preserve Discord context")
        return BoundDiscordContext(
            debate_id=command.debate_id,
            starter_message_id=persisted.starter_message_id,
            thread_id=persisted.thread_id,
            control_panel_message_id=persisted.control_panel_message_id,
        )

    async def _run_with_lease_heartbeat(self, debate_id: DebateId) -> None:
        phase_task = asyncio.create_task(
            self._run_owned_attempt(debate_id),
            name=f"phases:{debate_id}",
        )
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

    async def _run_owned_attempt(self, debate_id: DebateId) -> None:
        """Keep failure delivery under the same live lease heartbeat as phase work."""

        try:
            await self._recover_outbox_then_run_phases(debate_id)
        except asyncio.CancelledError:
            raise
        except _TerminalDeliveryConflictExhausted, RepositoryConflict:
            raise
        except _PhaseDeadlineExceeded:
            await self._fail_current(debate_id, error_code="phase_deadline_exceeded")
        except TimeoutError:
            await self._fail_current(debate_id, error_code="session_deadline_exceeded")
        except RequiredEvidenceUnavailable:
            await self._fail_current(
                debate_id,
                error_code=RequiredEvidenceUnavailable.code,
            )
        except OutboxRecoveryFailed as error:
            await self._fail_current(debate_id, error_code=error.delivery_code)
        except Exception:
            await self._fail_current(debate_id, error_code="phase_failed")

    async def _finish_terminal_delivery_with_conflict_retry(
        self,
        snapshot: DebateSnapshot,
    ) -> None:
        """Bound retries to a single staged terminal plan owned by this process."""

        delivery = snapshot.terminal_delivery
        if delivery is None:
            raise InvalidApplicationOperation("terminal delivery is not staged")
        current = snapshot
        for retry in range(self._terminal_delivery_conflict_retries + 1):
            try:
                await self._finish_terminal_delivery(current)
                return
            except _TerminalDeliveryConflict as error:
                will_retry = error.retryable and retry < self._terminal_delivery_conflict_retries
                _log_terminal_delivery_conflict(
                    snapshot.state.debate_id,
                    error,
                    retry_number=retry + 1 if will_retry else None,
                    retry_delay_seconds=(
                        self._terminal_delivery_conflict_retry_seconds if will_retry else None
                    ),
                )
                if not will_retry:
                    raise _TerminalDeliveryConflictExhausted(
                        "terminal delivery conflicts exceeded the bounded retry limit "
                        "or were not retryable"
                    ) from error
            current = await self._require_snapshot(snapshot.state.debate_id)
            if current.state.phase.is_terminal:
                return
            if current.terminal_delivery != delivery:
                raise _TerminalDeliveryConflictExhausted(
                    "terminal delivery changed during bounded conflict recovery"
                )
            self._require_owned_active_lease(current, at=self._clock.now())
            self._metrics.increment(
                MetricEvent.TERMINAL_DELIVERY_CONFLICT_RETRY,
                debate_id=current.state.debate_id,
            )
            await asyncio.sleep(self._terminal_delivery_conflict_retry_seconds)

    async def _recover_outbox_then_run_phases(self, debate_id: DebateId) -> None:
        recovered_legacy_outbox = False
        while True:
            snapshot = await self._require_snapshot(debate_id)
            if snapshot.state.phase.is_terminal:
                return
            self._require_owned_active_lease(snapshot, at=self._clock.now())
            if snapshot.terminal_delivery is not None:
                await self._finish_terminal_delivery_with_conflict_retry(snapshot)
                continue
            if not recovered_legacy_outbox:
                await self._outbox_recovery.drain(expected=snapshot)
                recovered_legacy_outbox = True
            async with asyncio.timeout(self._session_timeout_seconds):
                await self._run_phases(debate_id)

    async def _renew_lease_until_stopped(self, debate_id: DebateId) -> None:
        while True:
            await asyncio.sleep(self._lease_renewal_seconds)
            snapshot = await self._require_snapshot(debate_id)
            if snapshot.state.phase.is_terminal:
                return
            now = self._clock.now()
            self._require_owned_active_lease(snapshot, at=now)
            try:
                await self._repository.renew_lease(expected=snapshot, at=now)
            except RepositoryConflict:
                current = await self._require_snapshot(debate_id)
                if current.state.phase.is_terminal:
                    return
                raise

    async def cancel_debate(
        self,
        command: CancelDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> CancelledDebate:
        """Conditionally move an active debate to the cancelled terminal state."""

        replay_claim = (
            None if ingress_claim is None else _claim_for_write(ingress_claim, at=self._clock.now())
        )
        operation_result = await self._repository.get_operation_result(
            command.operation_id,
            ingress_claim=replay_claim,
        )
        snapshot = operation_result or await self._require_snapshot(command.debate_id)
        if snapshot.state.debate_id != command.debate_id:
            raise InvalidApplicationOperation("operation ID is bound to another debate")
        if (
            command.expected_attempt_id is not None
            and snapshot.state.attempt_id != command.expected_attempt_id
        ):
            raise InvalidApplicationOperation("panel operation is bound to another attempt")
        self._authorize_actor(snapshot, command.actor_id, command.can_manage_messages)
        if snapshot.state.phase is DebatePhase.CANCELLED or (
            snapshot.terminal_delivery is not None
            and snapshot.terminal_delivery.target_phase is DebatePhase.CANCELLED
        ):
            return CancelledDebate(command.debate_id, snapshot.state.attempt_id)
        if snapshot.state.phase.is_terminal:
            raise InvalidApplicationOperation("only an active debate may be cancelled")

        now = self._clock.now()
        self._require_owned_active_lease(snapshot, at=now)
        if snapshot.state.recovery_state is RecoveryState.CHECKPOINTED:
            snapshot = await self._replace_state(
                snapshot,
                snapshot.state.resume(at=now),
                metric_event=MetricEvent.RESUMED,
            )
            now = self._clock.now()
            self._require_owned_active_lease(snapshot, at=now)
        if snapshot.thread_id is not None:
            if isinstance(snapshot.terminal_delivery, PhaseDeliveryPlan):
                plan = snapshot.terminal_delivery
                if plan.status is PhaseDeliveryStatus.STAGED:
                    snapshot = await self._repository.terminate_terminal_delivery(
                        expected=snapshot,
                        at=now,
                        reason=DeliveryAbandonReason.CANCELLED,
                    )
                if snapshot.terminal_delivery is None or not isinstance(
                    snapshot.terminal_delivery,
                    PhaseDeliveryPlan,
                ):
                    raise RepositoryConflict("terminal cancellation lost its phase plan")
                if snapshot.terminal_delivery.status is PhaseDeliveryStatus.TERMINATING:
                    await self._outbox_recovery.terminate(expected=snapshot)
                    current = await self._require_snapshot(snapshot.state.debate_id)
                    snapshot = await self._repository.abandon_terminal_delivery(
                        expected=current,
                        at=self._clock.now(),
                        reason=DeliveryAbandonReason.CANCELLED,
                    )
            staged_at = self._clock.now()
            persisted = await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.CANCELLED,
                operation_id=command.operation_id,
                ingress_claim=_claim_for_write(ingress_claim, at=staged_at),
                at=staged_at,
            )
            return CancelledDebate(command.debate_id, persisted.state.attempt_id)

        # A context-creation failure can cancel a NEW attempt before a thread
        # exists.  Its public status message is the durable user notification;
        # a thread Outbox operation cannot be constructed in this state.
        updated = replace(
            snapshot,
            state=snapshot.state.transition_to(DebatePhase.CANCELLED, at=now),
        )
        persisted = await self._repository.replace(
            expected=snapshot,
            updated=updated,
            operation_id=command.operation_id,
            ingress_claim=_claim_for_write(ingress_claim, at=now),
        )
        self._metrics.increment(MetricEvent.CANCELLED, debate_id=command.debate_id)
        return CancelledDebate(command.debate_id, persisted.state.attempt_id)

    async def retry_debate(
        self,
        command: RetryDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedRetry:
        """Create a new immutable attempt from a failed attempt."""

        replay_claim = (
            None if ingress_claim is None else _claim_for_write(ingress_claim, at=self._clock.now())
        )
        operation_result = await self._repository.get_operation_result(
            command.operation_id,
            ingress_claim=replay_claim,
        )
        if operation_result is not None:
            if operation_result.state.debate_id != command.debate_id:
                raise InvalidApplicationOperation("operation ID is bound to another debate")
            self._authorize_actor(
                operation_result,
                command.actor_id,
                command.can_manage_messages,
            )
            retry_of = operation_result.state.retry_of
            if retry_of is None:
                raise InvalidApplicationOperation("operation result is not a retry")
            if command.expected_attempt_id is not None and retry_of != command.expected_attempt_id:
                raise InvalidApplicationOperation("panel operation is bound to another attempt")
            operation_result = await self._reclaim_ingress_replay(
                operation_result,
                ingress_claim=ingress_claim,
            )
            return AcceptedRetry(
                debate_id=command.debate_id,
                attempt_id=operation_result.state.attempt_id,
                retry_of=retry_of,
            )

        failed = await self._require_snapshot(command.debate_id)
        if (
            command.expected_attempt_id is not None
            and failed.state.attempt_id != command.expected_attempt_id
        ):
            raise InvalidApplicationOperation("panel operation is bound to another attempt")
        self._authorize_actor(failed, command.actor_id, command.can_manage_messages)
        if failed.state.phase is not DebatePhase.FAILED:
            raise InvalidApplicationOperation("only a failed debate may be retried")

        attempt_id = self._ids.new_attempt_id()
        now = self._clock.now()
        retry_state = failed.state.new_retry_attempt(attempt_id, at=now)
        retry = replace(
            failed,
            state=retry_state,
            attempt_created_at=retry_state.updated_at,
            origin_ingress_interaction_id=(
                None if ingress_claim is None else ingress_claim.interaction_id
            ),
            lease=None,
            error_code=None,
            final_decision=None,
            generation_checkpoints=(),
            terminal_delivery=None,
        )
        persisted = await self._repository.create_retry(
            expected_failed=failed,
            retry=retry,
            operation_id=command.operation_id,
            lease_owner=self._lease_owner,
            ingress_claim=_claim_for_write(ingress_claim, at=now),
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

        snapshots = await self.claim_recoverable()
        async with asyncio.TaskGroup() as task_group:
            for snapshot in snapshots:
                debate_id = snapshot.state.debate_id
                task_group.create_task(self.run_debate(debate_id), name=f"resume:{debate_id}")

    async def claim_recoverable(self) -> tuple[DebateSnapshot, ...]:
        """Claim recoverable debates without choosing a process task owner.

        Runtime composition uses this method to register every returned debate
        in its single shared task registry. The legacy ``resume_recoverable``
        wrapper remains available to non-production fixtures.
        """

        return await self._repository.claim_recoverable(
            lease_owner=self._lease_owner,
            at=self._clock.now(),
        )

    async def fail_pre_activation(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        kind: IngressKind,
        ingress_claim: IngressClaimFence,
        error_code: str,
    ) -> str:
        """Fail one admitted NEW/RETRY attempt before ingress acceptance.

        This compensation releases the execution slot but intentionally keeps
        the daily admission quota consumed.  The latter is an abuse-control
        counter for admitted attempts, not a success counter.
        """

        error_code = _validated_ingress_error_code(error_code)
        snapshot = await self._require_snapshot(debate_id)
        if (
            snapshot.state.attempt_id != attempt_id
            or snapshot.origin_ingress_interaction_id != ingress_claim.interaction_id
            or ingress_claim.kind is not kind
        ):
            raise RepositoryClaimLost("pre-activation failure is bound to another attempt")
        if snapshot.state.phase.is_terminal:
            if snapshot.state.phase is DebatePhase.FAILED and snapshot.error_code is not None:
                return snapshot.error_code
            raise RepositoryConflict("pre-activation attempt reached another terminal state")
        if (
            snapshot.terminal_delivery is not None
            and snapshot.terminal_delivery.target_phase is DebatePhase.FAILED
            and snapshot.error_code is not None
        ):
            return snapshot.error_code
        context = (
            snapshot.starter_message_id,
            snapshot.thread_id,
            snapshot.control_panel_message_id,
        )
        if kind is IngressKind.NEW_DEBATE:
            if snapshot.state.retry_of is not None or any(value is not None for value in context):
                raise RepositoryConflict("new debate context is no longer safe to compensate")
        elif kind is IngressKind.RETRY:
            if snapshot.state.retry_of is None or not all(value is not None for value in context):
                raise RepositoryConflict("retry context is no longer safe to compensate")
        else:
            raise InvalidApplicationOperation("cancel ingress cannot be compensated as a start")
        lease = snapshot.lease
        if lease is None or lease.owner_id != self._lease_owner:
            raise RepositoryConflict("pre-activation lease belongs to another runtime")
        now = self._clock.now()
        if kind is IngressKind.RETRY:
            await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.FAILED,
                error_code=error_code,
                operation_id=ingress_claim.operation_id,
                ingress_claim=_require_claim_for_write(ingress_claim, at=now),
                at=now,
            )
            return error_code

        updated = replace(
            snapshot,
            state=snapshot.state.transition_to(DebatePhase.FAILED, at=now),
            error_code=error_code,
        )
        persisted = await self._repository.fail_pre_activation(
            expected=snapshot,
            updated=updated,
            ingress_claim=_require_claim_for_write(ingress_claim, at=now),
        )
        if persisted.error_code is None:
            raise RepositoryConflict("pre-activation failure lost its error code")
        self._metrics.increment(MetricEvent.FAILED, debate_id=debate_id)
        return persisted.error_code

    async def _run_phases(self, debate_id: DebateId) -> None:
        while True:
            snapshot = await self._require_snapshot(debate_id)
            if snapshot.state.phase.is_terminal:
                return
            if snapshot.terminal_delivery is not None:
                return
            self._require_owned_active_lease(snapshot, at=self._clock.now())
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
                await self._begin_final_proposals(snapshot)
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
        at = self._clock.now()
        checkpoints = snapshot.generation_checkpoints
        if not snapshot.initial_opinions:
            checkpoints = tuple(
                sorted(
                    (
                        *snapshot.generation_checkpoints,
                        *(
                            GenerationCheckpoint.planned(
                                phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                                participant=participant,
                                at=at,
                            )
                            for participant in PARTICIPANTS
                        ),
                    ),
                    key=lambda checkpoint: (checkpoint.phase.value, checkpoint.participant.value),
                )
            )
        await self._replace_snapshot(
            expected=snapshot,
            updated=replace(
                snapshot,
                state=snapshot.state.transition_to(
                    DebatePhase.COLLECTING_INITIAL_OPINIONS,
                    at=at,
                ),
                evidence=evidence,
                generation_checkpoints=checkpoints,
            ),
        )

    async def _collect_initial_opinions(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        snapshot = await self._ensure_initial_opinion_checkpoints(snapshot)
        checkpoints = self._initial_opinion_checkpoints(snapshot)
        opinions = {opinion.participant: opinion for opinion in snapshot.initial_opinions}
        if len(opinions) != len(snapshot.initial_opinions):
            raise RepositoryConflict("initial opinion participant is duplicated")
        for participant, opinion in opinions.items():
            del opinion
            checkpoint = checkpoints.get(participant)
            if checkpoint is not None and checkpoint.status is not GenerationStatus.COMPLETED:
                raise RepositoryConflict("durable initial opinion conflicts with its generation")

        missing = tuple(participant for participant in PARTICIPANTS if participant not in opinions)
        if missing:
            thread_id = snapshot.thread_id
            if thread_id is None:
                await self._stage_terminal_delivery(
                    snapshot,
                    target=DebatePhase.FAILED,
                    error_code="discord_delivery_preflight_failed",
                )
                return
            for participant in missing:
                delivery_ready = await self._discord.delivery_target_is_ready(
                    bot_slot=DiscordBotSlot(participant.value),
                    guild_id=snapshot.guild_id,
                    thread_id=thread_id,
                )
                if not delivery_ready:
                    await self._stage_terminal_delivery(
                        snapshot,
                        target=DebatePhase.FAILED,
                        error_code="discord_delivery_preflight_failed",
                    )
                    return

            lease = snapshot.lease
            if lease is None:
                raise RepositoryConflict("initial opinion generation requires a fenced lease")
            at = self._clock.now()
            claimed_checkpoints = snapshot.generation_checkpoints
            for participant in missing:
                checkpoint = checkpoints[participant]
                if (
                    checkpoint.status is GenerationStatus.IN_FLIGHT
                    and checkpoint.logical_attempt == 2
                ):
                    failed = checkpoint.exhaust_after_recovery(
                        lease=lease,
                        at=at,
                        error_code="generation_attempts_exhausted",
                    )
                    await self._stage_terminal_delivery(
                        snapshot,
                        target=DebatePhase.FAILED,
                        error_code="generation_attempts_exhausted",
                        generation_checkpoint=failed,
                        at=at,
                    )
                    return
                claimed = checkpoint.claim(lease=lease, at=at)
                claimed_checkpoints = _generation_checkpoints_with(
                    claimed_checkpoints,
                    claimed,
                )
            claimed_snapshot = replace(
                snapshot,
                state=replace(snapshot.state, updated_at=at),
                generation_checkpoints=claimed_checkpoints,
            )
            snapshot = await self._repository.replace(
                expected=snapshot,
                updated=claimed_snapshot,
            )
            write_lock = asyncio.Lock()
            failures: dict[ParticipantSlot, str | None] = {}
            async with asyncio.TaskGroup() as task_group:
                tasks = {
                    participant: task_group.create_task(
                        self._generate_and_persist_initial_opinion(
                            snapshot=snapshot,
                            participant=participant,
                            evidence=evidence,
                            write_lock=write_lock,
                        ),
                        name=f"initial:{participant.value}",
                    )
                    for participant in missing
                }
            failures = {participant: tasks[participant].result() for participant in missing}
            error_code = next(
                (
                    failures[participant]
                    for participant in PARTICIPANTS
                    if failures.get(participant)
                ),
                None,
            )
            if error_code is not None:
                current = await self._require_snapshot(snapshot.state.debate_id)
                await self._stage_terminal_delivery(
                    current,
                    target=DebatePhase.FAILED,
                    error_code=error_code,
                )
                return
            snapshot = await self._require_snapshot(snapshot.state.debate_id)

        _validate_participant_outputs(snapshot.initial_opinions, label="initial opinions")
        current_checkpoints = self._initial_opinion_checkpoints(snapshot)
        if current_checkpoints and any(
            current_checkpoints[participant].status is not GenerationStatus.COMPLETED
            for participant in PARTICIPANTS
        ):
            raise RepositoryConflict("initial opinions are not durably generated")
        staged = await self._stage_initial_opinion_delivery(snapshot)
        if staged.terminal_delivery is None:  # pragma: no cover - repository contract
            raise RepositoryConflict("initial opinion delivery plan was not persisted")

    async def _begin_final_proposals(self, snapshot: DebateSnapshot) -> None:
        at = self._clock.now()
        checkpoints = snapshot.generation_checkpoints
        if not snapshot.final_proposals:
            checkpoints = tuple(
                sorted(
                    (
                        *checkpoints,
                        *(
                            GenerationCheckpoint.planned(
                                phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                                participant=participant,
                                at=at,
                            )
                            for participant in PARTICIPANTS
                        ),
                    ),
                    key=lambda checkpoint: (checkpoint.phase.value, checkpoint.participant.value),
                )
            )
        await self._replace_snapshot(
            expected=snapshot,
            updated=replace(
                snapshot,
                state=snapshot.state.transition_to(
                    DebatePhase.COLLECTING_FINAL_PROPOSALS,
                    at=at,
                ),
                generation_checkpoints=checkpoints,
            ),
        )

    async def _collect_final_proposals(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        _validate_participant_outputs(snapshot.initial_opinions, label="initial opinions")
        snapshot = await self._ensure_final_proposal_checkpoints(snapshot)
        checkpoints = self._final_proposal_checkpoints(snapshot)
        proposals = {proposal.participant: proposal for proposal in snapshot.final_proposals}
        if len(proposals) != len(snapshot.final_proposals):
            raise RepositoryConflict("final proposal participant is duplicated")
        for participant, proposal in proposals.items():
            del proposal
            checkpoint = checkpoints.get(participant)
            if checkpoint is not None and checkpoint.status is not GenerationStatus.COMPLETED:
                raise RepositoryConflict("durable final proposal conflicts with its generation")

        missing = tuple(participant for participant in PARTICIPANTS if participant not in proposals)
        if missing:
            thread_id = snapshot.thread_id
            if thread_id is None:
                await self._stage_terminal_delivery(
                    snapshot,
                    target=DebatePhase.FAILED,
                    error_code="discord_delivery_preflight_failed",
                )
                return
            for participant in missing:
                delivery_ready = await self._discord.delivery_target_is_ready(
                    bot_slot=DiscordBotSlot(participant.value),
                    guild_id=snapshot.guild_id,
                    thread_id=thread_id,
                )
                if not delivery_ready:
                    await self._stage_terminal_delivery(
                        snapshot,
                        target=DebatePhase.FAILED,
                        error_code="discord_delivery_preflight_failed",
                    )
                    return

            lease = snapshot.lease
            if lease is None:
                raise RepositoryConflict("final proposal generation requires a fenced lease")
            at = self._clock.now()
            claimed_checkpoints = snapshot.generation_checkpoints
            for participant in missing:
                checkpoint = checkpoints[participant]
                if (
                    checkpoint.status is GenerationStatus.IN_FLIGHT
                    and checkpoint.logical_attempt == 2
                ):
                    failed = checkpoint.exhaust_after_recovery(
                        lease=lease,
                        at=at,
                        error_code="generation_attempts_exhausted",
                    )
                    await self._stage_terminal_delivery(
                        snapshot,
                        target=DebatePhase.FAILED,
                        error_code="generation_attempts_exhausted",
                        generation_checkpoint=failed,
                        at=at,
                    )
                    return
                claimed = checkpoint.claim(lease=lease, at=at)
                claimed_checkpoints = _generation_checkpoints_with(
                    claimed_checkpoints,
                    claimed,
                )
            claimed_snapshot = replace(
                snapshot,
                state=replace(snapshot.state, updated_at=at),
                generation_checkpoints=claimed_checkpoints,
            )
            snapshot = await self._repository.replace(
                expected=snapshot,
                updated=claimed_snapshot,
            )
            write_lock = asyncio.Lock()
            async with asyncio.TaskGroup() as task_group:
                tasks = {
                    participant: task_group.create_task(
                        self._generate_and_persist_final_proposal(
                            snapshot=snapshot,
                            participant=participant,
                            evidence=evidence,
                            write_lock=write_lock,
                        ),
                        name=f"proposal:{participant.value}",
                    )
                    for participant in missing
                }
            failures = {participant: tasks[participant].result() for participant in missing}
            error_code = next(
                (
                    failures[participant]
                    for participant in PARTICIPANTS
                    if failures.get(participant)
                ),
                None,
            )
            if error_code is not None:
                current = await self._require_snapshot(snapshot.state.debate_id)
                await self._stage_terminal_delivery(
                    current,
                    target=DebatePhase.FAILED,
                    error_code=error_code,
                )
                return
            snapshot = await self._require_snapshot(snapshot.state.debate_id)

        _validate_participant_outputs(snapshot.final_proposals, label="final proposals")
        current_checkpoints = self._final_proposal_checkpoints(snapshot)
        if current_checkpoints and any(
            current_checkpoints[participant].status is not GenerationStatus.COMPLETED
            for participant in PARTICIPANTS
        ):
            raise RepositoryConflict("final proposals are not durably generated")
        staged = await self._stage_final_proposal_delivery(snapshot)
        if staged.terminal_delivery is None:  # pragma: no cover - repository contract
            raise RepositoryConflict("final proposal delivery plan was not persisted")

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
        at = self._clock.now()
        updated = replace(
            snapshot,
            state=snapshot.state.transition_to(DebatePhase.GENERATING_DECISION, at=at),
            votes=voting_result.votes,
            escalation_assessment=assessment,
        )
        if snapshot.final_decision is None:
            updated = updated.with_generation_checkpoint(
                GenerationCheckpoint.planned(
                    phase=DebatePhase.GENERATING_DECISION,
                    participant=voting_result.winner,
                    at=at,
                )
            )
        await self._replace_snapshot(expected=snapshot, updated=updated)

    async def _generate_decision(self, snapshot: DebateSnapshot) -> None:
        evidence = self._require_evidence(snapshot)
        voting_result = select_winner(snapshot.votes)
        decision = snapshot.final_decision
        decision_checkpoints = tuple(
            checkpoint
            for checkpoint in snapshot.generation_checkpoints
            if checkpoint.phase is DebatePhase.GENERATING_DECISION
        )
        if len(decision_checkpoints) > 1 or (
            decision_checkpoints and decision_checkpoints[0].participant is not voting_result.winner
        ):
            raise RepositoryConflict("generation checkpoint set conflicts with the Python winner")
        checkpoint = decision_checkpoints[0] if decision_checkpoints else None
        if decision is not None:
            if decision.winner is not voting_result.winner:
                raise RepositoryConflict("durable decision conflicts with the Python winner")
            if checkpoint is not None and checkpoint.status is not GenerationStatus.COMPLETED:
                raise RepositoryConflict("durable decision conflicts with its generation fence")
            await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.COMPLETED,
                final_decision=decision,
                generation_checkpoint=checkpoint,
            )
            return
        if checkpoint is None:
            # Existing schema-v7 attempts may have reached this phase before PR-0.
            checkpoint = GenerationCheckpoint.planned(
                phase=DebatePhase.GENERATING_DECISION,
                participant=voting_result.winner,
                at=snapshot.state.updated_at,
            )
            snapshot = await self._repository.replace(
                expected=snapshot,
                updated=snapshot.with_generation_checkpoint(checkpoint),
            )
        if checkpoint.participant is not voting_result.winner:
            raise RepositoryConflict("generation participant conflicts with Python winner")
        lease = snapshot.lease
        if lease is None:
            raise RepositoryConflict("generation requires a fenced lease")
        at = self._clock.now()
        if checkpoint.status is GenerationStatus.IN_FLIGHT and checkpoint.logical_attempt == 2:
            failed = checkpoint.exhaust_after_recovery(
                lease=lease,
                at=at,
                error_code="generation_attempts_exhausted",
            )
            await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.FAILED,
                error_code="generation_attempts_exhausted",
                generation_checkpoint=failed,
            )
            return
        thread_id = snapshot.thread_id
        delivery_ready = thread_id is not None and await self._discord.delivery_target_is_ready(
            bot_slot=DiscordBotSlot.MODERATOR,
            guild_id=snapshot.guild_id,
            thread_id=thread_id,
        )
        if not delivery_ready:
            error_code = "discord_delivery_preflight_failed"
            if checkpoint.status is GenerationStatus.PLANNED:
                failed = checkpoint.fail_before_call(at=at, error_code=error_code)
            else:
                claim_identity = (
                    checkpoint.claim_owner,
                    checkpoint.claim_slot,
                    checkpoint.claim_fencing_token,
                )
                lease_identity = (lease.owner_id, lease.slot, lease.fencing_token)
                failed = (
                    checkpoint.fail(lease=lease, at=at, error_code=error_code)
                    if claim_identity == lease_identity
                    else checkpoint.fail_before_recovery_call(
                        lease=lease,
                        at=at,
                        error_code=error_code,
                    )
                )
            await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.FAILED,
                error_code=error_code,
                generation_checkpoint=failed,
                at=at,
            )
            return
        claimed_checkpoint = checkpoint.claim(lease=lease, at=at)
        claimed = snapshot.with_generation_checkpoint(claimed_checkpoint)
        claimed = replace(
            claimed,
            state=replace(snapshot.state, updated_at=at),
        )
        snapshot = await self._repository.replace(expected=snapshot, updated=claimed)
        try:
            decision = await self._within_phase(
                self._openai.generate_decision(
                    question=snapshot.question,
                    evidence=evidence,
                    proposals=snapshot.final_proposals,
                    voting_result=voting_result,
                )
            )
        except asyncio.CancelledError:
            raise
        except GenerationProviderError as error:
            await self._stage_generation_failure(snapshot, error_code=error.code)
            return
        except _PhaseDeadlineExceeded:
            await self._stage_generation_failure(
                snapshot,
                error_code="generation_deadline_exceeded",
            )
            return
        if decision.winner is not voting_result.winner:
            await self._stage_generation_failure(
                snapshot,
                error_code="openai_winner_mismatch",
            )
            return
        current, current_checkpoint = await self._refresh_generation_claim(
            snapshot,
            claimed_checkpoint,
        )
        settled_at = self._clock.now()
        lease = current.lease
        if lease is None:  # pragma: no cover - refresh validates the lease
            raise RepositoryConflict("generation lease disappeared before settlement")
        completed_checkpoint = current_checkpoint.complete(lease=lease, at=settled_at)
        await self._stage_terminal_delivery(
            current,
            target=DebatePhase.COMPLETED,
            final_decision=decision,
            generation_checkpoint=completed_checkpoint,
            at=settled_at,
        )

    async def _stage_generation_failure(
        self,
        snapshot: DebateSnapshot,
        *,
        error_code: str,
    ) -> None:
        voting_result = select_winner(snapshot.votes)
        checkpoint = snapshot.checkpoint_for(
            phase=DebatePhase.GENERATING_DECISION,
            participant=voting_result.winner,
        )
        if checkpoint is None:
            raise RepositoryConflict("generation failure lost its durable fence")
        current, checkpoint = await self._refresh_generation_claim(snapshot, checkpoint)
        lease = current.lease
        if lease is None:  # pragma: no cover - refresh validates the lease
            raise RepositoryConflict("generation failure lost its lease")
        at = self._clock.now()
        failed = checkpoint.fail(lease=lease, at=at, error_code=error_code)
        await self._stage_terminal_delivery(
            current,
            target=DebatePhase.FAILED,
            error_code=error_code,
            generation_checkpoint=failed,
            at=at,
        )

    async def _refresh_generation_claim(
        self,
        snapshot: DebateSnapshot,
        checkpoint: GenerationCheckpoint,
    ) -> tuple[DebateSnapshot, GenerationCheckpoint]:
        """Refresh lease expiry without accepting a changed generation fence."""

        current = await self._require_snapshot(snapshot.state.debate_id)
        current_checkpoint = current.checkpoint_for(
            phase=checkpoint.phase,
            participant=checkpoint.participant,
        )
        if current_checkpoint != checkpoint:
            raise RepositoryConflict("generation result lost its exact durable claim")
        self._require_owned_active_lease(current, at=self._clock.now())
        return current, current_checkpoint

    async def _ensure_initial_opinion_checkpoints(
        self,
        snapshot: DebateSnapshot,
    ) -> DebateSnapshot:
        checkpoints = self._initial_opinion_checkpoints(snapshot)
        if checkpoints:
            if set(checkpoints) != set(PARTICIPANTS):
                raise RepositoryConflict("initial opinion generation checkpoint set is incomplete")
            return snapshot
        if snapshot.initial_opinions:
            if len(snapshot.initial_opinions) != len(PARTICIPANTS):
                raise RepositoryConflict("legacy initial opinions are incomplete")
            return snapshot
        at = self._clock.now()
        updated = replace(
            snapshot,
            state=replace(snapshot.state, updated_at=at),
            generation_checkpoints=tuple(
                sorted(
                    (
                        *snapshot.generation_checkpoints,
                        *(
                            GenerationCheckpoint.planned(
                                phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                                participant=participant,
                                at=at,
                            )
                            for participant in PARTICIPANTS
                        ),
                    ),
                    key=lambda checkpoint: (checkpoint.phase.value, checkpoint.participant.value),
                )
            ),
        )
        return await self._repository.replace(expected=snapshot, updated=updated)

    @staticmethod
    def _initial_opinion_checkpoints(
        snapshot: DebateSnapshot,
    ) -> dict[ParticipantSlot, GenerationCheckpoint]:
        checkpoints = {
            checkpoint.participant: checkpoint
            for checkpoint in snapshot.generation_checkpoints
            if checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
        }
        if len(checkpoints) != sum(
            checkpoint.phase is DebatePhase.COLLECTING_INITIAL_OPINIONS
            for checkpoint in snapshot.generation_checkpoints
        ):
            raise RepositoryConflict("initial opinion generation checkpoint is duplicated")
        return checkpoints

    async def _generate_and_persist_initial_opinion(
        self,
        *,
        snapshot: DebateSnapshot,
        participant: ParticipantSlot,
        evidence: EvidenceBundle,
        write_lock: asyncio.Lock,
    ) -> str | None:
        try:
            opinion = await self._within_phase(
                self._openai.generate_initial_opinion(
                    participant=participant,
                    question=snapshot.question,
                    evidence=evidence,
                )
            )
        except GenerationProviderError as error:
            return error.code
        except _PhaseDeadlineExceeded:
            return "generation_deadline_exceeded"
        if opinion.participant is not participant:
            return "openai_participant_mismatch"

        async with write_lock:
            current = await self._require_snapshot(snapshot.state.debate_id)
            checkpoint = current.checkpoint_for(
                phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                participant=participant,
            )
            expected_checkpoint = snapshot.checkpoint_for(
                phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                participant=participant,
            )
            if checkpoint is None or checkpoint != expected_checkpoint:
                raise RepositoryConflict("initial opinion result lost its exact generation claim")
            if any(existing.participant is participant for existing in current.initial_opinions):
                raise RepositoryConflict("initial opinion output is already durable")
            lease = current.lease
            if lease is None:
                raise RepositoryConflict("initial opinion result lost its fenced lease")
            settled_at = self._clock.now()
            completed = checkpoint.complete(lease=lease, at=settled_at)
            opinions = tuple(
                sorted(
                    (*current.initial_opinions, opinion),
                    key=lambda item: PARTICIPANTS.index(item.participant),
                )
            )
            updated = replace(
                current,
                state=replace(current.state, updated_at=settled_at),
                initial_opinions=opinions,
                generation_checkpoints=current.generation_checkpoints_with(completed),
            )
            await self._repository.replace(expected=current, updated=updated)
        return None

    async def _stage_initial_opinion_delivery(
        self,
        snapshot: DebateSnapshot,
    ) -> DebateSnapshot:
        staged_at = self._clock.now()
        self._require_owned_active_lease(snapshot, at=staged_at)
        operations = prepare_initial_opinion_outbox_operations(
            snapshot=snapshot,
            created_at=staged_at,
        )
        delivery = PhaseDeliveryPlan(
            plan_id="initial-opinions",
            source_phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
            target_phase=DebatePhase.DISCUSSING,
            operation_ids=tuple(operation.operation_id for operation in operations),
            content_hashes=tuple(operation.content_hash for operation in operations),
            delivery_sequences=tuple(
                operation.delivery_sequence
                for operation in operations
                if operation.delivery_sequence is not None
            ),
            staged_at=staged_at,
            deadline_at=staged_at + timedelta(minutes=15),
        )
        staged = replace(
            snapshot,
            state=replace(snapshot.state, updated_at=staged_at),
            terminal_delivery=delivery,
        )
        return await self._repository.stage_terminal_delivery(
            expected=snapshot,
            staged=staged,
            operations=operations,
        )

    async def _ensure_final_proposal_checkpoints(
        self,
        snapshot: DebateSnapshot,
    ) -> DebateSnapshot:
        checkpoints = self._final_proposal_checkpoints(snapshot)
        if checkpoints:
            if set(checkpoints) != set(PARTICIPANTS):
                raise RepositoryConflict("final proposal generation checkpoint set is incomplete")
            return snapshot
        if snapshot.final_proposals:
            if len(snapshot.final_proposals) != len(PARTICIPANTS):
                raise RepositoryConflict("legacy final proposals are incomplete")
            return snapshot
        at = self._clock.now()
        updated = replace(
            snapshot,
            state=replace(snapshot.state, updated_at=at),
            generation_checkpoints=tuple(
                sorted(
                    (
                        *snapshot.generation_checkpoints,
                        *(
                            GenerationCheckpoint.planned(
                                phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                                participant=participant,
                                at=at,
                            )
                            for participant in PARTICIPANTS
                        ),
                    ),
                    key=lambda checkpoint: (checkpoint.phase.value, checkpoint.participant.value),
                )
            ),
        )
        return await self._repository.replace(expected=snapshot, updated=updated)

    @staticmethod
    def _final_proposal_checkpoints(
        snapshot: DebateSnapshot,
    ) -> dict[ParticipantSlot, GenerationCheckpoint]:
        checkpoints = {
            checkpoint.participant: checkpoint
            for checkpoint in snapshot.generation_checkpoints
            if checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
        }
        if len(checkpoints) != sum(
            checkpoint.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS
            for checkpoint in snapshot.generation_checkpoints
        ):
            raise RepositoryConflict("final proposal generation checkpoint is duplicated")
        return checkpoints

    async def _generate_and_persist_final_proposal(
        self,
        *,
        snapshot: DebateSnapshot,
        participant: ParticipantSlot,
        evidence: EvidenceBundle,
        write_lock: asyncio.Lock,
    ) -> str | None:
        try:
            proposal = await self._within_phase(
                self._openai.generate_final_proposal(
                    participant=participant,
                    question=snapshot.question,
                    evidence=evidence,
                    initial_opinions=snapshot.initial_opinions,
                )
            )
        except GenerationProviderError as error:
            return error.code
        except _PhaseDeadlineExceeded:
            return "generation_deadline_exceeded"
        if proposal.participant is not participant:
            return "openai_participant_mismatch"

        async with write_lock:
            current = await self._require_snapshot(snapshot.state.debate_id)
            checkpoint = current.checkpoint_for(
                phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                participant=participant,
            )
            expected_checkpoint = snapshot.checkpoint_for(
                phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                participant=participant,
            )
            if checkpoint is None or checkpoint != expected_checkpoint:
                raise RepositoryConflict("final proposal result lost its exact generation claim")
            if any(existing.participant is participant for existing in current.final_proposals):
                raise RepositoryConflict("final proposal output is already durable")
            lease = current.lease
            if lease is None:
                raise RepositoryConflict("final proposal result lost its fenced lease")
            settled_at = self._clock.now()
            completed = checkpoint.complete(lease=lease, at=settled_at)
            proposals = tuple(
                sorted(
                    (*current.final_proposals, proposal),
                    key=lambda item: PARTICIPANTS.index(item.participant),
                )
            )
            updated = replace(
                current,
                state=replace(current.state, updated_at=settled_at),
                final_proposals=proposals,
                generation_checkpoints=current.generation_checkpoints_with(completed),
            )
            await self._repository.replace(expected=current, updated=updated)
        return None

    async def _stage_final_proposal_delivery(
        self,
        snapshot: DebateSnapshot,
    ) -> DebateSnapshot:
        staged_at = self._clock.now()
        self._require_owned_active_lease(snapshot, at=staged_at)
        operations = prepare_final_proposal_outbox_operations(
            snapshot=snapshot,
            created_at=staged_at,
        )
        delivery = PhaseDeliveryPlan(
            plan_id="final-proposals",
            source_phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
            target_phase=DebatePhase.SELECTING_WINNER,
            operation_ids=tuple(operation.operation_id for operation in operations),
            content_hashes=tuple(operation.content_hash for operation in operations),
            delivery_sequences=tuple(
                operation.delivery_sequence
                for operation in operations
                if operation.delivery_sequence is not None
            ),
            staged_at=staged_at,
            deadline_at=staged_at + timedelta(minutes=15),
        )
        staged = replace(
            snapshot,
            state=replace(snapshot.state, updated_at=staged_at),
            terminal_delivery=delivery,
        )
        return await self._repository.stage_terminal_delivery(
            expected=snapshot,
            staged=staged,
            operations=operations,
        )

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
        self._require_owned_active_lease(expected, at=updated.state.updated_at)
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
            or current.terminal_delivery is not None
        ):
            return
        now = self._clock.now()
        try:
            self._require_owned_active_lease(current, at=now)
            checkpointed = replace(current, state=current.state.checkpoint(at=now))
            await self._repository.replace(expected=current, updated=checkpointed)
        except RepositoryConflict:
            return
        self._metrics.increment(MetricEvent.CHECKPOINTED, debate_id=debate_id)

    async def _fail_current(self, debate_id: DebateId, *, error_code: str) -> None:
        current = await self._require_snapshot(debate_id)
        if current.state.phase.is_terminal:
            return
        if current.terminal_delivery is not None:
            await self._finish_terminal_delivery_with_conflict_retry(current)
            return
        try:
            staged = await self._stage_terminal_delivery(
                current,
                target=DebatePhase.FAILED,
                error_code=error_code,
            )
            await self._finish_terminal_delivery_with_conflict_retry(staged)
        except RepositoryConflict:
            return

    async def _stage_terminal_delivery(
        self,
        snapshot: DebateSnapshot,
        *,
        target: DebatePhase,
        error_code: str | None = None,
        final_decision: FinalDecision | None = None,
        generation_checkpoint: GenerationCheckpoint | None = None,
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
        at: datetime | None = None,
    ) -> DebateSnapshot:
        """Atomically freeze a terminal outcome and its required Discord Outbox."""

        staged_at = self._clock.now() if at is None else at
        self._require_owned_active_lease(snapshot, at=staged_at)
        checkpoints = snapshot.generation_checkpoints
        if generation_checkpoint is not None:
            checkpoints = snapshot.generation_checkpoints_with(generation_checkpoint)
        if target is DebatePhase.FAILED:
            if error_code is None:
                raise ValueError("failed delivery requires an error code")
            lease = snapshot.lease
            if lease is None:
                raise RepositoryConflict("generation failure settlement requires a fenced lease")
            checkpoints = tuple(
                _settle_generation_checkpoint_failure(
                    checkpoint,
                    lease=lease,
                    at=staged_at,
                    error_code=error_code,
                )
                for checkpoint in checkpoints
            )
        if target is DebatePhase.CANCELLED:
            checkpoints = tuple(
                checkpoint.cancel(at=staged_at)
                if checkpoint.status in {GenerationStatus.PLANNED, GenerationStatus.IN_FLIGHT}
                else checkpoint
                for checkpoint in checkpoints
            )
        payload_source = (
            replace(snapshot, terminal_delivery=None)
            if isinstance(snapshot.terminal_delivery, PhaseDeliveryPlan)
            and snapshot.terminal_delivery.status is PhaseDeliveryStatus.ABANDONED
            else snapshot
        )
        if target is DebatePhase.COMPLETED:
            payload = replace(
                payload_source,
                error_code=error_code,
                generation_checkpoints=checkpoints,
                final_decision=final_decision or snapshot.final_decision,
            )
        elif target is DebatePhase.FAILED:
            if final_decision is not None:
                raise ValueError("only completed delivery may supply a final decision")
            payload = replace(
                payload_source,
                error_code=error_code,
                generation_checkpoints=checkpoints,
            )
        elif target is DebatePhase.CANCELLED:
            if final_decision is not None:
                raise ValueError("only completed delivery may supply a final decision")
            payload = snapshot
        else:
            raise ValueError("terminal delivery target must be completed, failed, or cancelled")
        operations = prepare_terminal_outbox_operations(
            snapshot=payload,
            target_phase=target,
            created_at=staged_at,
            error_code=error_code,
        )
        delivery = PhaseDeliveryPlan(
            plan_id=operations[0].plan_id or f"terminal-{target.value}",
            source_phase=snapshot.state.phase,
            target_phase=target,
            operation_ids=tuple(operation.operation_id for operation in operations),
            content_hashes=tuple(operation.content_hash for operation in operations),
            delivery_sequences=tuple(
                operation.delivery_sequence
                for operation in operations
                if operation.delivery_sequence is not None
            ),
            staged_at=staged_at,
            deadline_at=staged_at + timedelta(minutes=15),
        )
        staged = replace(
            snapshot if target is DebatePhase.CANCELLED else payload,
            state=replace(snapshot.state, updated_at=staged_at),
            error_code=error_code,
            generation_checkpoints=checkpoints,
            terminal_delivery=delivery,
        )
        return await self._repository.stage_terminal_delivery(
            expected=snapshot,
            staged=staged,
            operations=operations,
            operation_id=operation_id,
            ingress_claim=ingress_claim,
        )

    async def _finish_terminal_delivery(self, snapshot: DebateSnapshot) -> DebateSnapshot:
        """Drain required Outbox and atomically terminalize only after every SENT check."""

        delivery = snapshot.terminal_delivery
        if delivery is None:
            raise InvalidApplicationOperation("terminal delivery is not staged")
        if snapshot.state.phase.is_terminal:
            return snapshot
        if (
            isinstance(delivery, PhaseDeliveryPlan)
            and delivery.status is PhaseDeliveryStatus.TERMINATING
        ):
            return await self._settle_terminating_delivery(snapshot)
        if (
            isinstance(delivery, PhaseDeliveryPlan)
            and delivery.status is PhaseDeliveryStatus.ABANDONED
        ):
            return await self._continue_after_abandoned_delivery(snapshot)
        self._require_owned_active_lease(snapshot, at=self._clock.now())
        try:
            await self._outbox_recovery.drain(expected=snapshot)
        except OutboxRecoveryAbandoned as error:
            try:
                reason = DeliveryAbandonReason(error.reason)
            except ValueError as invalid_reason:  # pragma: no cover - local producer is typed
                raise RepositoryConflict(
                    "outbox recovery returned an unknown reason"
                ) from invalid_reason
            current = await self._require_snapshot(snapshot.state.debate_id)
            self._require_owned_active_lease(current, at=self._clock.now())
            terminating = await self._repository.terminate_terminal_delivery(
                expected=current,
                at=self._clock.now(),
                reason=reason,
            )
            return await self._settle_terminating_delivery(
                terminating,
                delivery_code=error.delivery_code,
            )
        except RepositoryTransactionConflict as error:
            raise _TerminalDeliveryConflict(
                stage=error.stage.value,
                actions=tuple(action.value for action, _ in error.failures),
                codes=tuple(code.value for _, code in error.failures),
                reasons_complete=error.reasons_complete,
                retryable=error.retryable,
            ) from error
        except RepositoryConflict as error:
            raise _TerminalDeliveryConflict(
                stage="outbox_recovery",
                actions=("unknown",),
                codes=("repository_conflict",),
                reasons_complete=False,
                retryable=False,
            ) from error
        current = await self._require_snapshot(snapshot.state.debate_id)
        if current.state.phase.is_terminal:
            return current
        if current.terminal_delivery != delivery:
            raise _TerminalDeliveryConflict(
                stage="state_recheck",
                actions=("terminal_delivery_plan",),
                codes=("identity_mismatch",),
                reasons_complete=True,
                retryable=False,
            )
        at = self._clock.now()
        self._require_owned_active_lease(current, at=at)
        if not delivery.target_phase.is_terminal:
            finalized_phase = replace(
                current,
                state=current.state.transition_to(delivery.target_phase, at=at),
                terminal_delivery=None,
            )
            try:
                persisted_phase = await self._repository.finalize_phase_delivery(
                    expected=current,
                    updated=finalized_phase,
                )
            except RepositoryTransactionConflict as error:
                raise _TerminalDeliveryConflict(
                    stage=error.stage.value,
                    actions=tuple(action.value for action, _ in error.failures),
                    codes=tuple(code.value for _, code in error.failures),
                    reasons_complete=error.reasons_complete,
                    retryable=error.retryable,
                ) from error
            except RepositoryConflict as error:
                raise _TerminalDeliveryConflict(
                    stage="phase_delivery_finalize",
                    actions=("unknown",),
                    codes=("repository_conflict",),
                    reasons_complete=False,
                    retryable=False,
                ) from error
            self._metrics.increment(
                MetricEvent.PHASE_COMPLETED,
                debate_id=persisted_phase.state.debate_id,
            )
            return persisted_phase
        finalized = replace(
            current,
            state=current.state.transition_to(delivery.target_phase, at=at),
            terminal_delivery=delivery.complete(at=at),
        )
        try:
            persisted = await self._repository.finalize_terminal(
                expected=current,
                updated=finalized,
            )
        except RepositoryTransactionConflict as error:
            raise _TerminalDeliveryConflict(
                stage=error.stage.value,
                actions=tuple(action.value for action, _ in error.failures),
                codes=tuple(code.value for _, code in error.failures),
                reasons_complete=error.reasons_complete,
                retryable=error.retryable,
            ) from error
        except RepositoryConflict as error:
            raise _TerminalDeliveryConflict(
                stage="terminal_finalize",
                actions=("unknown",),
                codes=("repository_conflict",),
                reasons_complete=False,
                retryable=False,
            ) from error
        metric = {
            DebatePhase.COMPLETED: MetricEvent.COMPLETED,
            DebatePhase.FAILED: MetricEvent.FAILED,
            DebatePhase.CANCELLED: MetricEvent.CANCELLED,
        }[persisted.state.phase]
        self._metrics.increment(metric, debate_id=persisted.state.debate_id)
        return persisted

    async def _settle_terminating_delivery(
        self,
        snapshot: DebateSnapshot,
        *,
        delivery_code: str | None = None,
    ) -> DebateSnapshot:
        """Reconcile an in-flight POST before the persisted stop intent settles."""

        plan = snapshot.terminal_delivery
        if (
            not isinstance(plan, PhaseDeliveryPlan)
            or plan.status is not PhaseDeliveryStatus.TERMINATING
            or plan.abandon_reason is None
        ):
            raise RepositoryConflict("terminating delivery lost its durable stop intent")
        all_sent = await self._outbox_recovery.terminate(expected=snapshot)
        current = await self._require_snapshot(snapshot.state.debate_id)
        current_plan = current.terminal_delivery
        if (
            not isinstance(current_plan, PhaseDeliveryPlan)
            or current_plan.status is not PhaseDeliveryStatus.TERMINATING
            or current_plan.abandon_reason is not plan.abandon_reason
        ):
            raise RepositoryConflict("terminating delivery changed during reconciliation")
        if all_sent and plan.abandon_reason is not DeliveryAbandonReason.CANCELLED:
            at = self._clock.now()
            if not current_plan.target_phase.is_terminal:
                finalized_phase = replace(
                    current,
                    state=current.state.transition_to(current_plan.target_phase, at=at),
                    terminal_delivery=None,
                )
                persisted_phase = await self._repository.finalize_phase_delivery(
                    expected=current,
                    updated=finalized_phase,
                )
                self._metrics.increment(
                    MetricEvent.PHASE_COMPLETED,
                    debate_id=persisted_phase.state.debate_id,
                )
                return persisted_phase
            finalized = replace(
                current,
                state=current.state.transition_to(current_plan.target_phase, at=at),
                terminal_delivery=current_plan.complete(at=at),
            )
            persisted = await self._repository.finalize_terminal(
                expected=current,
                updated=finalized,
            )
            metric = {
                DebatePhase.COMPLETED: MetricEvent.COMPLETED,
                DebatePhase.FAILED: MetricEvent.FAILED,
                DebatePhase.CANCELLED: MetricEvent.CANCELLED,
            }[persisted.state.phase]
            self._metrics.increment(metric, debate_id=persisted.state.debate_id)
            return persisted
        abandoned = await self._repository.abandon_terminal_delivery(
            expected=current,
            at=self._clock.now(),
            reason=plan.abandon_reason,
        )
        return await self._continue_after_abandoned_delivery(
            abandoned,
            delivery_code=delivery_code,
        )

    async def _continue_after_abandoned_delivery(
        self,
        snapshot: DebateSnapshot,
        *,
        delivery_code: str | None = None,
    ) -> DebateSnapshot:
        plan = snapshot.terminal_delivery
        if (
            not isinstance(plan, PhaseDeliveryPlan)
            or plan.status is not PhaseDeliveryStatus.ABANDONED
        ):
            raise RepositoryConflict("delivery abandonment lost its durable plan")
        if plan.abandon_reason is DeliveryAbandonReason.CANCELLED:
            staged = await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.CANCELLED,
                at=self._clock.now(),
            )
            return await self._finish_terminal_delivery(staged)
        if plan.target_phase is DebatePhase.COMPLETED:
            reason = plan.abandon_reason
            if reason is None:
                raise RepositoryConflict("abandoned delivery lost its reason")
            error_code = delivery_code or f"discord_outbox_{reason.value}"
            staged = await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.FAILED,
                error_code=error_code,
                at=self._clock.now(),
            )
            return await self._finish_terminal_delivery(staged)
        if not plan.target_phase.is_terminal:
            reason = plan.abandon_reason
            if reason is None:
                raise RepositoryConflict("abandoned delivery lost its reason")
            error_code = delivery_code or f"discord_outbox_{reason.value}"
            staged = await self._stage_terminal_delivery(
                snapshot,
                target=DebatePhase.FAILED,
                error_code=error_code,
                at=self._clock.now(),
            )
            return await self._finish_terminal_delivery(staged)

        at = self._clock.now()
        target = plan.target_phase
        finalized = replace(
            snapshot,
            state=snapshot.state.transition_to(target, at=at),
            terminal_delivery=plan,
        )
        persisted = await self._repository.finalize_terminal(
            expected=snapshot,
            updated=finalized,
        )
        metric = {
            DebatePhase.FAILED: MetricEvent.FAILED,
            DebatePhase.CANCELLED: MetricEvent.CANCELLED,
        }[persisted.state.phase]
        self._metrics.increment(metric, debate_id=persisted.state.debate_id)
        return persisted

    async def _require_snapshot(self, debate_id: DebateId) -> DebateSnapshot:
        snapshot = await self._repository.get(debate_id)
        if snapshot is None:
            raise DebateNotFound(f"debate not found: {debate_id}")
        return snapshot

    async def _reclaim_ingress_replay(
        self,
        snapshot: DebateSnapshot,
        *,
        ingress_claim: IngressClaimFence | None,
    ) -> DebateSnapshot:
        if ingress_claim is None or snapshot.state.phase.is_terminal:
            return snapshot
        at = self._clock.now()
        current_claim = _claim_for_write(ingress_claim, at=at)
        if current_claim is None:
            raise RepositoryClaimLost("ingress replay lost its durable claim")
        return await self._repository.reclaim_for_ingress(
            expected=snapshot,
            lease_owner=self._lease_owner,
            at=at,
            ingress_claim=current_claim,
        )

    def _require_owned_active_lease(self, snapshot: DebateSnapshot, *, at: datetime) -> None:
        """Prevent a stale process from reusing a replacement runtime's lease."""

        if snapshot.state.phase.is_terminal:
            return
        lease = snapshot.lease
        if lease is None or lease.owner_id != self._lease_owner or lease.expires_at < at:
            raise RepositoryConflict("active debate lease is not owned by this runtime")

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


def _log_terminal_delivery_conflict(
    debate_id: DebateId,
    error: _TerminalDeliveryConflict,
    *,
    retry_number: int | None,
    retry_delay_seconds: float | None,
) -> None:
    _LOGGER.warning(
        json.dumps(
            {
                "severity": "WARNING" if error.retryable else "ERROR",
                "event": "terminal_delivery_conflict",
                "debate_id": str(debate_id),
                "transaction_stage": error.stage,
                "failed_action_kinds": error.actions,
                "failure_codes": error.codes,
                "cancellation_reasons_complete": error.reasons_complete,
                "retryable": error.retryable,
                "retry_number": retry_number,
                "retry_delay_seconds": retry_delay_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _claim_for_write(
    ingress_claim: IngressClaimFence | None,
    *,
    at: datetime,
) -> IngressClaimFence | None:
    if ingress_claim is None:
        return None
    try:
        return ingress_claim.for_write_at(at)
    except ValueError as error:
        raise RepositoryClaimLost("ingress claim expired before the durable write") from error


def _require_claim_for_write(
    ingress_claim: IngressClaimFence,
    *,
    at: datetime,
) -> IngressClaimFence:
    current = _claim_for_write(ingress_claim, at=at)
    if current is None:  # pragma: no cover - non-optional input narrows this defensively
        raise RepositoryClaimLost("ingress claim is required for the durable write")
    return current


def _validated_ingress_error_code(error_code: str) -> str:
    value = error_code.strip()
    if not value or len(value) > 100:
        raise ValueError("ingress error code must contain at most 100 characters")
    return value


def _generation_checkpoints_with(
    checkpoints: tuple[GenerationCheckpoint, ...],
    updated: GenerationCheckpoint,
) -> tuple[GenerationCheckpoint, ...]:
    retained = tuple(
        checkpoint
        for checkpoint in checkpoints
        if (checkpoint.phase, checkpoint.participant) != (updated.phase, updated.participant)
    )
    return tuple(
        sorted(
            (*retained, updated),
            key=lambda checkpoint: (checkpoint.phase.value, checkpoint.participant.value),
        )
    )


def _settle_generation_checkpoint_failure(
    checkpoint: GenerationCheckpoint,
    *,
    lease: LeaseGrant,
    at: datetime,
    error_code: str,
) -> GenerationCheckpoint:
    if checkpoint.status in {GenerationStatus.COMPLETED, GenerationStatus.FAILED}:
        return checkpoint
    if checkpoint.status is GenerationStatus.PLANNED:
        return checkpoint.fail_before_call(at=at, error_code=error_code)
    claim_identity = (
        checkpoint.claim_owner,
        checkpoint.claim_slot,
        checkpoint.claim_fencing_token,
    )
    lease_identity = (lease.owner_id, lease.slot, lease.fencing_token)
    if claim_identity == lease_identity:
        return checkpoint.fail(lease=lease, at=at, error_code=error_code)
    return checkpoint.fail_before_recovery_call(
        lease=lease,
        at=at,
        error_code=error_code,
    )


def _validate_participant_outputs(
    outputs: tuple[InitialOpinion, ...] | tuple[FinalProposal, ...],
    *,
    label: str,
) -> None:
    if len(outputs) != len(PARTICIPANTS):
        raise ValueError(f"{label} must contain exactly one item per participant")
    if {output.participant for output in outputs} != set(PARTICIPANTS):
        raise ValueError(f"{label} contain a duplicate or unknown participant")
