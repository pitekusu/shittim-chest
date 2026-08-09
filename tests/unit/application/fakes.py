"""Deterministic Protocol fakes for application tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from shittim_chest.application.discord import DiscordBotSlot, OutboxOperation, OutboxStatus
from shittim_chest.application.models import (
    AcceptDebateRequest,
    DebateSnapshot,
    DeliveryAbandonReason,
    LeaseGrant,
    MetricEvent,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
)
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.scale_to_zero import IngressClaimFence
from shittim_chest.domain import (
    AttemptId,
    DebateId,
    EvidenceBundle,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    Vote,
    VotingResult,
)


@dataclass(slots=True)
class FakeClock:
    current: datetime = datetime(2026, 7, 17, tzinfo=UTC)

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


@dataclass(slots=True)
class FakeIds:
    debate_ids: list[DebateId] = field(default_factory=list)
    attempt_ids: list[AttemptId] = field(default_factory=list)

    def new_debate_id(self) -> DebateId:
        value = DebateId.new()
        self.debate_ids.append(value)
        return value

    def new_attempt_id(self) -> AttemptId:
        value = AttemptId.new()
        self.attempt_ids.append(value)
        return value


@dataclass(slots=True)
class FakeMetrics:
    events: list[tuple[MetricEvent, DebateId]] = field(default_factory=list)

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None:
        self.events.append((event, debate_id))


@dataclass(slots=True)
class FakeDiscord:
    ready: bool = True
    allowed: bool = True
    delivery_ready: bool = True
    delivery_ready_by_slot: dict[DiscordBotSlot, bool] = field(default_factory=dict)
    delivery_ready_results: list[bool] = field(default_factory=list)
    delivery_checks: list[tuple[DiscordBotSlot, str, str]] = field(default_factory=list)

    async def all_identities_ready(self) -> bool:
        return self.ready

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool:
        del request
        return self.allowed

    async def delivery_target_is_ready(
        self,
        *,
        bot_slot: DiscordBotSlot,
        guild_id: str,
        thread_id: str,
    ) -> bool:
        self.delivery_checks.append((bot_slot, guild_id, thread_id))
        if self.delivery_ready_results:
            return self.delivery_ready_results.pop(0)
        return self.delivery_ready_by_slot.get(bot_slot, self.delivery_ready)


@dataclass(slots=True)
class FakeEvidence:
    bundle: EvidenceBundle = field(default_factory=EvidenceBundle)
    calls: list[str] = field(default_factory=list)
    delay: float = 0.0
    error: Exception | None = None

    async def prepare_evidence(self, *, question: str) -> EvidenceBundle:
        self.calls.append(question)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.bundle


@dataclass(slots=True)
class FakeCandidateOrderer:
    calls: list[tuple[ParticipantSlot, tuple[ParticipantSlot, ...]]] = field(default_factory=list)
    corrupt: bool = False
    duplicate: bool = False

    def order_candidates(
        self,
        *,
        voter: ParticipantSlot,
        candidates: tuple[FinalProposal, ...],
    ) -> tuple[FinalProposal, ...]:
        self.calls.append((voter, tuple(candidate.participant for candidate in candidates)))
        if self.corrupt:
            return candidates[:1]
        if self.duplicate:
            return (*candidates, candidates[0])
        return tuple(reversed(candidates))


@dataclass(slots=True)
class FakeOutboxRecovery:
    calls: list[DebateSnapshot] = field(default_factory=list)
    error: Exception | None = None
    delay: float = 0.0
    termination_complete: bool = True

    async def drain(self, *, expected: DebateSnapshot) -> None:
        self.calls.append(expected)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

    async def terminate(self, *, expected: DebateSnapshot) -> bool:
        self.calls.append(expected)
        return self.termination_complete


class FakeOpenAI:
    def __init__(self) -> None:
        self.initial_calls: list[ParticipantSlot] = []
        self.proposal_calls: list[ParticipantSlot] = []
        self.vote_calls: list[tuple[ParticipantSlot, tuple[ParticipantSlot, ...]]] = []
        self.decision_calls: list[ParticipantSlot] = []
        self.decision_errors: list[BaseException] = []
        self.initial_errors: dict[ParticipantSlot, BaseException] = {}
        self.initial_participant_override: dict[ParticipantSlot, ParticipantSlot] = {}
        self.proposal_errors: dict[ParticipantSlot, BaseException] = {}
        self.proposal_participant_override: dict[ParticipantSlot, ParticipantSlot] = {}
        self.decision_delay = 0.0
        self.fail_initial_for: ParticipantSlot | None = None
        self.block_initial = False
        self.cancelled_initial: set[ParticipantSlot] = set()

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
    ) -> InitialOpinion:
        del question, evidence
        self.initial_calls.append(participant)
        initial_error = self.initial_errors.get(participant)
        if initial_error is not None:
            raise initial_error
        if participant is self.fail_initial_for:
            await asyncio.sleep(0)
            raise RuntimeError("generated failure")
        if self.block_initial:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled_initial.add(participant)
        return InitialOpinion(
            self.initial_participant_override.get(participant, participant),
            "summary",
            "proposal",
        )

    async def generate_final_proposal(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
    ) -> FinalProposal:
        del question, evidence, initial_opinions
        self.proposal_calls.append(participant)
        proposal_error = self.proposal_errors.get(participant)
        if proposal_error is not None:
            raise proposal_error
        return FinalProposal(
            self.proposal_participant_override.get(participant, participant),
            f"title-{participant.value}",
            "proposal",
        )

    async def cast_vote(
        self,
        *,
        voter: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        candidates: tuple[FinalProposal, ...],
    ) -> Vote:
        del question, evidence
        candidate_slots = tuple(candidate.participant for candidate in candidates)
        self.vote_calls.append((voter, candidate_slots))
        choice = {
            ParticipantSlot.PARTICIPANT_A: ParticipantSlot.PARTICIPANT_C,
            ParticipantSlot.PARTICIPANT_B: ParticipantSlot.PARTICIPANT_A,
            ParticipantSlot.PARTICIPANT_C: ParticipantSlot.PARTICIPANT_B,
        }[voter]
        return Vote(voter, choice, 3, 3, 3, "reason")

    async def generate_decision(
        self,
        *,
        question: str,
        evidence: EvidenceBundle,
        proposals: tuple[FinalProposal, ...],
        voting_result: VotingResult,
    ) -> FinalDecision:
        del question, evidence, proposals
        self.decision_calls.append(voting_result.winner)
        if self.decision_delay:
            await asyncio.sleep(self.decision_delay)
        if self.decision_errors:
            raise self.decision_errors.pop(0)
        return FinalDecision(voting_result.winner, "decision", ("action",), ())


class FakeRepository:
    def __init__(self) -> None:
        self.current: dict[DebateId, DebateSnapshot] = {}
        self.history: dict[DebateId, list[DebateSnapshot]] = defaultdict(list)
        self.recoverable: tuple[DebateId, ...] = ()
        self.operations: dict[str, DebateSnapshot] = {}
        self.next_fencing_token = 1
        self.renew_calls: list[tuple[DebateId, datetime]] = []
        self.ingress_claims: list[IngressClaimFence | None] = []
        self.terminal_operations: dict[str, OutboxOperation] = {}
        self.terminal_stages: list[DebateSnapshot] = []
        self.terminal_finalizations: list[DebateSnapshot] = []
        self.phase_delivery_finalizations: list[DebateSnapshot] = []
        self.terminal_finalize_errors: list[RepositoryConflict] = []

    async def get_operation_result(
        self,
        operation_id: str,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot | None:
        del ingress_claim
        return self.operations.get(operation_id)

    def _grant(self, snapshot: DebateSnapshot, lease_owner: str) -> DebateSnapshot:
        lease = LeaseGrant(
            owner_id=lease_owner,
            slot=0,
            fencing_token=self.next_fencing_token,
            expires_at=snapshot.state.updated_at + timedelta(seconds=60),
        )
        self.next_fencing_token += 1
        return replace(snapshot, lease=lease)

    async def create(
        self,
        snapshot: DebateSnapshot,
        *,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        self.ingress_claims.append(ingress_claim)
        if operation_id in self.operations:
            return self.operations[operation_id]
        if snapshot.state.debate_id in self.current:
            raise RepositoryConflict
        persisted = self._grant(snapshot, lease_owner)
        self.current[snapshot.state.debate_id] = persisted
        self.history[snapshot.state.debate_id].append(persisted)
        self.operations[operation_id] = persisted
        return persisted

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None:
        return self.current.get(debate_id)

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        self.ingress_claims.append(ingress_claim)
        if operation_id is not None and operation_id in self.operations:
            return self.operations[operation_id]
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        if current is None or not _same_snapshot_version(current, expected):
            raise RepositoryConflict
        persisted = (
            replace(updated, lease=None)
            if updated.state.phase.is_terminal
            else replace(updated, lease=current.lease)
        )
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        if operation_id is not None:
            self.operations[operation_id] = persisted
        return persisted

    async def stage_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        staged: DebateSnapshot,
        operations: tuple[OutboxOperation, ...],
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        self.ingress_claims.append(ingress_claim)
        if operation_id is not None:
            replay = self.operations.get(operation_id)
            if replay is not None and replay.terminal_delivery is not None:
                return replay
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        if current is None or not _same_snapshot_version(current, expected):
            raise RepositoryConflict
        persisted = replace(staged, lease=current.lease)
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        self.terminal_stages.append(persisted)
        for operation in operations:
            existing = self.terminal_operations.get(operation.operation_id)
            if existing is not None and existing != operation:
                raise RepositoryConflict
            self.terminal_operations[operation.operation_id] = operation
        if operation_id is not None:
            self.operations[operation_id] = persisted
        return persisted

    async def finalize_terminal(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
    ) -> DebateSnapshot:
        if self.terminal_finalize_errors:
            raise self.terminal_finalize_errors.pop(0)
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        if current is None or not _same_snapshot_version(current, expected):
            raise RepositoryConflict
        persisted = replace(updated, lease=None)
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        self.terminal_finalizations.append(persisted)
        for operation_id, result in tuple(self.operations.items()):
            if (
                result.state.debate_id == persisted.state.debate_id
                and result.state.attempt_id == persisted.state.attempt_id
            ):
                self.operations[operation_id] = persisted
        return persisted

    async def finalize_phase_delivery(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
    ) -> DebateSnapshot:
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        if current is None or not _same_snapshot_version(current, expected):
            raise RepositoryConflict
        persisted = replace(updated, lease=current.lease)
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        self.phase_delivery_finalizations.append(persisted)
        return persisted

    async def terminate_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
        reason: DeliveryAbandonReason,
    ) -> DebateSnapshot:
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        plan = expected.terminal_delivery
        if (
            current is None
            or not _same_snapshot_version(current, expected)
            or not isinstance(plan, PhaseDeliveryPlan)
        ):
            raise RepositoryConflict
        persisted = replace(
            expected,
            state=replace(expected.state, updated_at=at),
            terminal_delivery=plan.terminate(reason=reason),
        )
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        return persisted

    async def abandon_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
        reason: DeliveryAbandonReason,
    ) -> DebateSnapshot:
        debate_id = expected.state.debate_id
        current = self.current.get(debate_id)
        plan = expected.terminal_delivery
        if (
            current is None
            or not _same_snapshot_version(current, expected)
            or not isinstance(plan, PhaseDeliveryPlan)
            or plan.status
            not in {
                PhaseDeliveryStatus.STAGED,
                PhaseDeliveryStatus.TERMINATING,
            }
        ):
            raise RepositoryConflict
        persisted = replace(
            expected,
            state=replace(expected.state, updated_at=at),
            terminal_delivery=plan.abandon(at=at, reason=reason),
        )
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        for operation_id in plan.operation_ids:
            operation = self.terminal_operations[operation_id]
            if operation.status is OutboxStatus.SENT:
                continue
            self.terminal_operations[operation_id] = replace(
                operation,
                status=OutboxStatus.ABANDONED,
                claim_owner=None,
                claim_expires_at=None,
                next_retry_at=None,
                abandoned_at=at,
                abandon_reason=reason,
            )
        return persisted

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot:
        self.ingress_claims.append(ingress_claim)
        if operation_id in self.operations:
            return self.operations[operation_id]
        debate_id = expected_failed.state.debate_id
        if self.current.get(debate_id) != expected_failed:
            raise RepositoryConflict
        persisted = self._grant(retry, lease_owner)
        self.current[debate_id] = persisted
        self.history[debate_id].append(persisted)
        self.operations[operation_id] = persisted
        return persisted

    async def reclaim_for_ingress(
        self,
        *,
        expected: DebateSnapshot,
        lease_owner: str,
        at: datetime,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        del ingress_claim
        current = self.current.get(expected.state.debate_id)
        if current != expected:
            raise RepositoryConflict
        persisted = replace(
            expected,
            lease=LeaseGrant(
                owner_id=lease_owner,
                slot=0,
                fencing_token=self.next_fencing_token,
                expires_at=at + timedelta(seconds=60),
            ),
        )
        self.next_fencing_token += 1
        self.current[expected.state.debate_id] = persisted
        self.history[expected.state.debate_id].append(persisted)
        return persisted

    async def fail_pre_activation(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot:
        self.ingress_claims.append(ingress_claim)
        current = self.current.get(expected.state.debate_id)
        if current != expected:
            raise RepositoryConflict
        persisted = replace(updated, lease=None)
        self.current[expected.state.debate_id] = persisted
        self.history[expected.state.debate_id].append(persisted)
        for operation_id, result in tuple(self.operations.items()):
            if (
                result.state.debate_id == persisted.state.debate_id
                and result.state.attempt_id == persisted.state.attempt_id
            ):
                self.operations[operation_id] = persisted
        return persisted

    async def claim_recoverable(
        self,
        *,
        lease_owner: str,
        at: datetime,
    ) -> tuple[DebateSnapshot, ...]:
        del at
        snapshots = tuple(self.current[debate_id] for debate_id in self.recoverable)
        claimed = tuple(self._grant(snapshot, lease_owner) for snapshot in snapshots)
        for snapshot in claimed:
            self.current[snapshot.state.debate_id] = snapshot
        return claimed

    async def renew_lease(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
    ) -> LeaseGrant:
        current = self.current.get(expected.state.debate_id)
        if current != expected or expected.lease is None:
            raise RepositoryConflict
        self.renew_calls.append((expected.state.debate_id, at))
        renewed = replace(expected.lease, expires_at=at + timedelta(seconds=60))
        self.current[expected.state.debate_id] = replace(expected, lease=renewed)
        return renewed


def _same_snapshot_version(current: DebateSnapshot, expected: DebateSnapshot) -> bool:
    if current.lease is None or expected.lease is None:
        return current == expected
    same_lease_identity = (
        current.lease.owner_id == expected.lease.owner_id
        and current.lease.slot == expected.lease.slot
        and current.lease.fencing_token == expected.lease.fencing_token
    )
    return same_lease_identity and replace(current, lease=expected.lease) == expected
