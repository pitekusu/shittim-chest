"""Deterministic all-record ranking aggregation tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.rankings import (
    AffectionProfileSeed,
    RankingDataInvalid,
    RankingService,
    build_rankings,
)

NOW = datetime(2026, 8, 22, 0, 0, tzinfo=UTC)
PARTICIPANTS = {
    "participant-a": {"display_name": "Arona", "accent": "cyan"},
    "participant-b": {"display_name": "Plana", "accent": "pink"},
    "participant-c": {"display_name": "Participant C", "accent": "blue"},
}
ALPHA_KEY = "a" * 43
BETA_KEY = "b" * 43
GAMMA_KEY = "c" * 43


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


def affection_profile(
    requester_key: str,
    display_name: str,
    scores: tuple[int, int, int],
) -> DynamoItem:
    return {
        "PK": "AFFECTION#PROFILE",
        "SK": requester_key,
        "schema_version": 1,
        "record_type": "affection_profile",
        "source_version": 1,
        "display_name": display_name,
        "scores": dict(zip(PARTICIPANTS, scores, strict=True)),
        "updated_at": NOW.isoformat(),
    }


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


def test_affection_rankings_include_every_profile_with_competition_ranks() -> None:
    archives = (
        archive_meta(1, winner="participant-a", requester_key=ALPHA_KEY, requester_name="Alpha"),
        archive_meta(2, winner="participant-b", requester_key=BETA_KEY, requester_name="Beta"),
    )
    profiles = (
        affection_profile(ALPHA_KEY, "Alpha", (700, 500, 100))
        | {"schema_version": 2, "reset_count": 2, "memorial_cycle": 3},
        affection_profile(BETA_KEY, "Beta", (700, 600, 900)),
        affection_profile(GAMMA_KEY, "Gamma", (500, 600, 900)),
    )

    snapshot = build_rankings(archives, affection_profiles=profiles, generated_at=NOW)

    assert snapshot.affection_profile_count == 3
    by_participant = {ranking.participant: ranking for ranking in snapshot.affection}
    assert [
        (entry.requester_key, entry.score, entry.rank, entry.reset_count)
        for entry in by_participant["participant-a"].entries
    ] == [(ALPHA_KEY, 700, 1, 2), (BETA_KEY, 700, 1, 0), (GAMMA_KEY, 500, 3, 0)]
    assert [entry.requester_key for entry in by_participant["participant-b"].entries] == [
        BETA_KEY,
        GAMMA_KEY,
        ALPHA_KEY,
    ]
    assert len(by_participant["participant-c"].entries) == 3


def test_refresh_seeds_archived_requesters_at_500_without_replacing_real_profiles() -> None:
    archives = (
        archive_meta(1, winner="participant-a", requester_key=ALPHA_KEY, requester_name="Alpha"),
        archive_meta(2, winner="participant-b", requester_key=BETA_KEY, requester_name="Beta"),
    )

    class Source:
        def __init__(self) -> None:
            self.profiles = [affection_profile(ALPHA_KEY, "Alpha", (700, 600, 500))]
            self.seeded: list[str] = []

        def list_completed_meta(self) -> tuple[DynamoItem, ...]:
            return archives

        def list_affection_profiles(self) -> tuple[DynamoItem, ...]:
            return tuple(self.profiles)

        def seed_default_affection_profiles(
            self,
            seeds: tuple[AffectionProfileSeed, ...],
            *,
            updated_at: datetime,
        ) -> None:
            for seed in seeds:
                key = seed.requester_key
                name = seed.display_name
                self.seeded.append(key)
                self.profiles.append(
                    affection_profile(key, name, (500, 500, 500))
                    | {"source_version": 0, "updated_at": updated_at.isoformat()}
                )

    class Store:
        def __init__(self) -> None:
            self.snapshot: object = None

        def save_rankings(self, snapshot: object) -> None:
            self.snapshot = snapshot

    source = Source()
    store = Store()

    snapshot = RankingService(source=cast(Any, source), store=cast(Any, store)).refresh(now=NOW)

    assert source.seeded == [BETA_KEY]
    participant_a = next(
        ranking for ranking in snapshot.affection if ranking.participant == "participant-a"
    )
    assert [(entry.requester_key, entry.score) for entry in participant_a.entries] == [
        (ALPHA_KEY, 700),
        (BETA_KEY, 500),
    ]


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
    malformed["schema_version"] = 3
    with pytest.raises(RankingDataInvalid, match="identity"):
        build_rankings((malformed,), generated_at=NOW)


def test_build_rankings_rejects_inconsistent_affection_reset_cycle() -> None:
    profile = affection_profile(ALPHA_KEY, "Alpha", (500, 500, 500)) | {
        "schema_version": 2,
        "reset_count": 2,
        "memorial_cycle": 2,
    }

    with pytest.raises(RankingDataInvalid, match="affection profile"):
        build_rankings((), affection_profiles=(profile,), generated_at=NOW)


def test_build_rankings_rejects_non_ascii_opaque_profile_or_unlock_key() -> None:
    unicode_profile = affection_profile("é" * 43, "Alpha", (500, 500, 500))
    with pytest.raises(RankingDataInvalid, match="affection profile"):
        build_rankings((), affection_profiles=(unicode_profile,), generated_at=NOW)

    malformed_unlock = affection_profile(ALPHA_KEY, "Alpha", (1000, 500, 500)) | {
        "schema_version": 2,
        "reset_count": 0,
        "memorial_cycle": 1,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(),
        "unlock_record_id": "é" * 43,
        "unlock_display_name": "Alpha",
        "unlock_memorial_cycle": 1,
        "unlock_retroactive": False,
    }
    with pytest.raises(RankingDataInvalid, match="memorial unlock"):
        build_rankings((), affection_profiles=(malformed_unlock,), generated_at=NOW)


@pytest.mark.parametrize("retroactive", (False, True))
def test_build_rankings_accepts_memorial_unlock_provenance(retroactive: bool) -> None:
    unlocked = affection_profile(ALPHA_KEY, "Alpha", (1000, 500, 500)) | {
        "schema_version": 2,
        "reset_count": 0,
        "memorial_cycle": 1,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(),
        "unlock_record_id": "d" * 43,
        "unlock_display_name": "Alpha",
        "unlock_memorial_cycle": 1,
        "unlock_retroactive": retroactive,
    }

    snapshot = build_rankings((), affection_profiles=(unlocked,), generated_at=NOW)

    assert snapshot.affection_profile_count == 1


@pytest.mark.parametrize("retroactive", (None, "false"))
def test_build_rankings_rejects_missing_or_non_boolean_unlock_provenance(
    retroactive: object,
) -> None:
    unlocked = affection_profile(ALPHA_KEY, "Alpha", (1000, 500, 500)) | {
        "schema_version": 2,
        "reset_count": 0,
        "memorial_cycle": 1,
        "unlocked_participant": "participant-a",
        "unlocked_at": NOW.isoformat(),
        "unlock_record_id": "d" * 43,
        "unlock_display_name": "Alpha",
        "unlock_memorial_cycle": 1,
        "unlock_retroactive": retroactive,
    }
    if retroactive is None:
        del unlocked["unlock_retroactive"]

    with pytest.raises(RankingDataInvalid, match=r"profile|memorial unlock"):
        build_rankings((), affection_profiles=cast(Any, (unlocked,)), generated_at=NOW)


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


def test_build_rankings_accepts_archive_v2_metadata() -> None:
    item = archive_meta(
        1,
        winner="participant-a",
        requester_key="requester-a",
        requester_name="Requester",
    )
    item["schema_version"] = 2
    item["affection"] = {
        "status": "applied",
        "rubric_version": "affection-rubric-v1",
        "participants": [],
    }

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
