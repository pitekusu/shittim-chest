"""Archive projection contract tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from shittim_chest.application import DebateSnapshot
from shittim_chest.domain import DebatePhase, FinalDecision, ParticipantSlot
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.archive import ProjectionRejected, project_completed_debate

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"


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
