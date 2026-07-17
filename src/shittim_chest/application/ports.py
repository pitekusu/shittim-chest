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


class DiscordGateway(Protocol):
    """Expose Discord readiness and configured request authorization."""

    async def all_identities_ready(self) -> bool: ...

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool: ...


class DiscordPublisher(Protocol):
    """Publish only an operation previously persisted by an outbox adapter."""

    async def publish_persisted(self, operation_id: str) -> None: ...


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

    async def list_recoverable(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        at: datetime,
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
