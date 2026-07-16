"""Tests for the debate phase and recovery state machine."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shittim_chest.domain import (
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    InvalidPhaseTransition,
    InvalidRecoveryTransition,
    InvalidRetryTransition,
    RecoveryState,
)
from shittim_chest.domain.debate_state import (
    ALLOWED_PHASE_TRANSITIONS,
    NON_TERMINAL_PHASES,
    TERMINAL_PHASES,
)

STARTED_AT = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
EXPECTED_NON_TERMINAL_PHASES = frozenset(
    {
        DebatePhase.ACCEPTED,
        DebatePhase.PREPARING_EVIDENCE,
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        DebatePhase.DISCUSSING,
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        DebatePhase.SELECTING_WINNER,
        DebatePhase.GENERATING_DECISION,
    }
)
EXPECTED_TERMINAL_PHASES = frozenset(
    {DebatePhase.COMPLETED, DebatePhase.CANCELLED, DebatePhase.FAILED}
)
EXPECTED_PHASE_TRANSITIONS = frozenset(
    {
        (DebatePhase.ACCEPTED, DebatePhase.PREPARING_EVIDENCE),
        (DebatePhase.PREPARING_EVIDENCE, DebatePhase.COLLECTING_INITIAL_OPINIONS),
        (DebatePhase.COLLECTING_INITIAL_OPINIONS, DebatePhase.DISCUSSING),
        (DebatePhase.DISCUSSING, DebatePhase.COLLECTING_FINAL_PROPOSALS),
        (DebatePhase.COLLECTING_FINAL_PROPOSALS, DebatePhase.SELECTING_WINNER),
        (DebatePhase.SELECTING_WINNER, DebatePhase.GENERATING_DECISION),
        (DebatePhase.GENERATING_DECISION, DebatePhase.COMPLETED),
        (DebatePhase.ACCEPTED, DebatePhase.CANCELLED),
        (DebatePhase.PREPARING_EVIDENCE, DebatePhase.CANCELLED),
        (DebatePhase.COLLECTING_INITIAL_OPINIONS, DebatePhase.CANCELLED),
        (DebatePhase.DISCUSSING, DebatePhase.CANCELLED),
        (DebatePhase.COLLECTING_FINAL_PROPOSALS, DebatePhase.CANCELLED),
        (DebatePhase.SELECTING_WINNER, DebatePhase.CANCELLED),
        (DebatePhase.GENERATING_DECISION, DebatePhase.CANCELLED),
        (DebatePhase.ACCEPTED, DebatePhase.FAILED),
        (DebatePhase.PREPARING_EVIDENCE, DebatePhase.FAILED),
        (DebatePhase.COLLECTING_INITIAL_OPINIONS, DebatePhase.FAILED),
        (DebatePhase.DISCUSSING, DebatePhase.FAILED),
        (DebatePhase.COLLECTING_FINAL_PROPOSALS, DebatePhase.FAILED),
        (DebatePhase.SELECTING_WINNER, DebatePhase.FAILED),
        (DebatePhase.GENERATING_DECISION, DebatePhase.FAILED),
    }
)


def make_state(
    phase: DebatePhase = DebatePhase.ACCEPTED,
    *,
    recovery_state: RecoveryState = RecoveryState.NONE,
    attempt_id: AttemptId | None = None,
    retry_of: AttemptId | None = None,
    failed_from_phase: DebatePhase | None = None,
    schema_version: int = 1,
) -> DebateState:
    if phase is DebatePhase.FAILED and failed_from_phase is None:
        failed_from_phase = DebatePhase.ACCEPTED
    return DebateState(
        debate_id=DebateId.new(),
        attempt_id=attempt_id or AttemptId.new(),
        phase=phase,
        recovery_state=recovery_state,
        updated_at=STARTED_AT,
        retry_of=retry_of,
        failed_from_phase=failed_from_phase,
        schema_version=schema_version,
    )


def test_persisted_enum_values_are_explicit_and_stable() -> None:
    assert tuple(phase.value for phase in DebatePhase) == (
        "accepted",
        "preparing_evidence",
        "collecting_initial_opinions",
        "discussing",
        "collecting_final_proposals",
        "selecting_winner",
        "generating_decision",
        "completed",
        "cancelled",
        "failed",
    )
    assert tuple(state.value for state in RecoveryState) == ("none", "checkpointed")


def test_phase_transition_matrix_contains_exactly_the_21_designed_edges() -> None:
    assert len(EXPECTED_PHASE_TRANSITIONS) == 21
    assert ALLOWED_PHASE_TRANSITIONS == EXPECTED_PHASE_TRANSITIONS

    for current in DebatePhase:
        for target in DebatePhase:
            state = make_state(current)
            if (current, target) in EXPECTED_PHASE_TRANSITIONS:
                transitioned = state.transition_to(target, at=STARTED_AT + timedelta(seconds=1))
                assert transitioned.phase is target
                assert transitioned.debate_id == state.debate_id
                assert transitioned.attempt_id == state.attempt_id
                assert transitioned.retry_of == state.retry_of
                assert transitioned.recovery_state is RecoveryState.NONE
                assert transitioned.schema_version == state.schema_version
                if target is DebatePhase.FAILED:
                    assert transitioned.failed_from_phase is current
                else:
                    assert transitioned.failed_from_phase is None
                assert state.phase is current
            else:
                with pytest.raises(InvalidPhaseTransition) as raised:
                    state.transition_to(target, at=STARTED_AT + timedelta(seconds=1))
                assert raised.value.code == "invalid_phase_transition"
                assert raised.value.current is current
                assert raised.value.target is target


def test_terminal_and_non_terminal_sets_are_complete_and_disjoint() -> None:
    assert NON_TERMINAL_PHASES == EXPECTED_NON_TERMINAL_PHASES
    assert TERMINAL_PHASES == EXPECTED_TERMINAL_PHASES
    assert frozenset(DebatePhase) == EXPECTED_NON_TERMINAL_PHASES | EXPECTED_TERMINAL_PHASES
    assert EXPECTED_NON_TERMINAL_PHASES.isdisjoint(EXPECTED_TERMINAL_PHASES)


@pytest.mark.parametrize("phase", tuple(EXPECTED_NON_TERMINAL_PHASES))
def test_checkpoint_and_resume_preserve_phase_and_identity(phase: DebatePhase) -> None:
    state = make_state(phase, schema_version=3)

    checkpointed = state.checkpoint(at=STARTED_AT + timedelta(seconds=1))
    resumed = checkpointed.resume(at=STARTED_AT + timedelta(seconds=2))

    assert checkpointed.phase is phase
    assert checkpointed.recovery_state is RecoveryState.CHECKPOINTED
    assert resumed.phase is phase
    assert resumed.recovery_state is RecoveryState.NONE
    assert resumed.debate_id == state.debate_id
    assert resumed.schema_version == 3
    assert state.recovery_state is RecoveryState.NONE


def test_phase_transition_is_blocked_while_checkpointed() -> None:
    state = make_state().checkpoint(at=STARTED_AT)

    with pytest.raises(InvalidRecoveryTransition) as raised:
        state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=STARTED_AT)

    assert raised.value.code == "invalid_recovery_transition"
    assert raised.value.operation == "phase_transition"
    assert raised.value.current_phase is DebatePhase.ACCEPTED
    assert raised.value.recovery_state is RecoveryState.CHECKPOINTED


@pytest.mark.parametrize("phase", tuple(EXPECTED_TERMINAL_PHASES))
def test_terminal_phase_cannot_be_checkpointed(phase: DebatePhase) -> None:
    state = make_state(phase)

    with pytest.raises(InvalidRecoveryTransition) as raised:
        state.checkpoint(at=STARTED_AT)

    assert raised.value.operation == "checkpoint"


def test_duplicate_checkpoint_and_resume_without_checkpoint_are_rejected() -> None:
    state = make_state()
    checkpointed = state.checkpoint(at=STARTED_AT)

    with pytest.raises(InvalidRecoveryTransition) as duplicate:
        checkpointed.checkpoint(at=STARTED_AT)
    with pytest.raises(InvalidRecoveryTransition) as absent:
        state.resume(at=STARTED_AT)

    assert duplicate.value.operation == "checkpoint"
    assert absent.value.operation == "resume"


def test_terminal_checkpointed_state_is_rejected_at_boundary() -> None:
    with pytest.raises(ValueError, match="terminal debate state"):
        make_state(DebatePhase.COMPLETED, recovery_state=RecoveryState.CHECKPOINTED)


@pytest.mark.parametrize(
    "timestamp",
    [
        datetime(2026, 7, 16, 0, 0),
        datetime(2026, 7, 16, 9, 0, tzinfo=timezone(timedelta(hours=9))),
    ],
)
def test_state_requires_an_explicit_utc_timestamp(timestamp: datetime) -> None:
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        DebateState.accepted(DebateId.new(), AttemptId.new(), at=timestamp)


def test_timestamp_cannot_move_backwards_but_may_stay_equal() -> None:
    state = make_state()

    with pytest.raises(ValueError, match="cannot move backwards"):
        state.transition_to(
            DebatePhase.PREPARING_EVIDENCE,
            at=STARTED_AT - timedelta(microseconds=1),
        )

    transitioned = state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=STARTED_AT)
    assert transitioned.updated_at == STARTED_AT


@pytest.mark.parametrize("schema_version", [0, -1, True, 1.5, "1"])
def test_schema_version_must_be_a_positive_non_boolean_integer(schema_version: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        make_state(schema_version=cast(int, schema_version))


def test_accepted_factory_sets_initial_state() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()

    state = DebateState.accepted(debate_id, attempt_id, at=STARTED_AT)

    assert state == DebateState(
        debate_id=debate_id,
        attempt_id=attempt_id,
        phase=DebatePhase.ACCEPTED,
        recovery_state=RecoveryState.NONE,
        updated_at=STARTED_AT,
    )


def test_state_is_frozen_and_slotted() -> None:
    state = make_state()

    assert not hasattr(state, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(state, "phase", DebatePhase.FAILED)  # noqa: B010


@given(
    transition=st.sampled_from(tuple(EXPECTED_PHASE_TRANSITIONS)),
    elapsed_microseconds=st.integers(min_value=0, max_value=10_000_000),
)
def test_all_legal_transitions_preserve_domain_identity_and_schema(
    transition: tuple[DebatePhase, DebatePhase],
    elapsed_microseconds: int,
) -> None:
    current, target = transition
    retry_of = AttemptId.new()
    state = make_state(current, retry_of=retry_of, schema_version=7)

    transitioned = state.transition_to(
        target,
        at=STARTED_AT + timedelta(microseconds=elapsed_microseconds),
    )

    assert transitioned.debate_id == state.debate_id
    assert transitioned.attempt_id == state.attempt_id
    assert transitioned.retry_of == retry_of
    assert transitioned.schema_version == 7
    assert transitioned.phase is target
    assert transitioned.updated_at >= state.updated_at


@pytest.mark.parametrize("source_phase", tuple(EXPECTED_NON_TERMINAL_PHASES))
def test_failed_transition_records_its_source_phase(source_phase: DebatePhase) -> None:
    state = make_state(source_phase)

    failed = state.transition_to(DebatePhase.FAILED, at=STARTED_AT)

    assert failed.phase is DebatePhase.FAILED
    assert failed.failed_from_phase is source_phase
    assert state.failed_from_phase is None


@pytest.mark.parametrize("source_phase", tuple(EXPECTED_NON_TERMINAL_PHASES))
def test_retry_creates_a_new_attempt_from_immutable_failure(source_phase: DebatePhase) -> None:
    initial = make_state(source_phase, schema_version=4)
    failed = initial.transition_to(DebatePhase.FAILED, at=STARTED_AT)
    new_attempt_id = AttemptId.new()

    retry = failed.new_retry_attempt(new_attempt_id, at=STARTED_AT + timedelta(seconds=1))

    assert retry.debate_id == failed.debate_id
    assert retry.attempt_id == new_attempt_id
    assert retry.retry_of == failed.attempt_id
    assert retry.phase is source_phase
    assert retry.recovery_state is RecoveryState.NONE
    assert retry.failed_from_phase is None
    assert retry.schema_version == 4
    assert failed.phase is DebatePhase.FAILED
    assert failed.failed_from_phase is source_phase


def test_retry_chain_points_to_the_immediately_previous_attempt() -> None:
    first = make_state(DebatePhase.DISCUSSING)
    first_failure = first.transition_to(DebatePhase.FAILED, at=STARTED_AT)
    second = first_failure.new_retry_attempt(
        AttemptId.new(),
        at=STARTED_AT + timedelta(seconds=1),
    )
    second_progress = second.transition_to(
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        at=STARTED_AT + timedelta(seconds=2),
    )
    second_failure = second_progress.transition_to(
        DebatePhase.FAILED,
        at=STARTED_AT + timedelta(seconds=3),
    )

    third = second_failure.new_retry_attempt(
        AttemptId.new(),
        at=STARTED_AT + timedelta(seconds=4),
    )

    assert third.debate_id == first.debate_id
    assert third.retry_of == second.attempt_id
    assert third.retry_of != first.attempt_id
    assert third.phase is DebatePhase.COLLECTING_FINAL_PROPOSALS


@pytest.mark.parametrize(
    "phase",
    tuple(phase for phase in DebatePhase if phase is not DebatePhase.FAILED),
)
def test_retry_rejects_every_non_failed_source(phase: DebatePhase) -> None:
    active = make_state(phase)
    with pytest.raises(InvalidRetryTransition) as not_failed:
        active.new_retry_attempt(AttemptId.new(), at=STARTED_AT)

    assert not_failed.value.code == "invalid_retry_transition"
    assert not_failed.value.reason == "source_not_failed"


def test_retry_rejects_source_attempt_id_reuse() -> None:
    failed = make_state().transition_to(DebatePhase.FAILED, at=STARTED_AT)

    with pytest.raises(InvalidRetryTransition) as reused:
        failed.new_retry_attempt(failed.attempt_id, at=STARTED_AT)

    assert reused.value.code == "invalid_retry_transition"
    assert reused.value.reason == "attempt_id_reused"


def test_retry_timestamp_must_be_utc_and_non_decreasing() -> None:
    failed = make_state().transition_to(DebatePhase.FAILED, at=STARTED_AT)

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        failed.new_retry_attempt(AttemptId.new(), at=datetime(2026, 7, 16))
    with pytest.raises(ValueError, match="cannot move backwards"):
        failed.new_retry_attempt(
            AttemptId.new(),
            at=STARTED_AT - timedelta(microseconds=1),
        )


def test_attempt_retry_boundary_invariants_are_enforced() -> None:
    attempt_id = AttemptId.new()
    debate_id = DebateId.new()

    with pytest.raises(ValueError, match="requires a non-terminal source phase"):
        DebateState(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase.FAILED,
            recovery_state=RecoveryState.NONE,
            updated_at=STARTED_AT,
        )
    with pytest.raises(ValueError, match="only a failed debate attempt"):
        DebateState(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase.ACCEPTED,
            recovery_state=RecoveryState.NONE,
            updated_at=STARTED_AT,
            failed_from_phase=DebatePhase.DISCUSSING,
        )
    with pytest.raises(ValueError, match="requires a non-terminal source phase"):
        DebateState(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase.FAILED,
            recovery_state=RecoveryState.NONE,
            updated_at=STARTED_AT,
            failed_from_phase=DebatePhase.COMPLETED,
        )
    with pytest.raises(ValueError, match="retry source must differ"):
        DebateState(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase.ACCEPTED,
            recovery_state=RecoveryState.NONE,
            updated_at=STARTED_AT,
            retry_of=attempt_id,
        )
