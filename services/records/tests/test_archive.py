"""Archive projection contract tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from shittim_chest.adapters.dynamodb.serializer import PREVIOUS_SCHEMA_VERSION
from shittim_chest.application import DebateSnapshot
from shittim_chest.domain import (
    AFFECTION_RULES_VERSION,
    AffectionAssessment,
    AffectionAssessmentStatus,
    AttemptId,
    DebateId,
    DebatePhase,
    FinalDecision,
    ParticipantAffection,
    ParticipantSlot,
)
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.archive import (
    ProjectionRejected,
    derive_requester_key,
    project_completed_debate,
)

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"


def test_requester_key_matches_the_core_shared_hmac_vector() -> None:
    assert (
        derive_requester_key(b"i" * 32, "private-requester")
        == "xDtrTAPtslo-r6StMr0FS6GliBLskiI-CbXUlFIlbfI"
    )


def test_completed_projection_contains_exact_archive_shape_without_private_ids() -> None:
    source = completed_snapshot()

    projection = project_completed_debate(
        source,
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )

    assert len(projection.items) == 12
    assert {item["SK"] for item in projection.items} == {
        "META",
        "INITIAL#participant-a",
        "INITIAL#participant-b",
        "INITIAL#participant-c",
        "FINAL#participant-a",
        "FINAL#participant-b",
        "FINAL#participant-c",
        "VOTE#participant-a",
        "VOTE#participant-b",
        "VOTE#participant-c",
        "DECISION",
        "PROJECTION#V1",
    }
    meta = next(item for item in projection.items if item["SK"] == "META")
    assert meta["winner"] == "participant-b"
    assert meta["vote_counts"] == {
        "participant-a": 1,
        "participant-b": 2,
        "participant-c": 0,
    }
    assert meta["tie_break_applied"] is False
    assert "avatar_asset_key" not in repr(meta["participants"])
    serialized = repr(projection.items)
    for private_value in (
        source.requester_id,
        source.guild_id,
        source.channel_id,
        str(source.state.debate_id),
        str(source.state.attempt_id),
        source.thread_id,
    ):
        assert private_value is not None
        assert private_value not in serialized


def test_projection_is_deterministic_and_public_identity_is_hmac_derived() -> None:
    source = completed_snapshot()

    first = project_completed_debate(
        source,
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )
    second = project_completed_debate(
        source,
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )

    assert first == second
    assert len(first.record_id) == 43
    assert first.source_fingerprint != first.record_id

    changed_requester = project_completed_debate(
        replace(source, requester_id="different-requester"),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )
    assert changed_requester.record_id == first.record_id
    assert changed_requester.source_fingerprint != first.source_fingerprint


def test_projection_accepts_deployed_previous_source_schema() -> None:
    source = completed_snapshot()
    previous = replace(
        source,
        state=replace(source.state, schema_version=PREVIOUS_SCHEMA_VERSION),
    )

    projection = project_completed_debate(
        previous,
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )

    assert projection.record_id


def test_affection_projection_uses_archive_v2_and_exposes_only_question_changes() -> None:
    source = completed_snapshot()
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 500, 35, 35, 535),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 10, -43, -10, 0),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 987, 50, 13, 1000),
        ),
        assessed_at=NOW,
    )

    projection = project_completed_debate(
        replace(source, affection_assessment=assessment),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )

    assert projection.schema_version == 2
    assert {item["schema_version"] for item in projection.items} == {2}
    assert any(item["SK"] == "PROJECTION#V2" for item in projection.items)
    meta = next(item for item in projection.items if item["SK"] == "META")
    assert meta["affection"] == {
        "status": "applied",
        "rubric_version": AFFECTION_RULES_VERSION,
        "participants": [
            {
                "participant": "participant-a",
                "before": 500,
                "question_score": 35,
                "applied_delta": 35,
                "after": 535,
            },
            {
                "participant": "participant-b",
                "before": 10,
                "question_score": -43,
                "applied_delta": -10,
                "after": 0,
            },
            {
                "participant": "participant-c",
                "before": 987,
                "question_score": 50,
                "applied_delta": 13,
                "after": 1000,
            },
        ],
    }
    assert source.requester_id not in repr(projection.items)


def test_v8_affection_replay_preserves_pre_pr1_fingerprint_and_marker_schema() -> None:
    source = completed_snapshot()
    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 500, 35, 35, 535),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 10, -43, -10, 0),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 987, 50, 13, 1000),
        ),
        assessed_at=NOW,
    )
    deployed_v8 = replace(
        source,
        state=replace(
            source.state,
            debate_id=DebateId.parse("019d2c1f-0000-7000-8000-a00000000011"),
            attempt_id=AttemptId.parse("019d2c1f-0000-7000-8000-a00000000012"),
            schema_version=PREVIOUS_SCHEMA_VERSION,
        ),
        affection_assessment=assessment,
    )

    replay = project_completed_debate(
        deployed_v8,
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
        source_schema_version=PREVIOUS_SCHEMA_VERSION,
    )

    assert replay.source_fingerprint == (
        "19f588e022ec434caf596a87a8c0c5549f75456008f4ad3b90b9a04bcce7fce4"
    )
    marker = next(item for item in replay.items if item["record_type"] == "projection_marker")
    assert marker["SK"] == "PROJECTION#V2"
    assert marker["source_schema_version"] == 8
    assert marker["source_fingerprint"] == replay.source_fingerprint

    current_v9 = project_completed_debate(
        replace(deployed_v8, state=replace(deployed_v8.state, schema_version=9)),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
        source_schema_version=9,
    )
    assert current_v9.source_fingerprint != replay.source_fingerprint


@pytest.mark.parametrize(
    "mutation",
    (
        lambda source: replace(
            source,
            state=replace(source.state, phase=DebatePhase.FAILED),
            terminal_delivery=None,
            error_code="failure",
        ),
        lambda source: replace(source, terminal_delivery=None),
        lambda source: replace(source, initial_opinions=source.initial_opinions[:2]),
        lambda source: replace(source, final_proposals=source.final_proposals[:2]),
        lambda source: replace(source, votes=source.votes[:2]),
        lambda source: replace(
            source,
            final_decision=FinalDecision(
                ParticipantSlot.PARTICIPANT_A,
                "Wrong winner",
                (),
                (),
            ),
        ),
    ),
)
def test_projection_rejects_non_publishable_aggregates(
    mutation: Callable[[DebateSnapshot], DebateSnapshot],
) -> None:
    source = completed_snapshot()

    with pytest.raises((ProjectionRejected, ValueError)):
        project_completed_debate(
            mutation(source),
            identity_hmac_key=HMAC_KEY,
            presentation=presentation(),
            projected_at=NOW,
        )


def test_presentation_requires_exact_unique_participants() -> None:
    raw = presentation().model_dump(mode="json")
    del raw["participants"]["participant-c"]

    with pytest.raises(ValueError, match="exactly"):
        type(presentation()).model_validate(raw)


def test_presentation_rejects_historical_participant_avatar_keys() -> None:
    raw = presentation().model_dump(mode="json")
    raw["participants"]["participant-a"]["avatar_asset_key"] = (
        "participants/participant-a/history.webp"
    )

    with pytest.raises(ValueError):
        type(presentation()).model_validate(raw)
