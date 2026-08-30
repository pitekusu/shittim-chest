"""Structural Protocols implemented by adapters or deterministic test fakes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum, unique
from typing import Protocol

from shittim_chest.application.discord import DiscordBotSlot, OutboxOperation
from shittim_chest.application.models import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    BindDiscordContextCommand,
    BoundDiscordContext,
    CancelDebateCommand,
    CancelledDebate,
    DebateAuthorizationSnapshot,
    DebateSnapshot,
    DeliveryAbandonReason,
    LeaseGrant,
    MetricEvent,
    RetryDebateCommand,
)
from shittim_chest.application.scale_to_zero import (
    EcsRuntimeSnapshot,
    EnqueuedIngress,
    IngressClaimFence,
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    IngressStatusPublication,
    IngressWakeCandidate,
    RuntimeActivity,
    RuntimeState,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationWork,
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


@unique
class RepositoryTransactionStage(StrEnum):
    """Content-free repository transaction stages safe for diagnostics."""

    OUTBOX_PREPARE = "outbox_prepare"
    OUTBOX_CLAIM = "outbox_claim"
    OUTBOX_MARK_SENT = "outbox_mark_sent"
    OUTBOX_RESCHEDULE = "outbox_reschedule"
    PHASE_DELIVERY_TERMINATE = "phase_delivery_terminate"
    PHASE_DELIVERY_ABANDON = "phase_delivery_abandon"
    TERMINAL_FINALIZE = "terminal_finalize"


@unique
class RepositoryTransactionAction(StrEnum):
    """Content-free transaction action kinds safe for diagnostics."""

    ATTEMPT_CAS = "attempt_cas"
    LEASE_FENCE = "lease_fence"
    OUTBOX_OPERATION = "outbox_operation"
    OUTBOX_ACTIVITY = "outbox_activity"
    OUTBOX_SENT_CHECK = "outbox_sent_check"
    PHASE_DELIVERY_PLAN = "phase_delivery_plan"
    RELATED_ITEM_PUT = "related_item_put"
    SLOT_RELEASE = "slot_release"
    ACTIVE_ATTEMPT_COUNT = "active_attempt_count"
    PANEL_REFRESH_COUNT = "panel_refresh_count"
    INGRESS_REQUEST = "ingress_request"
    INGRESS_OPERATION = "ingress_operation"
    STATUS_PUBLICATION = "status_publication"
    UNKNOWN = "unknown"


@unique
class RepositoryCancellationCode(StrEnum):
    """Allowlisted DynamoDB cancellation codes without provider messages."""

    CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailed"
    TRANSACTION_CONFLICT = "TransactionConflict"
    ITEM_COLLECTION_SIZE_LIMIT_EXCEEDED = "ItemCollectionSizeLimitExceeded"
    PROVISIONED_THROUGHPUT_EXCEEDED = "ProvisionedThroughputExceeded"
    THROTTLING_ERROR = "ThrottlingError"
    VALIDATION_ERROR = "ValidationError"
    IDEMPOTENT_PARAMETER_MISMATCH = "IdempotentParameterMismatch"
    UNKNOWN = "Unknown"


class RepositoryTransactionConflict(RepositoryConflict):
    """Expose only safe action/code metadata for a cancelled transaction."""

    __slots__ = ("failures", "reasons_complete", "stage")

    def __init__(
        self,
        *,
        stage: RepositoryTransactionStage,
        failures: tuple[tuple[RepositoryTransactionAction, RepositoryCancellationCode], ...],
        reasons_complete: bool,
    ) -> None:
        if not failures:
            raise ValueError("repository transaction conflict requires at least one failure")
        self.stage = stage
        self.failures = failures
        self.reasons_complete = reasons_complete
        super().__init__("repository transaction condition failed")

    @property
    def retryable(self) -> bool:
        """Retry only complete transient conflicts or the attempt CAS race."""

        if not self.reasons_complete:
            return False
        if all(
            code is RepositoryCancellationCode.TRANSACTION_CONFLICT for _, code in self.failures
        ):
            return True
        return self.stage is RepositoryTransactionStage.TERMINAL_FINALIZE and all(
            (
                action is RepositoryTransactionAction.ATTEMPT_CAS
                and code is RepositoryCancellationCode.CONDITIONAL_CHECK_FAILED
            )
            for action, code in self.failures
        )


class RepositoryIdentityConflict(RepositoryConflict):
    """Raised when a replay reuses an operation with different immutable identity."""


class RepositoryClaimLost(RepositoryConflict):
    """Raised when an atomic write no longer owns its exact ingress claim."""


class RepositoryBusy(Exception):
    """Raised when all three global execution slots are leased."""


class RepositoryQuotaExceeded(Exception):
    """Raised when a Guild has consumed its daily acceptance quota."""


class RepositoryQueueFull(Exception):
    """Raised when the bounded ingress FIFO already contains twenty requests."""


class RepositoryUnavailable(RuntimeError):
    """Raised when a repository SDK call fails before a durable result is known."""

    def __init__(self) -> None:
        super().__init__("repository_unavailable")


class IngressExecutionDeadlineExceeded(RuntimeError):
    """Raised when HTTP ingress must stop starting external SDK operations."""

    def __init__(self) -> None:
        super().__init__("ingress_execution_deadline_exceeded")


class StatusTriggerUnavailable(RuntimeError):
    """Raised when the durable status publisher cannot be kicked asynchronously."""

    def __init__(self) -> None:
        super().__init__("status_trigger_unavailable")


class ReconciliationTriggerUnavailable(RuntimeError):
    """Raised when the runtime reconciler cannot be kicked asynchronously."""

    def __init__(self) -> None:
        super().__init__("reconciliation_trigger_unavailable")


class ParameterReadUnavailable(RuntimeError):
    """Raised when one explicitly configured Parameter Store value cannot be read."""

    def __init__(self) -> None:
        super().__init__("parameter_read_unavailable")


class EcsRuntimeUnavailable(RuntimeError):
    """Raised when the configured ECS singleton cannot be inspected or updated."""

    def __init__(self) -> None:
        super().__init__("ecs_runtime_unavailable")


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


class DebateCommandUseCases(Protocol):
    """SDK-independent command boundary shared by ingress implementations."""

    async def accept_debate(
        self,
        request: AcceptDebateRequest,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedDebate: ...

    async def cancel_debate(
        self,
        command: CancelDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> CancelledDebate: ...

    async def retry_debate(
        self,
        command: RetryDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedRetry: ...

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot: ...

    async def fail_pre_activation(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        kind: IngressKind,
        ingress_claim: IngressClaimFence,
        error_code: str,
    ) -> str: ...


class DebateInteractionUseCases(DebateCommandUseCases, Protocol):
    """Legacy Gateway controller boundary retained until HTTP cutover is complete."""

    async def bind_discord_context(
        self,
        command: BindDiscordContextCommand,
    ) -> BoundDiscordContext: ...

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot: ...

    async def run_debate(self, debate_id: DebateId) -> None: ...


class IngressRepository(Protocol):
    """Persist a bounded FIFO and its strongly consistent operation results."""

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress: ...

    async def get_replay(self, request: IngressRequest) -> EnqueuedIngress | None: ...

    async def get_operation_result(
        self,
        interaction_id: str,
    ) -> IngressOperationResult | None: ...

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def list_active_wake_candidates(self) -> tuple[IngressWakeCandidate, ...]: ...

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

    async def mark_terminal_deadline(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        error_code: str,
    ) -> IngressRequest: ...

    async def mark_claim_terminal(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        status: IngressStatus,
        error_code: str,
    ) -> IngressRequest: ...

    async def request_status_publication(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        at: datetime,
    ) -> IngressRequest: ...

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]: ...

    async def active_count(self) -> int: ...

    async def get_status_publication(
        self,
        interaction_id: str,
    ) -> IngressStatusPublication | None: ...

    async def pending_status_count(self) -> int: ...


class StatusPublicationRepository(Protocol):
    """Fence and settle one desired public status without exposing DynamoDB."""

    async def claim_status_publication(
        self,
        *,
        interaction_id: str,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None: ...

    async def reschedule_status_publication(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
        history_checkpoint: StatusHistoryCheckpoint | None = None,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication: ...

    async def mark_status_delivered(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> IngressStatusPublication: ...

    async def mark_status_failed(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        error_code: str,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication: ...

    async def replace_missing_status_message(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork: ...

    async def list_due_status_publications(
        self,
        *,
        at: datetime,
        limit: int,
    ) -> tuple[IngressStatusPublication, ...]: ...


class RuntimeStateRepository(Protocol):
    """Store one strongly consistent, generation-fenced runtime aggregate."""

    async def get(self) -> RuntimeState | None: ...

    async def request_wake(
        self,
        *,
        interaction_id: str,
        at: datetime,
    ) -> RuntimeState: ...

    async def ensure_wake(
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

    async def begin_idle_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState: ...

    async def begin_unneeded_start_stop(
        self,
        *,
        expected: RuntimeState,
        at: datetime,
    ) -> RuntimeState: ...


class EcsRuntimeControl(Protocol):
    """Control only the configured singleton ECS service through typed values."""

    async def describe(self) -> EcsRuntimeSnapshot: ...

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot: ...


class StatusPublicationTrigger(Protocol):
    """Kick the idempotent public-status publisher with a content-free identifier."""

    async def request_publication(self, interaction_id: str) -> None: ...


class RuntimeReconciliationTrigger(Protocol):
    """Kick lost-wake recovery after the request transaction commits."""

    async def request_reconciliation(self, interaction_id: str) -> None: ...


class ParameterReader(Protocol):
    """Read only an explicitly configured Parameter Store path."""

    async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str: ...


class DebateLookup(Protocol):
    """Read the current aggregate required to authorize a persisted component."""

    async def get(
        self,
        debate_id: DebateId,
        expected_attempt_id: AttemptId,
    ) -> DebateAuthorizationSnapshot | None: ...


class RuntimeActivityInspector(Protocol):
    """Aggregate every durable and process-owned activity required for IDLE."""

    async def inspect(self, *, at: datetime) -> RuntimeActivity: ...


class DiscordGateway(Protocol):
    """Expose Discord readiness and configured request authorization."""

    async def all_identities_ready(self) -> bool: ...

    async def request_is_allowed(self, request: AcceptDebateRequest) -> bool: ...

    async def delivery_target_is_ready(
        self,
        *,
        bot_slot: DiscordBotSlot,
        guild_id: str,
        thread_id: str,
    ) -> bool: ...


class DiscordPublisher(Protocol):
    """Publish only an operation previously persisted by an outbox adapter."""

    async def publish_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None: ...

    async def reconcile_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None: ...


class DiscordOutboxDrainer(Protocol):
    """Drain persisted Discord operations before debate phase work resumes."""

    async def drain(self, *, expected: DebateSnapshot) -> None: ...

    async def terminate(self, *, expected: DebateSnapshot) -> bool: ...


class PanelRefreshRepository(Protocol):
    """Fence durable control-panel convergence independently of debate execution."""

    async def claim_panel_refresh(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None: ...

    async def claim_next_due_panel_refresh(
        self,
        *,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None: ...

    async def complete_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot: ...

    async def reschedule_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> DebateSnapshot: ...

    async def abandon_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> DebateSnapshot: ...

    async def pending_panel_refresh_count(self) -> int: ...

    async def abandoned_panel_refresh_count(self) -> int: ...


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
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation: ...

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation: ...

    async def mark_reconciled_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
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

    async def score_affection(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
    ) -> int: ...

    async def generate_initial_opinion(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        affection_score: int,
    ) -> InitialOpinion: ...

    async def generate_final_proposal(
        self,
        *,
        participant: ParticipantSlot,
        question: str,
        evidence: EvidenceBundle,
        initial_opinions: tuple[InitialOpinion, ...],
        affection_score: int,
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
        affection_score: int,
    ) -> FinalDecision: ...


class DebateRepository(Protocol):
    """Persist application aggregates with conditional-write semantics."""

    async def get_operation_result(
        self,
        operation_id: str,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot | None: ...

    async def create(
        self,
        snapshot: DebateSnapshot,
        *,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot: ...

    async def get(self, debate_id: DebateId) -> DebateSnapshot | None: ...

    async def settle_affection(
        self,
        *,
        expected: DebateSnapshot,
        scores: tuple[int, int, int] | None,
        at: datetime,
    ) -> DebateSnapshot: ...

    async def replace(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot: ...

    async def stage_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        staged: DebateSnapshot,
        operations: tuple[OutboxOperation, ...],
        operation_id: str | None = None,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot: ...

    async def finalize_terminal(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
    ) -> DebateSnapshot: ...

    async def finalize_phase_delivery(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
    ) -> DebateSnapshot: ...

    async def terminate_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
        reason: DeliveryAbandonReason,
    ) -> DebateSnapshot: ...

    async def abandon_terminal_delivery(
        self,
        *,
        expected: DebateSnapshot,
        at: datetime,
        reason: DeliveryAbandonReason,
    ) -> DebateSnapshot: ...

    async def create_retry(
        self,
        *,
        expected_failed: DebateSnapshot,
        retry: DebateSnapshot,
        operation_id: str,
        lease_owner: str,
        ingress_claim: IngressClaimFence | None = None,
    ) -> DebateSnapshot: ...

    async def reclaim_for_ingress(
        self,
        *,
        expected: DebateSnapshot,
        lease_owner: str,
        at: datetime,
        ingress_claim: IngressClaimFence,
    ) -> DebateSnapshot: ...

    async def fail_pre_activation(
        self,
        *,
        expected: DebateSnapshot,
        updated: DebateSnapshot,
        ingress_claim: IngressClaimFence,
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
