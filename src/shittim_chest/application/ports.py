"""Structural Protocols implemented by adapters or deterministic test fakes."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from shittim_chest.application.discord import OutboxOperation
from shittim_chest.application.models import (
    AcceptDebateRequest,
    DebateSnapshot,
    LeaseGrant,
    MetricEvent,
)
from shittim_chest.application.scale_to_zero import (
    EcsRuntimeSnapshot,
    EnqueuedIngress,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    RuntimeActivity,
    RuntimeState,
    StatusMessageState,
)
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


class RepositoryConflict(Exception):
    """Raised when a conditional repository operation loses its expected state."""


class RepositoryBusy(Exception):
    """Raised when all three global execution slots are leased."""


class RepositoryQuotaExceeded(Exception):
    """Raised when a Guild has consumed its daily acceptance quota."""


class RepositoryQueueFull(Exception):
    """Raised when the bounded ingress FIFO already contains twenty requests."""


class Clock(Protocol):
    """Provide timezone-aware UTC wall-clock timestamps."""

    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    """Generate UUIDv7 domain identifiers."""

    def new_debate_id(self) -> DebateId: ...

    def new_attempt_id(self) -> AttemptId: ...


class Metrics(Protocol):
    """Record low-cardinality application events without user content."""

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None: ...


class IngressRepository(Protocol):
    """Persist a bounded FIFO and its strongly consistent operation results."""

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress: ...

    async def get_operation_result(
        self,
        interaction_id: str,
    ) -> IngressOperationResult | None: ...

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def claim(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None: ...

    async def reschedule(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest: ...

    async def mark_accepted(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest: ...

    async def mark_startup_timeout(
        self,
        *,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest: ...

    async def mark_terminal(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest: ...

    async def update_status_message(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        message_id: str,
        at: datetime,
    ) -> IngressRequest: ...

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def active_count(self) -> int: ...


class RuntimeStateRepository(Protocol):
    """Store one strongly consistent, generation-fenced runtime aggregate."""

    async def get(self) -> RuntimeState | None: ...

    async def request_wake(
        self,
        *,
        interaction_id: str,
        at: datetime,
    ) -> RuntimeState: ...

    async def replace(
        self,
        *,
        expected: RuntimeState,
        updated: RuntimeState,
    ) -> RuntimeState: ...


class EcsRuntimeControl(Protocol):
    """Control only the configured singleton ECS service through typed values."""

    async def describe(self) -> EcsRuntimeSnapshot: ...

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot: ...


class DiscordStatusPublisher(Protocol):
    """Create or update one persisted public startup-status message."""

    async def publish(self, *, request: IngressRequest) -> IngressRequest: ...


class RuntimeActivityInspector(Protocol):
    """Aggregate every durable and process-owned activity required for IDLE."""

    async def inspect(self, *, at: datetime) -> RuntimeActivity: ...


class DiscordGateway(Protocol):
    """Expose Discord readiness and configured request authorization."""

    async def all_identities_ready(self) -> bool: ...

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool: ...


class DiscordPublisher(Protocol):
    """Publish only an operation previously persisted by an outbox adapter."""

    async def publish_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None: ...


class DiscordOutboxDrainer(Protocol):
    """Drain persisted Discord operations before debate phase work resumes."""

    async def drain(self, *, expected: DebateSnapshot) -> None: ...


class DiscordOutboxRepository(Protocol):
    """Persist and fence Discord delivery without exposing DynamoDB to its publisher."""

    async def prepare(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> OutboxOperation: ...

    async def get(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        operation_id: str,
    ) -> OutboxOperation | None: ...

    async def claim(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
    ) -> OutboxOperation | None: ...

    async def mark_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation: ...

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation: ...

    async def list_pending(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]: ...


class EvidenceService(Protocol):
    """Prepare one immutable evidence bundle shared by all participants."""

    async def prepare_evidence(self, *, question: str) -> EvidenceBundle: ...


class CandidateOrderer(Protocol):
    """Randomize candidate presentation through an injectable boundary."""

    def order_candidates(
        self,
        *,
        voter: ParticipantSlot,
        candidates: tuple[FinalProposal, ...],
    ) -> tuple[FinalProposal, ...]: ...


class OpenAIService(Protocol):
    """Return validated domain models rather than SDK response objects."""

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
    ) -> InitialOpinion: ...

    async def generate_final_proposal(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
    ) -> FinalProposal: ...

    async def cast_vote(
        self,
        *,
        voter: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        candidates: tuple[FinalProposal, ...],
    ) -> Vote: ...

    async def generate_decision(
        self,
        *,
        question: str,
        evidence: EvidenceBundle,
        proposals: tuple[FinalProposal, ...],
        voting_result: VotingResult,
    ) -> FinalDecision: ...


class DebateRepository(Protocol):
    """Persist application aggregates with conditional-write semantics."""

    async def get_operation_result(self, operation_id: str) -> DebateSnapshot | None: ...

    async def create(
        self,
        snapshot: DebateSnapshot,
        *,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot: ...

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None: ...

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None = None,
    ) -> DebateSnapshot: ...

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
    ) -> DebateSnapshot: ...

    async def claim_recoverable(
        self,
        *,
        lease_owner: str,
        at: datetime,
    ) -> tuple[DebateSnapshot, ...]: ...

    async def renew_lease(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
    ) -> LeaseGrant: ...
