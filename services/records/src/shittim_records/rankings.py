"""Deterministic ranking snapshots derived from completed Archive metadata."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import AwareDatetime, TypeAdapter, ValidationError
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.contracts import ParticipantSlot

PARTICIPANT_SLOTS: tuple[ParticipantSlot, ...] = (
    "participant-a",
    "participant-b",
    "participant-c",
)


class RankingDataInvalid(ValueError):
    """Raised when an Archive row cannot safely contribute to rankings."""


@dataclass(frozen=True, slots=True)
class ParticipantRanking:
    participant: ParticipantSlot
    display_name: str
    count: int
    rank: int


@dataclass(frozen=True, slots=True)
class RequesterRanking:
    requester_key: str
    display_name: str
    count: int
    rank: int


@dataclass(frozen=True, slots=True)
class RankingSnapshot:
    generated_at: datetime
    wins: tuple[ParticipantRanking, ...]
    requests: tuple[RequesterRanking, ...]
    archive_count: int


class RankingSource(Protocol):
    def list_completed_meta(self) -> tuple[DynamoItem, ...]: ...


class RankingSnapshotStore(Protocol):
    def save_rankings(self, snapshot: RankingSnapshot) -> None: ...


class RankingService:
    """Recompute rankings from the complete immutable Archive."""

    def __init__(self, *, source: RankingSource, store: RankingSnapshotStore) -> None:
        self._source = source
        self._store = store

    def refresh(self, *, now: datetime) -> RankingSnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("ranking timestamp must be timezone-aware")
        items = self._source.list_completed_meta()
        snapshot = build_rankings(items, generated_at=now.astimezone(UTC))
        self._store.save_rankings(snapshot)
        return snapshot


@dataclass(frozen=True, slots=True)
class _ArchiveRankingRow:
    record_id: str
    completed_at: datetime
    winner: ParticipantSlot
    participant_names: dict[ParticipantSlot, str]
    requester_key: str
    requester_display_name: str


def build_rankings(
    items: tuple[DynamoItem, ...],
    *,
    generated_at: datetime,
) -> RankingSnapshot:
    """Validate all source rows before constructing an all-or-nothing snapshot."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("ranking timestamp must be timezone-aware")
    rows = tuple(_parse_archive_row(item) for item in items)
    record_ids = tuple(row.record_id for row in rows)
    if len(set(record_ids)) != len(record_ids):
        raise RankingDataInvalid("Archive contains duplicate metadata records")
    if not rows:
        return RankingSnapshot(
            generated_at=generated_at.astimezone(UTC),
            wins=(),
            requests=(),
            archive_count=0,
        )

    latest = max(rows, key=lambda row: (row.completed_at, row.record_id))
    win_counts = Counter(row.winner for row in rows)
    win_candidates = [
        (slot, latest.participant_names[slot], win_counts.get(slot, 0))
        for slot in PARTICIPANT_SLOTS
    ]
    ordered_wins = sorted(win_candidates, key=lambda entry: (-entry[2], entry[1], entry[0]))
    win_ranks = _competition_ranks(tuple(entry[2] for entry in ordered_wins))
    wins = tuple(
        ParticipantRanking(
            participant=slot,
            display_name=display_name,
            count=count,
            rank=rank,
        )
        for (slot, display_name, count), rank in zip(ordered_wins, win_ranks, strict=True)
    )

    requester_counts = Counter(row.requester_key for row in rows)
    latest_requester_names: dict[str, tuple[datetime, str, str]] = {}
    for row in rows:
        candidate = (row.completed_at, row.record_id, row.requester_display_name)
        current = latest_requester_names.get(row.requester_key)
        if current is None or candidate[:2] > current[:2]:
            latest_requester_names[row.requester_key] = candidate
    requester_candidates = [
        (key, latest_requester_names[key][2], count) for key, count in requester_counts.items()
    ]
    ordered_requesters = sorted(
        requester_candidates,
        key=lambda entry: (-entry[2], entry[1], entry[0]),
    )[:10]
    requester_ranks = _competition_ranks(tuple(entry[2] for entry in ordered_requesters))
    requests = tuple(
        RequesterRanking(
            requester_key=key,
            display_name=display_name,
            count=count,
            rank=rank,
        )
        for (key, display_name, count), rank in zip(
            ordered_requesters,
            requester_ranks,
            strict=True,
        )
    )
    return RankingSnapshot(
        generated_at=generated_at.astimezone(UTC),
        wins=wins,
        requests=requests,
        archive_count=len(rows),
    )


