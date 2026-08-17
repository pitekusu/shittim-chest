"""Authenticated Archive mapping and cursor tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.archive import project_completed_debate
from shittim_records.read_api import (
    ArchivePage,
    CursorCodec,
    ListQuery,
    ReadFailure,
    RecordsReadService,
    RequesterProfile,
    parse_aware_datetime,
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

    def list_meta(self, **kwargs: Any) -> ArchivePage:
        self.list_calls.append(kwargs)
        return self.page

    def load_record(self, *, record_id: str) -> tuple[dict[str, Any], ...]:
        return self.items if record_id == self.record_id else ()

    def load_profiles(
        self,
        *,
        requester_keys: tuple[str, ...],
    ) -> dict[str, RequesterProfile]:
        return {
            key: RequesterProfile(
                display_name="Current Requester",
                avatar_asset_key=f"requesters/{key}/avatar.webp",
                expires_at=int((NOW + timedelta(days=1)).timestamp()),
            )
            for key in requester_keys
        }

    def avatar_url(self, *, asset_key: str) -> str:
        return f"https://media.example.invalid/{asset_key}"


def service(reader: FakeReader | None = None) -> tuple[RecordsReadService, FakeReader]:
    actual = reader or FakeReader()
    return RecordsReadService(reader=actual, cursor_codec=CursorCodec(SESSION_KEY)), actual


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


def test_list_rejects_a_row_outside_the_requested_time_range() -> None:
    records, _reader = service()

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(from_at=NOW + timedelta(seconds=1)), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_list_converts_stored_timestamp_overflow_to_archive_unavailable() -> None:
    records, reader = service()
    reader.meta["completed_at"] = "9999-12-31T23:59:59-01:00"

    with pytest.raises(ReadFailure) as caught:
        records.list_records(query=ListQuery(), now=NOW)

    assert (caught.value.code, caught.value.status) == ("ARCHIVE_UNAVAILABLE", 503)


def test_cursor_is_bound_to_filters_limit_index_and_expiry() -> None:
    codec = CursorCodec(SESSION_KEY)
    query = ListQuery(limit=12, winner="participant-b")
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
        query=ListQuery(limit=12, winner="participant-b", cursor=cursor),
        now=NOW,
    )
    assert index == "gsi2"
    assert restored == key

    for changed in (
        ListQuery(limit=13, winner="participant-b", cursor=cursor),
        ListQuery(limit=12, winner="participant-a", cursor=cursor),
    ):
        with pytest.raises(ReadFailure) as caught:
            codec.decode(query=changed, now=NOW)
        assert caught.value.code == "CURSOR_INVALID"
    with pytest.raises(ReadFailure):
        codec.decode(
            query=ListQuery(limit=12, winner="participant-b", cursor=cursor),
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


def test_list_query_rejects_naive_or_reversed_dates() -> None:
    records, _reader = service()
    with pytest.raises(ReadFailure):
        records.list_records(
            query=ListQuery(
                from_at=datetime(2026, 8, 18, tzinfo=UTC),
                to_at=datetime(2026, 8, 17, tzinfo=UTC),
            ),
            now=NOW,
        )
    with pytest.raises(ReadFailure):
        records.list_records(query=ListQuery(from_at=datetime(2026, 8, 17)), now=NOW)
    assert parse_aware_datetime("2026-08-17T12:00:00+09:00") == datetime(
        2026, 8, 17, 3, 0, tzinfo=UTC
    )
