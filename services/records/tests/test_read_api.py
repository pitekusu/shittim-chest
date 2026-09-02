"""Authenticated Archive mapping and cursor tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from shittim_chest.domain import (
    AFFECTION_RULES_VERSION,
    AffectionAssessment,
    AffectionAssessmentStatus,
    ParticipantAffection,
    ParticipantSlot,
)
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.archive import project_completed_debate
from shittim_records.costs import CostCategory, StoredDailyCost, StoredDailyRate
from shittim_records.read_api import (
    AffectionRankingQuery,
    ArchivePage,
    CursorCodec,
    ListQuery,
    ReadFailure,
    RecordsReadService,
    RequesterProfile,
)

HMAC_KEY = b"records-test-key-that-is-longer-than-32-bytes"
SESSION_KEY = b"session-key-that-is-longer-than-32-bytes"


class FakeReader:
    def __init__(self) -> None:
        projection = project_completed_debate(
            completed_snapshot(),
            identity_hmac_key=HMAC_KEY,
            presentation=presentation(),
            projected_at=NOW,
        )
        self.record_id = projection.record_id
        self.items = projection.items
        self.meta = next(item for item in self.items if item["SK"] == "META")
        self.page = ArchivePage(items=(self.meta,), last_evaluated_key=None, index_name="gsi1")
        self.list_calls: list[dict[str, Any]] = []
        self.ranking_items = ranking_items()
        (
            self.affection_pointer,
            self.affection_generation,
            self.affection_pages,
        ) = affection_ranking_items()
        self.costs = tuple(
            StoredDailyCost(
                cost_date=date(2026, 8, 14),
                category=cast(CostCategory, category),
                amount_usd=Decimal(str(index)),
                estimated=False,
                collected_at=NOW,
            )
            for index, category in enumerate(
                ("FARGATE", "LAMBDA", "OPENAI", "OTHER_AWS"),
                1,
            )
        )
        self.rates = (
            StoredDailyRate(
                rate_date=date(2026, 8, 14),
                usd_jpy=Decimal("150"),
                collected_at=NOW,
            ),
        )

    def list_meta(self, **kwargs: Any) -> ArchivePage:
        self.list_calls.append(kwargs)
        return self.page

    def load_record(self, *, record_id: str) -> tuple[dict[str, Any], ...]:
        return self.items if record_id == self.record_id else ()

    def load_ranking_snapshots(self) -> tuple[dict[str, Any], ...]:
        return self.ranking_items

    def load_affection_ranking_pointer(self) -> dict[str, Any] | None:
        return self.affection_pointer

    def load_affection_ranking_generation(self, *, generation_id: str) -> dict[str, Any] | None:
        return (
            self.affection_generation
            if generation_id == self.affection_generation["generation_id"]
            else None
        )

    def load_affection_ranking_pages(
        self,
        *,
        generation_id: str,
        page_indices: tuple[int, ...],
    ) -> tuple[dict[str, Any], ...]:
        assert generation_id == self.affection_generation["generation_id"]
        return tuple(page for page in self.affection_pages if page["page_index"] in page_indices)

    def load_cost_ledger(
        self,
    ) -> tuple[tuple[StoredDailyCost, ...], tuple[StoredDailyRate, ...]]:
        return self.costs, self.rates

    def load_profiles(
        self,
        *,
        requester_keys: tuple[str, ...],
    ) -> dict[str, RequesterProfile]:
        return {
            key: RequesterProfile(
                display_name="Current Requester",
                avatar_asset_key=f"requesters/{key}/avatar.webp",
            )
            for key in requester_keys
        }

    def avatar_url(self, *, asset_key: str) -> str:
        return f"https://media.example.invalid/{asset_key}"


def service(reader: FakeReader | None = None) -> tuple[RecordsReadService, FakeReader]:
    actual = reader or FakeReader()
    return RecordsReadService(reader=actual, cursor_codec=CursorCodec(SESSION_KEY)), actual


def ranking_items() -> tuple[dict[str, Any], ...]:
    common = {
        "SK": "CURRENT",
        "schema_version": 1,
        "record_type": "ranking_snapshot",
        "generated_at": NOW.isoformat(),
        "archive_count": 4,
    }
    return (
        {
            **common,
            "PK": "RANKING#WINS",
            "ranking_kind": "wins",
            "entries": [
                {"participant": "participant-a", "display_name": "Arona", "count": 2, "rank": 1},
                {
                    "participant": "participant-c",
                    "display_name": "Participant C",
                    "count": 1,
                    "rank": 2,
                },
                {"participant": "participant-b", "display_name": "Plana", "count": 1, "rank": 2},
            ],
        },
        {
            **common,
            "PK": "RANKING#REQUESTS",
            "ranking_kind": "requests",
            "entries": [
                {"requester_key": "requester-a", "display_name": "Stored A", "count": 2, "rank": 1},
                {"requester_key": "requester-b", "display_name": "Stored B", "count": 2, "rank": 1},
            ],
        },
    )


def affection_ranking_items(
    *,
    profile_count: int = 2,
    generation_id: str = "a" * 32,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    tuple[dict[str, Any], ...],
]:
    generated_at = NOW.isoformat()
    checksum = "b" * 64
    if profile_count == 2:
        entries = [
            {
                "requester_key": "requester-a",
                "display_name": "Stored A",
                "score": 700,
                "rank": 1,
                "reset_count": 2,
            },
            {
                "requester_key": "requester-b",
                "display_name": "Stored B",
                "score": 500,
                "rank": 2,
            },
        ]
    else:
        entries = [
            {
                "requester_key": f"requester-{index:03d}",
                "display_name": f"Requester {index:03d}",
                "score": 1000 - index,
                "rank": index + 1,
            }
            for index in range(profile_count)
        ]
    page_count = (profile_count + 49) // 50
    pages = tuple(
        {
            "PK": f"RANKING#AFFECTION#GEN#{generation_id}",
            "SK": f"PAGE#{page_index:06d}",
            "schema_version": 1,
            "record_type": "affection_ranking_page",
            "generation_id": generation_id,
            "page_index": page_index,
            "offset": page_index * 50,
            "entry_count": len(entries[page_index * 50 : (page_index + 1) * 50]),
            "rankings": [
                {
                    "participant": slot,
                    "entries": [
                        dict(entry) for entry in entries[page_index * 50 : (page_index + 1) * 50]
                    ],
                }
                for slot in (
                    "participant-a",
                    "participant-b",
                    "participant-c",
                )
            ],
        }
        for page_index in range(page_count)
    )
    return (
        {
            "PK": "RANKING#AFFECTION",
            "SK": "CURRENT",
            "schema_version": 1,
            "record_type": "affection_ranking_pointer",
            "generation_id": generation_id,
            "generated_at": generated_at,
            "profile_count": profile_count,
            "page_count": page_count,
            "checksum": checksum,
        },
        {
            "PK": f"RANKING#AFFECTION#GEN#{generation_id}",
            "SK": "META",
            "schema_version": 1,
            "record_type": "affection_ranking_generation",
            "generation_id": generation_id,
            "generated_at": generated_at,
            "profile_count": profile_count,
            "page_count": page_count,
            "page_size": 50,
            "checksum": checksum,
            "participants": [
                {"participant": "participant-a", "display_name": "Arona"},
                {"participant": "participant-b", "display_name": "Plana"},
                {"participant": "participant-c", "display_name": "Participant C"},
            ],
        },
        pages,
    )


def test_list_normalizes_preview_and_uses_profile_avatar() -> None:
    records, reader = service()
    reader.meta["question"] = "  one\n two\tthree  "

    result = records.list_records(query=ListQuery(), now=NOW)

    assert len(result.items) == 1
    assert result.items[0].record_id == reader.record_id
    assert result.items[0].question_preview == "one two three"
    assert result.items[0].requester.display_name == "Current Requester"
    assert result.items[0].requester.avatar.kind == "image"
    assert reader.list_calls[0]["limit"] == 12
    assert reader.list_calls[0]["sort"] == "newest"


def test_rankings_use_atomic_snapshots_and_current_requester_profiles() -> None:
    records, _reader = service()

    result = records.get_rankings(now=NOW)
    payload = result.model_dump(by_alias=True, mode="json")

    assert [(entry.display_name, entry.count, entry.rank) for entry in result.wins] == [
        ("Arona", 2, 1),
        ("Participant C", 1, 2),
        ("Plana", 1, 2),
    ]
    assert [(entry.display_name, entry.count, entry.rank) for entry in result.requests] == [
        ("Current Requester", 2, 1),
        ("Current Requester", 2, 1),
    ]
    assert all(entry.avatar.kind == "image" for entry in result.requests)
    assert "requesterKey" not in repr(payload)


def test_affection_rankings_return_all_three_personas_without_internal_keys() -> None:
    records, _reader = service()

    result = records.get_affection_rankings(query=AffectionRankingQuery(), now=NOW)
    payload = result.model_dump(by_alias=True, mode="json")

    assert result.default_score == 500
    assert result.max_score == 1000
    assert [ranking.participant for ranking in result.rankings] == [
        "participant-a",
        "participant-b",
        "participant-c",
    ]
    assert all(len(ranking.entries) == 2 for ranking in result.rankings)
    assert result.rankings[0].entries[0].display_name == "Stored A"
    assert result.rankings[0].entries[0].score == 700
    assert result.rankings[0].entries[0].reset_count == 2
    assert result.rankings[0].entries[1].reset_count == 0
    assert result.next_cursor is None
    assert "requesterKey" not in repr(payload)


def test_affection_rankings_cursor_reads_every_profile_from_one_immutable_generation() -> None:
    records, reader = service()
    (
        reader.affection_pointer,
        reader.affection_generation,
        reader.affection_pages,
    ) = affection_ranking_items(profile_count=55)

    first = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50),
        now=NOW,
    )

    assert all(len(ranking.entries) == 50 for ranking in first.rankings)
    assert first.next_cursor is not None
    reader.affection_pointer = {"corrupt": True}

    second = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50, cursor=first.next_cursor),
        now=NOW,
    )

    assert all(len(ranking.entries) == 5 for ranking in second.rankings)
    assert [ranking.entries[0].rank for ranking in second.rankings] == [51, 51, 51]
    assert second.next_cursor is None
    assert second.generated_at == first.generated_at


def test_affection_cursor_chain_keeps_initial_expiry_after_late_next_page() -> None:
    reader = FakeReader()
    (
        reader.affection_pointer,
        reader.affection_generation,
        reader.affection_pages,
    ) = affection_ranking_items(profile_count=105)
    codec = CursorCodec(SESSION_KEY)
    records = RecordsReadService(reader=reader, cursor_codec=codec)

    first = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50),
        now=NOW,
    )
    assert first.next_cursor is not None
    _generation, _offset, initial_expiry = codec.decode_affection(
        cursor=first.next_cursor,
        limit=50,
        now=NOW,
    )
    assert initial_expiry == int((NOW + timedelta(hours=1)).timestamp())
    reader.affection_pointer = {"replaced": True}
    late = NOW + timedelta(minutes=59)

    second = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50, cursor=first.next_cursor),
        now=late,
    )

    assert second.next_cursor is not None
    _generation, offset, inherited_expiry = codec.decode_affection(
        cursor=second.next_cursor,
        limit=50,
        now=late,
    )
    assert offset == 100
    assert inherited_expiry == initial_expiry


def test_affection_rankings_cursor_is_bound_to_limit_and_signature() -> None:
    records, reader = service()
    (
        reader.affection_pointer,
        reader.affection_generation,
        reader.affection_pages,
    ) = affection_ranking_items(profile_count=55)
    first = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50),
        now=NOW,
    )
    assert first.next_cursor is not None
    replacement = "A" if first.next_cursor[-1] != "A" else "B"

    with pytest.raises(ReadFailure) as wrong_limit:
        records.get_affection_rankings(
            query=AffectionRankingQuery(limit=49, cursor=first.next_cursor),
            now=NOW,
        )
    with pytest.raises(ReadFailure) as tampered:
        records.get_affection_rankings(
            query=AffectionRankingQuery(
                limit=50,
                cursor=f"{first.next_cursor[:-1]}{replacement}",
            ),
            now=NOW,
        )

    assert wrong_limit.value.code == "CURSOR_INVALID"
    assert wrong_limit.value.status == 400
    assert tampered.value.code == "CURSOR_INVALID"
    assert tampered.value.status == 400


def test_affection_rankings_reject_corrupt_competition_rank_across_pages() -> None:
    records, reader = service()
    (
        reader.affection_pointer,
        reader.affection_generation,
        reader.affection_pages,
    ) = affection_ranking_items(profile_count=55)
    for ranking in reader.affection_pages[1]["rankings"]:
        ranking["entries"][0]["score"] = 951
    first = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=49),
        now=NOW,
    )
    assert first.next_cursor is not None

    with pytest.raises(ReadFailure) as failure:
        records.get_affection_rankings(
            query=AffectionRankingQuery(limit=49, cursor=first.next_cursor),
            now=NOW,
        )

    assert failure.value.code == "INSIGHTS_UNAVAILABLE"
    assert failure.value.status == 503


def test_affection_rankings_reject_corrupt_rank_at_an_aligned_page_boundary() -> None:
    records, reader = service()
    (
        reader.affection_pointer,
        reader.affection_generation,
        reader.affection_pages,
    ) = affection_ranking_items(profile_count=55)
    first = records.get_affection_rankings(
        query=AffectionRankingQuery(limit=50),
        now=NOW,
    )
    assert first.next_cursor is not None
    for ranking in reader.affection_pages[1]["rankings"]:
        ranking["entries"][0]["rank"] = 1

    with pytest.raises(ReadFailure) as failure:
        records.get_affection_rankings(
            query=AffectionRankingQuery(limit=50, cursor=first.next_cursor),
            now=NOW,
        )

    assert failure.value.code == "INSIGHTS_UNAVAILABLE"
    assert failure.value.status == 503


def test_detail_maps_archive_v2_affection_and_historical_v1_remains_null() -> None:
    records, reader = service()
    historical = records.get_record(record_id=reader.record_id, now=NOW)
    assert historical.schema_version == 2
    assert historical.affection is None

    assessment = AffectionAssessment(
        status=AffectionAssessmentStatus.APPLIED,
        rules_version=AFFECTION_RULES_VERSION,
        participants=(
            ParticipantAffection(ParticipantSlot.PARTICIPANT_A, 500, 25, 25, 525),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_B, 500, -10, -10, 490),
            ParticipantAffection(ParticipantSlot.PARTICIPANT_C, 990, 50, 10, 1000),
        ),
        assessed_at=NOW,
    )
    projection = project_completed_debate(
        replace(completed_snapshot(), affection_assessment=assessment),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )
    reader.record_id = projection.record_id
    reader.items = projection.items

    current = records.get_record(record_id=projection.record_id, now=NOW)

    assert current.affection is not None
    assert current.affection.participants[2].applied_delta == 10


@pytest.mark.parametrize(
    "mutate",
    (
        lambda reader: setattr(reader, "ranking_items", reader.ranking_items[:1]),
        lambda reader: reader.ranking_items[1]["entries"][0].update({"count": 0}),
        lambda reader: reader.ranking_items[1].update(
            {"generated_at": (NOW + timedelta(seconds=1)).isoformat()}
        ),
    ),
)
def test_rankings_fail_closed_on_incomplete_or_malformed_snapshot(mutate: Any) -> None:
    records, reader = service()
    mutate(reader)

    with pytest.raises(ReadFailure) as caught:
        records.get_rankings(now=NOW)

    assert (caught.value.code, caught.value.status) == ("INSIGHTS_UNAVAILABLE", 503)


def test_costs_return_jpy_breakdown_with_japanese_calendar_dates() -> None:
    records, _reader = service()

    result = records.get_costs(period="all", now=NOW)
    payload = result.model_dump(by_alias=True, mode="json")

    assert payload["timeZone"] == "Asia/Tokyo"
    assert payload["currency"] == "JPY"
    assert payload["startDate"] == "2026-08-14"
    assert payload["breakdown"] == {
        "fargate": "150.000000",
        "lambda": "300.000000",
        "openai": "450.000000",
        "otherAws": "600.000000",
    }
    assert payload["total"] == "1500.000000"
    assert payload["updatedAt"].endswith("+09:00")
    assert "amountUsd" not in repr(payload)


def test_costs_fail_closed_on_duplicate_stored_daily_record() -> None:
    records, reader = service()
    reader.costs = (*reader.costs, reader.costs[0])

    with pytest.raises(ReadFailure) as caught:
        records.get_costs(period="week", now=NOW)

    assert (caught.value.code, caught.value.status) == ("INSIGHTS_UNAVAILABLE", 503)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("gsi1pk", "ARCHIVE#UNKNOWN"),
        ("gsi1sk", "2026-08-17T00:00:00+00:00#wrong-record"),
    ),
)
def test_list_rejects_malformed_selected_gsi1_projection(field: str, value: str) -> None:
    records, reader = service()
    reader.meta[field] = value

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_winner_filtered_list_rejects_a_row_for_another_winner() -> None:
    records, reader = service()
    requested_winner = cast(Any, reader.meta["winner"])
    conflicting_winner = next(
        slot
        for slot in ("participant-a", "participant-b", "participant-c")
        if slot != requested_winner
    )
    reader.meta["winner"] = conflicting_winner
    reader.page = ArchivePage(
        items=(reader.meta,),
        last_evaluated_key=None,
        index_name="gsi2",
    )

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(winner=requested_winner), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("gsi2pk", "WINNER#participant-unknown"),
        ("gsi2sk", "2026-08-17T00:00:00+00:00#wrong-record"),
    ),
)
def test_winner_filtered_list_rejects_malformed_selected_gsi2_projection(
    field: str,
    value: str,
) -> None:
    records, reader = service()
    requested_winner = cast(Any, reader.meta["winner"])
    reader.meta[field] = value
    reader.page = ArchivePage(
        items=(reader.meta,),
        last_evaluated_key=None,
        index_name="gsi2",
    )

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(winner=requested_winner), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_list_converts_stored_timestamp_overflow_to_archive_unavailable() -> None:
    records, reader = service()
    reader.meta["completed_at"] = "9999-12-31T23:59:59-01:00"

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_cursor_is_bound_to_filters_limit_sort_index_and_expiry() -> None:
    codec = CursorCodec(SESSION_KEY)
    query = ListQuery(limit=12, sort="oldest", winner="participant-b")
    key = {
        "PK": f"RECORD#{'r' * 43}",
        "SK": "META",
        "gsi2pk": "WINNER#participant-b",
        "gsi2sk": f"2026-08-17T00:00:00+00:00#{'r' * 43}",
    }
    cursor = codec.encode(
        query=query,
        index_name="gsi2",
        last_evaluated_key=cast(Any, key),
        now=NOW,
    )

    index, restored = codec.decode(
        query=ListQuery(limit=12, sort="oldest", winner="participant-b", cursor=cursor),
        now=NOW,
    )
    assert index == "gsi2"
    assert restored == key

    for changed in (
        ListQuery(limit=13, winner="participant-b", cursor=cursor),
        ListQuery(limit=12, winner="participant-a", cursor=cursor),
        ListQuery(limit=12, sort="newest", winner="participant-b", cursor=cursor),
    ):
        with pytest.raises(ReadFailure) as caught:
            codec.decode(query=changed, now=NOW)
        assert caught.value.code == "CURSOR_INVALID"
    with pytest.raises(ReadFailure):
        codec.decode(
            query=ListQuery(limit=12, sort="oldest", winner="participant-b", cursor=cursor),
            now=NOW + timedelta(hours=1),
        )


def test_tampered_cursor_is_rejected() -> None:
    codec = CursorCodec(SESSION_KEY)
    query = ListQuery()
    cursor = codec.encode(
        query=query,
        index_name="gsi1",
        last_evaluated_key={
            "PK": "p",
            "SK": "s",
            "gsi1pk": "ARCHIVE#COMPLETED",
            "gsi1sk": "completed#record",
        },
        now=NOW,
    )

    with pytest.raises(ReadFailure) as caught:
        codec.decode(query=ListQuery(cursor=f"x{cursor}"), now=NOW)
    assert caught.value.status == 400


def test_cursor_rejects_a_key_for_the_wrong_index() -> None:
    codec = CursorCodec(SESSION_KEY)

    with pytest.raises(ReadFailure) as caught:
        codec.encode(
            query=ListQuery(),
            index_name="gsi1",
            last_evaluated_key={
                "PK": "p",
                "SK": "s",
                "gsi2pk": "WINNER#participant-b",
                "gsi2sk": "completed#record",
            },
            now=NOW,
        )
    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_detail_reconstructs_exact_twelve_items_without_internal_fields() -> None:
    records, reader = service()

    result = records.get_record(record_id=reader.record_id, now=NOW)
    payload = result.model_dump(by_alias=True, mode="json")

    assert result.result.winner == "participant-b"
    assert len(result.initial_opinions) == 3
    assert len(result.final_proposals) == 3
    assert len(result.votes) == 3
    assert [participant.avatar.url for participant in result.participants] == [
        "https://media.example.invalid/participants/participant-a/avatar.webp",
        "https://media.example.invalid/participants/participant-b/avatar.webp",
        "https://media.example.invalid/participants/participant-c/avatar.webp",
    ]
    serialized = repr(payload)
    for forbidden in (
        "accuracyScore",
        "usefulnessScore",
        "safetyScore",
        "requesterKey",
        "sourceFingerprint",
        "attemptId",
        "evidence",
    ):
        assert forbidden not in serialized


def test_detail_ignores_legacy_archived_participant_avatar_key() -> None:
    records, reader = service()
    participants = cast(dict[str, dict[str, Any]], reader.meta["participants"])
    participants["participant-a"]["avatar_asset_key"] = "participants/participant-a/historical.webp"

    result = records.get_record(record_id=reader.record_id, now=NOW)

    assert result.participants[0].avatar.url == (
        "https://media.example.invalid/participants/participant-a/avatar.webp"
    )


def test_detail_rejects_conflicting_saved_winners() -> None:
    reader = FakeReader()
    decision = next(item for item in reader.items if item["SK"] == "DECISION")
    decision["winner"] = "participant-a"
    records, _reader = service(reader)

    with pytest.raises(ReadFailure) as caught:
        records.get_record(record_id=reader.record_id, now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda items: items[:-1],
        lambda items: (*items, items[0]),
        lambda items: ({**items[0], "SK": "UNKNOWN"}, *items[1:]),
    ),
)
def test_detail_fails_closed_on_missing_duplicate_or_unknown_items(mutation: Any) -> None:
    reader = FakeReader()
    reader.items = tuple(mutation(reader.items))
    records, _reader = service(reader)

    with pytest.raises(ReadFailure) as caught:
        records.get_record(record_id=reader.record_id, now=NOW)
    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_invalid_record_id_and_missing_record_are_distinct() -> None:
    records, _reader = service()
    with pytest.raises(ReadFailure) as invalid:
        records.get_record(record_id="short", now=NOW)
    assert invalid.value.status == 400

    with pytest.raises(ReadFailure) as missing:
        records.get_record(record_id="x" * 43, now=NOW)
    assert missing.value.status == 404


def test_list_query_rejects_unknown_sort() -> None:
    records, _reader = service()
    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(sort=cast(Any, "unknown")), now=NOW)

    assert (caught.value.code, caught.value.status) == ("REQUEST_INVALID", 400)
