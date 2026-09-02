"""Deterministic ranking snapshots derived from completed Archive metadata."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

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
class AffectionRankingEntry:
    requester_key: str
    display_name: str
    score: int
    rank: int
    reset_count: int = 0


@dataclass(frozen=True, slots=True)
class ParticipantAffectionRanking:
    participant: ParticipantSlot
    display_name: str
    entries: tuple[AffectionRankingEntry, ...]


@dataclass(frozen=True, slots=True)
class AffectionProfileSeed:
    requester_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RankingSnapshot:
    generated_at: datetime
    wins: tuple[ParticipantRanking, ...]
    requests: tuple[RequesterRanking, ...]
    archive_count: int
    affection: tuple[ParticipantAffectionRanking, ...] = ()
    affection_profile_count: int = 0


class RankingSource(Protocol):
    def list_completed_meta(self) -> tuple[DynamoItem, ...]: ...

    def list_affection_profiles(self) -> tuple[DynamoItem, ...]: ...

    def seed_default_affection_profiles(
        self,
        seeds: tuple[AffectionProfileSeed, ...],
        *,
        updated_at: datetime,
    ) -> None: ...


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
        generated_at = now.astimezone(UTC)
        seeds = _missing_profile_seeds(items, self._source.list_affection_profiles())
        if seeds:
            self._source.seed_default_affection_profiles(seeds, updated_at=generated_at)
        profiles = self._source.list_affection_profiles()
        snapshot = build_rankings(
            items,
            affection_profiles=profiles,
            generated_at=generated_at,
        )
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
    affection_profiles: tuple[DynamoItem, ...] = (),
    generated_at: datetime,
) -> RankingSnapshot:
    """Validate all source rows before constructing an all-or-nothing snapshot."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("ranking timestamp must be timezone-aware")
    rows = tuple(_parse_archive_row(item) for item in items)
    record_ids = tuple(row.record_id for row in rows)
    if len(set(record_ids)) != len(record_ids):
        raise RankingDataInvalid("Archive contains duplicate metadata records")
    wins: tuple[ParticipantRanking, ...] = ()
    if rows:
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
    affection = _build_affection_rankings(
        affection_profiles,
        rows=rows,
    )
    return RankingSnapshot(
        generated_at=generated_at.astimezone(UTC),
        wins=wins,
        requests=requests,
        affection=affection,
        archive_count=len(rows),
        affection_profile_count=len(affection_profiles),
    )


def _missing_profile_seeds(
    archive_items: tuple[DynamoItem, ...],
    profile_items: tuple[DynamoItem, ...],
) -> tuple[AffectionProfileSeed, ...]:
    rows = tuple(_parse_archive_row(item) for item in archive_items)
    profiles = tuple(_parse_affection_profile(item) for item in profile_items)
    existing = {profile.requester_key for profile in profiles}
    latest_names: dict[str, tuple[datetime, str, str]] = {}
    for row in rows:
        candidate = (row.completed_at, row.record_id, row.requester_display_name)
        current = latest_names.get(row.requester_key)
        if current is None or candidate[:2] > current[:2]:
            latest_names[row.requester_key] = candidate
    return tuple(
        AffectionProfileSeed(requester_key=key, display_name=value[2])
        for key, value in sorted(latest_names.items())
        if key not in existing
    )


@dataclass(frozen=True, slots=True)
class _AffectionProfileRow:
    requester_key: str
    display_name: str
    scores: dict[ParticipantSlot, int]
    reset_count: int


def _build_affection_rankings(
    profile_items: tuple[DynamoItem, ...],
    *,
    rows: tuple[_ArchiveRankingRow, ...],
) -> tuple[
    ParticipantAffectionRanking,
    ParticipantAffectionRanking,
    ParticipantAffectionRanking,
]:
    profiles = tuple(_parse_affection_profile(item) for item in profile_items)
    if len({profile.requester_key for profile in profiles}) != len(profiles):
        raise RankingDataInvalid("affection profiles contain duplicate requesters")
    participant_names: dict[ParticipantSlot, str] = {
        "participant-a": "アロナ",
        "participant-b": "プラナ",
        "participant-c": "安倍晋三AI",
    }
    if rows:
        latest = max(rows, key=lambda row: (row.completed_at, row.record_id))
        participant_names = latest.participant_names
    rankings: list[ParticipantAffectionRanking] = []
    for slot in PARTICIPANT_SLOTS:
        ordered = sorted(
            profiles,
            key=lambda profile: (
                -profile.scores[slot],
                profile.display_name,
                profile.requester_key,
            ),
        )
        ranks = _competition_ranks(tuple(profile.scores[slot] for profile in ordered))
        rankings.append(
            ParticipantAffectionRanking(
                participant=slot,
                display_name=participant_names[slot],
                entries=tuple(
                    AffectionRankingEntry(
                        requester_key=profile.requester_key,
                        display_name=profile.display_name,
                        score=profile.scores[slot],
                        rank=rank,
                        reset_count=profile.reset_count,
                    )
                    for profile, rank in zip(ordered, ranks, strict=True)
                ),
            )
        )
    return (rankings[0], rankings[1], rankings[2])


