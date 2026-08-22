"""Deterministic all-record ranking aggregation tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.rankings import RankingDataInvalid, build_rankings

NOW = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
PARTICIPANTS = {
    "participant-a": {"display_name": "Arona", "accent": "cyan"},
    "participant-b": {"display_name": "Plana", "accent": "pink"},
    "participant-c": {"display_name": "Participant C", "accent": "blue"},
}


def archive_meta(
    index: int,
    *,
    winner: str,
    requester_key: str,
    requester_name: str,
) -> DynamoItem:
    record_id = f"{index:043d}"
    completed_at = (NOW + timedelta(minutes=index)).isoformat()
    vote_counts = {
        "participant-a": 2 if winner == "participant-a" else 0,
        "participant-b": 2 if winner == "participant-b" else 0,
        "participant-c": 2 if winner == "participant-c" else 0,
    }
    vote_counts[next(slot for slot in vote_counts if slot != winner)] = 1
    return cast(
        DynamoItem,
        {
            "PK": f"RECORD#{record_id}",
            "SK": "META",
            "schema_version": 1,
            "record_type": "archive_meta",
            "record_id": record_id,
            "completed_at": completed_at,
            "gsi1pk": "ARCHIVE#COMPLETED",
            "gsi1sk": f"{completed_at}#{record_id}",
            "winner": winner,
            "participants": deepcopy(PARTICIPANTS),
            "vote_counts": vote_counts,
            "tie_break_applied": False,
            "requester_key": requester_key,
            "requester_display_name": requester_name,
        },
    )


def test_build_rankings_uses_competition_ranks_and_latest_requester_name() -> None:
    items = (
        archive_meta(1, winner="participant-a", requester_key="requester-a", requester_name="Old"),
        archive_meta(2, winner="participant-a", requester_key="requester-b", requester_name="Beta"),
        archive_meta(
            3, winner="participant-b", requester_key="requester-a", requester_name="Alpha"
        ),
        archive_meta(4, winner="participant-b", requester_key="requester-b", requester_name="Beta"),
    )

    snapshot = build_rankings(items, generated_at=NOW)

    assert snapshot.archive_count == 4
    assert [(entry.participant, entry.count, entry.rank) for entry in snapshot.wins] == [
        ("participant-a", 2, 1),
        ("participant-b", 2, 1),
        ("participant-c", 0, 3),
    ]
    assert [
        (entry.requester_key, entry.display_name, entry.count, entry.rank)
        for entry in snapshot.requests
    ] == [
        ("requester-a", "Alpha", 2, 1),
        ("requester-b", "Beta", 2, 1),
    ]


def test_build_rankings_bounds_requesters_to_ten_with_stable_order() -> None:
    items = tuple(
        archive_meta(
            index,
            winner="participant-a",
            requester_key=f"requester-{index:02d}",
            requester_name=f"Requester {index:02d}",
        )
        for index in range(1, 13)
    )

    snapshot = build_rankings(items, generated_at=NOW)

    assert len(snapshot.requests) == 10
    assert [entry.requester_key for entry in snapshot.requests] == [
        f"requester-{index:02d}" for index in range(1, 11)
    ]
    assert all(entry.rank == 1 for entry in snapshot.requests)


def test_build_rankings_returns_an_explicit_empty_snapshot() -> None:
    snapshot = build_rankings((), generated_at=NOW)

    assert snapshot.archive_count == 0
    assert snapshot.wins == ()
    assert snapshot.requests == ()


def test_build_rankings_rejects_duplicate_or_malformed_archive_metadata() -> None:
    first = archive_meta(
        1,
        winner="participant-a",
        requester_key="requester-a",
        requester_name="Requester",
    )
    with pytest.raises(RankingDataInvalid, match="duplicate"):
        build_rankings((first, dict(first)), generated_at=NOW)

    malformed = dict(first)
    malformed["schema_version"] = 2
    with pytest.raises(RankingDataInvalid, match="identity"):
        build_rankings((malformed,), generated_at=NOW)


def test_build_rankings_rejects_inconsistent_saved_winner_summary() -> None:
    item = archive_meta(
        1,
        winner="participant-a",
        requester_key="requester-a",
        requester_name="Requester",
    )
    item["vote_counts"] = {
        "participant-a": 0,
        "participant-b": 2,
        "participant-c": 1,
    }

    with pytest.raises(RankingDataInvalid, match="winner summary"):
        build_rankings((item,), generated_at=NOW)


def test_build_rankings_accepts_historical_null_avatar_compatibility_member() -> None:
    item = archive_meta(
        1,
        winner="participant-a",
        requester_key="requester-a",
        requester_name="Requester",
    )
    participants = cast(dict[str, dict[str, object]], item["participants"])
    for profile in participants.values():
        profile["avatar_asset_key"] = None

    snapshot = build_rankings((item,), generated_at=NOW)

    assert snapshot.archive_count == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("avatar_asset_key", "participants/participant-a/historical.webp"),
        ("unexpected", None),
    ),
)
def test_build_rankings_rejects_non_null_or_unknown_participant_fields(
    field: str,
    value: object,
) -> None:
    item = archive_meta(
        1,
        winner="participant-a",
        requester_key="requester-a",
        requester_name="Requester",
    )
    participants = cast(dict[str, dict[str, object]], item["participants"])
    participants["participant-a"][field] = value

    with pytest.raises(RankingDataInvalid, match="participant presentation"):
        build_rankings((item,), generated_at=NOW)


def test_build_rankings_requires_an_aware_generation_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_rankings((), generated_at=datetime(2026, 8, 22))
