"""Debate phase and recovery state machine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum, unique
from itertools import pairwise
from typing import ClassVar, Final, Literal

from shittim_chest.domain.identifiers import AttemptId, DebateId


@unique
class DebatePhase(StrEnum):
    """Persisted phases in the only valid forward order."""

    ACCEPTED = "accepted"
    PREPARING_EVIDENCE = "preparing_evidence"
    COLLECTING_INITIAL_OPINIONS = "collecting_initial_opinions"
    DISCUSSING = "discussing"
    COLLECTING_FINAL_PROPOSALS = "collecting_final_proposals"
    SELECTING_WINNER = "selecting_winner"
    GENERATING_DECISION = "generating_decision"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Return whether this phase has no outgoing transitions."""

        return self in TERMINAL_PHASES


@unique
class RecoveryState(StrEnum):
    """Recovery state kept separately from the debate phase."""

    NONE = "none"
    CHECKPOINTED = "checkpointed"


NORMAL_PHASE_FLOW: Final[tuple[DebatePhase, ...]] = (
    DebatePhase.ACCEPTED,
    DebatePhase.PREPARING_EVIDENCE,
    DebatePhase.COLLECTING_INITIAL_OPINIONS,
    DebatePhase.DISCUSSING,
    DebatePhase.COLLECTING_FINAL_PROPOSALS,
    DebatePhase.SELECTING_WINNER,
    DebatePhase.GENERATING_DECISION,
    DebatePhase.COMPLETED,
)
NON_TERMINAL_PHASES: Final[frozenset[DebatePhase]] = frozenset(NORMAL_PHASE_FLOW[:-1])
TERMINAL_PHASES: Final[frozenset[DebatePhase]] = frozenset(
    {DebatePhase.COMPLETED, DebatePhase.CANCELLED, DebatePhase.FAILED}
)
NORMAL_PHASE_TRANSITIONS: Final[frozenset[tuple[DebatePhase, DebatePhase]]] = frozenset(
    pairwise(NORMAL_PHASE_FLOW)
)
ALLOWED_PHASE_TRANSITIONS: Final[frozenset[tuple[DebatePhase, DebatePhase]]] = frozenset(
    NORMAL_PHASE_TRANSITIONS
    | {(phase, DebatePhase.CANCELLED) for phase in NON_TERMINAL_PHASES}
    | {(phase, DebatePhase.FAILED) for phase in NON_TERMINAL_PHASES}
)


class InvalidStateTransition(Exception):
    """Base class for invalid state-machine operations."""

    code: ClassVar[str]


class InvalidPhaseTransition(InvalidStateTransition):
    """Raised when a phase edge is not part of the state machine."""

    __slots__ = ("current", "target")
    code = "invalid_phase_transition"

    current: DebatePhase
    target: DebatePhase

    def __init__(self, current: DebatePhase, target: DebatePhase) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid debate phase transition: {current.value} -> {target.value}")


RecoveryOperation = Literal["checkpoint", "resume", "phase_transition"]


class InvalidRecoveryTransition(InvalidStateTransition):
    """Raised when checkpoint recovery invariants would be violated."""

    __slots__ = ("current_phase", "operation", "recovery_state")
    code = "invalid_recovery_transition"

    current_phase: DebatePhase
    recovery_state: RecoveryState
    operation: RecoveryOperation

    def __init__(
        self,
        current_phase: DebatePhase,
        recovery_state: RecoveryState,
        operation: RecoveryOperation,
    ) -> None:
        self.current_phase = current_phase
        self.recovery_state = recovery_state
        self.operation = operation
        super().__init__(
            "invalid debate recovery transition: "
            f"operation={operation}, phase={current_phase.value}, "
            f"recovery={recovery_state.value}"
        )


RetryFailureReason = Literal["attempt_id_reused", "source_not_failed"]


class InvalidRetryTransition(InvalidStateTransition):
    """Raised when a new retry attempt cannot be created."""

    __slots__ = ("attempt_id", "reason", "source_phase")
    code = "invalid_retry_transition"

    attempt_id: AttemptId
    reason: RetryFailureReason
    source_phase: DebatePhase

    def __init__(
        self,
        source_phase: DebatePhase,
        attempt_id: AttemptId,
        reason: RetryFailureReason,
    ) -> None:
        self.source_phase = source_phase
        self.attempt_id = attempt_id
        self.reason = reason
        super().__init__(
            "invalid debate retry: "
            f"reason={reason}, phase={source_phase.value}, attempt_id={attempt_id}"
        )