def _parse_affection_profile(item: DynamoItem) -> _AffectionProfileRow:
    legacy_fields = {
        "PK",
        "SK",
        "schema_version",
        "record_type",
        "source_version",
        "display_name",
        "scores",
        "updated_at",
    }
    current_fields = {
        *legacy_fields,
        "reset_count",
        "memorial_cycle",
    }
    unlock_fields = {
        "unlocked_participant",
        "unlocked_at",
        "unlock_record_id",
        "unlock_display_name",
        "unlock_memorial_cycle",
        "unlock_retroactive",
    }
    key = item.get("SK")
    schema_version = item.get("schema_version")
    source_version = item.get("source_version")
    scores = item.get("scores")
    reset_count = item.get("reset_count", 0)
    memorial_cycle = item.get("memorial_cycle", 1)
    expected_fields = legacy_fields if schema_version == 1 else current_fields
    if schema_version == 2 and "unlocked_participant" in item:
        expected_fields = {*current_fields, *unlock_fields}
    if (
        set(item) != expected_fields
        or item.get("PK") != "AFFECTION#PROFILE"
        or schema_version not in {1, 2}
        or item.get("record_type") != "affection_profile"
        or not _is_opaque_key(key)
        or isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or source_version < 0
        or isinstance(reset_count, bool)
        or not isinstance(reset_count, int)
        or reset_count < 0
        or isinstance(memorial_cycle, bool)
        or not isinstance(memorial_cycle, int)
        or memorial_cycle < 1
        or memorial_cycle != reset_count + 1
        or not isinstance(scores, dict)
        or set(scores) != set(PARTICIPANT_SLOTS)
    ):
        raise RankingDataInvalid("affection profile is invalid")
    if schema_version == 2 and unlock_fields.intersection(item):
        _validate_memorial_unlock(item, memorial_cycle=memorial_cycle)
    parsed_scores: dict[ParticipantSlot, int] = {}
    for slot in PARTICIPANT_SLOTS:
        value = scores[slot]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
            raise RankingDataInvalid("affection profile score is invalid")
        parsed_scores[slot] = value
    updated_text = _required_text(item, "updated_at")
    try:
        updated_at = TypeAdapter(AwareDatetime).validate_python(updated_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise RankingDataInvalid("affection profile timestamp is invalid") from None
    if updated_at.isoformat() != updated_text:
        raise RankingDataInvalid("affection profile timestamp is not canonical UTC")
    return _AffectionProfileRow(
        requester_key=cast(str, key),
        display_name=_required_text(item, "display_name"),
        scores=parsed_scores,
        reset_count=reset_count,
    )


def _validate_memorial_unlock(item: DynamoItem, *, memorial_cycle: int) -> None:
    participant = item.get("unlocked_participant")
    record_id = item.get("unlock_record_id")
    unlock_cycle = item.get("unlock_memorial_cycle")
    retroactive = item.get("unlock_retroactive")
    if (
        participant not in PARTICIPANT_SLOTS
        or not _is_opaque_key(record_id)
        or unlock_cycle != memorial_cycle
        or not isinstance(retroactive, bool)
    ):
        raise RankingDataInvalid("affection memorial unlock is invalid")
    _required_text(item, "unlock_display_name")
    unlocked_text = _required_text(item, "unlocked_at")
    try:
        unlocked_at = TypeAdapter(AwareDatetime).validate_python(unlocked_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise RankingDataInvalid("affection memorial unlock timestamp is invalid") from None
    if unlocked_at.isoformat() != unlocked_text:
        raise RankingDataInvalid("affection memorial unlock timestamp is not canonical UTC")


def _is_opaque_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 43
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _parse_archive_row(item: DynamoItem) -> _ArchiveRankingRow:
    record_id = _required_text(item, "record_id")
    completed_text = _required_text(item, "completed_at")
    if (
        item.get("schema_version") not in {1, 2}
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
        if not isinstance(profile, dict):
            raise RankingDataInvalid("Archive participant presentation is invalid")
        profile_fields = set(profile)
        current_fields = {"display_name", "accent"}
        historical_fields = {*current_fields, "avatar_asset_key"}
        if profile_fields not in (current_fields, historical_fields) or (
            profile_fields == historical_fields and profile["avatar_asset_key"] is not None
        ):
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