def _parse_archive_row(item: DynamoItem) -> _ArchiveRankingRow:
    record_id = _required_text(item, "record_id")
    completed_text = _required_text(item, "completed_at")
    if (
        item.get("schema_version") != 1
        or item.get("record_type") != "archive_meta"
        or item.get("PK") != f"RECORD#{record_id}"
        or item.get("SK") != "META"
        or item.get("gsi1pk") != "ARCHIVE#COMPLETED"
        or item.get("gsi1sk") != f"{completed_text}#{record_id}"
        or len(record_id) != 43
        or not all(character.isalnum() or character in "_-" for character in record_id)
    ):
        raise RankingDataInvalid("Archive metadata identity is invalid")
    try:
        completed_at = TypeAdapter(AwareDatetime).validate_python(completed_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise RankingDataInvalid("Archive completion timestamp is invalid") from None
    if completed_at.isoformat() != completed_text:
        raise RankingDataInvalid("Archive completion timestamp is not canonical UTC")

    winner = _required_text(item, "winner")
    if winner not in PARTICIPANT_SLOTS:
        raise RankingDataInvalid("Archive winner is invalid")
    raw_participants = item.get("participants")
    if not isinstance(raw_participants, dict) or set(raw_participants) != set(PARTICIPANT_SLOTS):
        raise RankingDataInvalid("Archive participant presentation is invalid")
    participant_names: dict[ParticipantSlot, str] = {}
    for slot in PARTICIPANT_SLOTS:
        profile = raw_participants[slot]
        if not isinstance(profile, dict) or set(profile) != {"display_name", "accent"}:
            raise RankingDataInvalid("Archive participant presentation is invalid")
        participant_names[slot] = _required_text(profile, "display_name")
        _required_text(profile, "accent")
    if len(set(participant_names.values())) != len(PARTICIPANT_SLOTS):
        raise RankingDataInvalid("Archive participant names are not unique")

    raw_counts = item.get("vote_counts")
    if not isinstance(raw_counts, dict) or set(raw_counts) != set(PARTICIPANT_SLOTS):
        raise RankingDataInvalid("Archive vote counts are invalid")
    counts: dict[ParticipantSlot, int] = {}
    for slot in PARTICIPANT_SLOTS:
        count = raw_counts[slot]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 3:
            raise RankingDataInvalid("Archive vote counts are invalid")
        counts[slot] = count
    if sum(counts.values()) != len(PARTICIPANT_SLOTS):
        raise RankingDataInvalid("Archive vote counts are incomplete")
    leaders = {slot for slot, count in counts.items() if count == max(counts.values())}
    tie_break = item.get("tie_break_applied")
    if winner not in leaders or not isinstance(tie_break, bool) or tie_break != (len(leaders) > 1):
        raise RankingDataInvalid("Archive winner summary is inconsistent")

    return _ArchiveRankingRow(
        record_id=record_id,
        completed_at=completed_at,
        winner=winner,
        participant_names=participant_names,
        requester_key=_required_text(item, "requester_key"),
        requester_display_name=_required_text(item, "requester_display_name"),
    )


def _required_text(item: DynamoItem, field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RankingDataInvalid(f"Archive {field} is invalid")
    return value


def _competition_ranks(counts: tuple[int, ...]) -> tuple[int, ...]:
    ranks: list[int] = []
    previous_count: int | None = None
    previous_rank = 0
    for index, count in enumerate(counts, start=1):
        rank = previous_rank if count == previous_count else index
        ranks.append(rank)
        previous_count = count
        previous_rank = rank
    return tuple(ranks)