@dataclass(frozen=True, slots=True)
class DebateState:
    """Immutable debate state with explicit phase and recovery transitions."""

    debate_id: DebateId
    attempt_id: AttemptId
    phase: DebatePhase
    recovery_state: RecoveryState
    updated_at: datetime
    retry_of: AttemptId | None = None
    failed_from_phase: DebatePhase | None = None
    schema_version: int = 4

    def __post_init__(self) -> None:
        _validate_utc_timestamp(self.updated_at)
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ValueError("schema version must be a positive integer")
        if self.attempt_id == self.retry_of:
            raise ValueError("attempt ID and retry source must differ")
        if self.phase.is_terminal and self.recovery_state is RecoveryState.CHECKPOINTED:
            raise ValueError("terminal debate state cannot be checkpointed")
        if self.phase is DebatePhase.FAILED:
            if self.failed_from_phase not in NON_TERMINAL_PHASES:
                raise ValueError("failed debate attempt requires a non-terminal source phase")
        elif self.failed_from_phase is not None:
            raise ValueError("only a failed debate attempt may retain its source phase")

    @classmethod
    def accepted(
        cls,
        debate_id: DebateId,
        attempt_id: AttemptId,
        *,
        at: datetime,
    ) -> DebateState:
        """Create a newly accepted debate state."""

        return cls(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase.ACCEPTED,
            recovery_state=RecoveryState.NONE,
            updated_at=at,
        )

    def transition_to(self, target: DebatePhase, *, at: datetime) -> DebateState:
        """Return a new state after a valid phase transition."""

        self._validate_next_timestamp(at)
        if self.recovery_state is RecoveryState.CHECKPOINTED:
            raise InvalidRecoveryTransition(self.phase, self.recovery_state, "phase_transition")
        if (self.phase, target) not in ALLOWED_PHASE_TRANSITIONS:
            raise InvalidPhaseTransition(self.phase, target)
        failed_from_phase = self.phase if target is DebatePhase.FAILED else None
        return replace(
            self,
            phase=target,
            updated_at=at,
            failed_from_phase=failed_from_phase,
        )

    def checkpoint(self, *, at: datetime) -> DebateState:
        """Checkpoint an active debate without changing its phase."""

        self._validate_next_timestamp(at)
        if self.phase.is_terminal or self.recovery_state is not RecoveryState.NONE:
            raise InvalidRecoveryTransition(self.phase, self.recovery_state, "checkpoint")
        return replace(self, recovery_state=RecoveryState.CHECKPOINTED, updated_at=at)

    def resume(self, *, at: datetime) -> DebateState:
        """Resume a checkpointed debate at the same phase."""

        self._validate_next_timestamp(at)
        if self.recovery_state is not RecoveryState.CHECKPOINTED:
            raise InvalidRecoveryTransition(self.phase, self.recovery_state, "resume")
        return replace(self, recovery_state=RecoveryState.NONE, updated_at=at)

    def new_retry_attempt(self, attempt_id: AttemptId, *, at: datetime) -> DebateState:
        """Create a new attempt from this immutable failed attempt."""

        self._validate_next_timestamp(at)
        if self.phase is not DebatePhase.FAILED or self.failed_from_phase is None:
            raise InvalidRetryTransition(self.phase, attempt_id, "source_not_failed")
        # Aggregate-wide uniqueness is enforced by the repository's conditional Put.
        # This state object can reject reuse of the only source attempt it owns.
        if attempt_id == self.attempt_id:
            raise InvalidRetryTransition(self.phase, attempt_id, "attempt_id_reused")
        return DebateState(
            debate_id=self.debate_id,
            attempt_id=attempt_id,
            phase=self.failed_from_phase,
            recovery_state=RecoveryState.NONE,
            updated_at=at,
            retry_of=self.attempt_id,
            schema_version=self.schema_version,
        )

    def _validate_next_timestamp(self, at: datetime) -> None:
        _validate_utc_timestamp(at)
        if at < self.updated_at:
            raise ValueError("state update timestamp cannot move backwards")


def _validate_utc_timestamp(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("state timestamp must be timezone-aware UTC")
