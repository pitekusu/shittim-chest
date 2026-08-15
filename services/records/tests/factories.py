"""Validated Records test aggregates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from shittim_chest.application import DebateSnapshot, TerminalDeliveryPlan
from shittim_chest.domain import (
    PARTICIPANTS,
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
    Vote,
)

from shittim_records.archive import RecordsPresentationConfig

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def completed_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    state = DebateState(
        debate_id=debate_id,
        attempt_id=attempt_id,
        phase=DebatePhase.COMPLETED,
        recovery_state=RecoveryState.NONE,
        updated_at=NOW,
    )
    votes = (
        Vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_B, 4, 4, 4, "A reason"),
        Vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A, 3, 3, 3, "B reason"),
        Vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B, 5, 5, 5, "C reason"),
    )
    delivery = TerminalDeliveryPlan(
        target_phase=DebatePhase.COMPLETED,
        operation_ids=("terminal-operation",),
        content_hashes=("a" * 64,),
        staged_at=NOW - timedelta(seconds=2),
        completed_at=NOW - timedelta(seconds=1),
    )
    return DebateSnapshot(
        state=state,
        question="Which option should we choose?",
        requester_id="discord-user-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        created_at=NOW - timedelta(minutes=5),
        attempt_created_at=NOW - timedelta(minutes=5),
        starter_message_id="starter-message",
        thread_id="thread-id",
        control_panel_message_id="panel-message",
        initial_opinions=tuple(
            InitialOpinion(slot, f"{slot.value} summary", f"{slot.value} initial")
            for slot in PARTICIPANTS
        ),
        final_proposals=tuple(
            FinalProposal(slot, f"{slot.value} title", f"{slot.value} final")
            for slot in PARTICIPANTS
        ),
        votes=votes,
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "Stored decision",
            ("Action one",),
            ("Caveat one",),
            "Victory message",
        ),
        terminal_delivery=delivery,
    )


def presentation() -> RecordsPresentationConfig:
    return RecordsPresentationConfig.model_validate(
        {
            "schema_version": 1,
            "presentation_version": "v0001",
            "participants": {
                "participant-a": {"display_name": "Arona", "accent": "cyan"},
                "participant-b": {"display_name": "Plana", "accent": "pink"},
                "participant-c": {"display_name": "Participant C", "accent": "blue"},
            },
        }
    )
