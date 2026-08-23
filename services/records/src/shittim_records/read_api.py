"""Authenticated Records read model and signed pagination cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import AwareDatetime, TypeAdapter, ValidationError
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.contracts import (
    CostBreakdown,
    CostConversion,
    CostPeriod,
    CostsResponse,
    ImageAvatarRef,
    ParticipantSlot,
    PlaceholderAvatarRef,
    RankingEntry,
    RankingsResponse,
    RecordDetailResponse,
    RecordListItem,
    RecordListResponse,
)
from shittim_records.costs import (
    CostDataInvalid,
    StoredDailyCost,
    StoredDailyRate,
    build_cost_view,
)

CURSOR_TTL = timedelta(hours=1)
RECORD_ID_LENGTH = 43
PARTICIPANT_SLOTS: tuple[ParticipantSlot, ...] = (
    "participant-a",
    "participant-b",
    "participant-c",
)
PARTICIPANT_AVATAR_ASSET_KEYS: dict[ParticipantSlot, str] = {
    slot: f"participants/{slot}/avatar.webp" for slot in PARTICIPANT_SLOTS
}
FallbackVariant = Literal["cyan", "pink", "lavender"]
SortOrder = Literal["newest", "oldest"]


class ReadFailure(RuntimeError):
    """Stable public read failure with an HTTP status mapping."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ListQuery:
    limit: int = 12
    sort: SortOrder = "newest"
    winner: ParticipantSlot | None = None
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ArchivePage:
    items: tuple[DynamoItem, ...]
    last_evaluated_key: DynamoItem | None
    index_name: Literal["gsi1", "gsi2"]


@dataclass(frozen=True, slots=True)
class RequesterProfile:
    display_name: str
    avatar_asset_key: str | None


class RecordsReader(Protocol):
    def list_meta(
        self,
        *,
        limit: int,
        sort: SortOrder,
        winner: ParticipantSlot | None,
        exclusive_start_key: DynamoItem | None,
    ) -> ArchivePage: ...

    def load_record(self, *, record_id: str) -> tuple[DynamoItem, ...]: ...

    def load_ranking_snapshots(self) -> tuple[DynamoItem, ...]: ...

    def load_cost_ledger(
        self,
    ) -> tuple[tuple[StoredDailyCost, ...], tuple[StoredDailyRate, ...]]: ...

    def load_profiles(self, *, requester_keys: tuple[str, ...]) -> dict[str, RequesterProfile]: ...

    def avatar_url(self, *, asset_key: str) -> str: ...


class CursorCodec:
    """HMAC-bind a cursor to its complete list query."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("cursor key must contain at least 32 bytes")
        self._key = key

    def encode(
        self,
        *,
        query: ListQuery,
        index_name: str,
        last_evaluated_key: DynamoItem,
        now: datetime,
    ) -> str:
        expected_index = "gsi2" if query.winner else "gsi1"
        if index_name != expected_index:
            raise ReadFailure("CURSOR_INVALID", 400)
        try:
            cursor_key = _validate_cursor_key(last_evaluated_key, index_name=index_name)
        except ValueError:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503) from None
        payload = {
            "version": 2,
            "index": index_name,
            "limit": query.limit,
            "sort": query.sort,
            "winner": query.winner,
            "expires_at": int((_utc(now) + CURSOR_TTL).timestamp()),
            "last_evaluated_key": cursor_key,
        }
        encoded = _base64url(_canonical(payload))
        signature = _base64url(
            hmac.new(self._key, f"records:cursor:{encoded}".encode(), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def decode(self, *, query: ListQuery, now: datetime) -> tuple[str, DynamoItem]:
        if not query.cursor or len(query.cursor) > 4096:
            raise ReadFailure("CURSOR_INVALID", 400)
        try:
            encoded, signature = query.cursor.split(".", 1)
            expected = _base64url(
                hmac.new(
                    self._key,
                    f"records:cursor:{encoded}".encode(),
                    hashlib.sha256,
                ).digest()
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(_decode_base64url(encoded))
        except UnicodeDecodeError, ValueError, json.JSONDecodeError:
            raise ReadFailure("CURSOR_INVALID", 400) from None
        expected_fields = {
            "version",
            "index",
            "limit",
            "sort",
            "winner",
            "expires_at",
            "last_evaluated_key",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ReadFailure("CURSOR_INVALID", 400)
        expected_index = "gsi2" if query.winner else "gsi1"
        if (
            payload["version"] != 2
            or payload["index"] != expected_index
            or payload["limit"] != query.limit
            or payload["sort"] != query.sort
            or payload["winner"] != query.winner
            or isinstance(payload["expires_at"], bool)
            or not isinstance(payload["expires_at"], int)
            or payload["expires_at"] <= int(_utc(now).timestamp())
        ):
            raise ReadFailure("CURSOR_INVALID", 400)
        try:
            key = _validate_cursor_key(
                payload["last_evaluated_key"],
                index_name=expected_index,
            )
        except ValueError:
            raise ReadFailure("CURSOR_INVALID", 400) from None
        return expected_index, key


class RecordsReadService:
    """Map immutable Archive v1 items to the public Records API."""

    def __init__(self, *, reader: RecordsReader, cursor_codec: CursorCodec) -> None:
        self._reader = reader
        self._cursor_codec = cursor_codec

    def list_records(self, *, query: ListQuery, now: datetime) -> RecordListResponse:
        query = validate_list_query(query)
        expected_index = "gsi2" if query.winner else "gsi1"
        start_key: DynamoItem | None = None
        if query.cursor:
            cursor_index, start_key = self._cursor_codec.decode(query=query, now=now)
            if cursor_index != expected_index:
                raise ReadFailure("CURSOR_INVALID", 400)
        page = self._reader.list_meta(
            limit=query.limit,
            sort=query.sort,
            winner=query.winner,
            exclusive_start_key=start_key,
        )
        if page.index_name != expected_index:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        for item in page.items:
            _validate_list_projection(
                item,
                index_name=page.index_name,
                winner=query.winner,
            )
        requester_keys = tuple(
            cast(str, item.get("requester_key"))
            for item in page.items
            if isinstance(item.get("requester_key"), str)
        )
        profiles = self._reader.load_profiles(requester_keys=tuple(dict.fromkeys(requester_keys)))
        items = tuple(self._list_item(item, profiles) for item in page.items)
        next_cursor = None
        if page.last_evaluated_key is not None:
            next_cursor = self._cursor_codec.encode(
                query=query,
                index_name=page.index_name,
                last_evaluated_key=page.last_evaluated_key,
                now=now,
            )
        return RecordListResponse(schema_version=1, items=items, next_cursor=next_cursor)

    def get_record(self, *, record_id: str, now: datetime) -> RecordDetailResponse:
        if not _is_record_id(record_id):
            raise ReadFailure("REQUEST_INVALID", 400)
        items = self._reader.load_record(record_id=record_id)
        if not items:
            raise ReadFailure("RECORD_NOT_FOUND", 404)
        by_key: dict[str, DynamoItem] = {}
        for item in items:
            sk = item.get("SK")
            if not isinstance(sk, str) or sk in by_key:
                raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
            by_key[sk] = item
        expected = {
            "META",
            "DECISION",
            "PROJECTION#V1",
            *(f"INITIAL#{slot}" for slot in PARTICIPANT_SLOTS),
            *(f"FINAL#{slot}" for slot in PARTICIPANT_SLOTS),
            *(f"VOTE#{slot}" for slot in PARTICIPANT_SLOTS),
        }
        if set(by_key) != expected:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        _validate_archive_items(by_key, record_id=record_id)
        meta = by_key["META"]
        requester_key = _required_text(meta, "requester_key")
        profiles = self._reader.load_profiles(requester_keys=(requester_key,))
        requester = self._requester(
            meta,
            profiles.get(requester_key),
        )
        stored_winner = _required_text(meta, "winner")
        decision_winner = _required_text(by_key["DECISION"], "winner")
        if stored_winner != decision_winner:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "recordId": _required_text(meta, "record_id"),
            "completedAt": _required_text(meta, "completed_at"),
            "question": _required_text(meta, "question"),
            "requester": requester,
            "participants": self._participants(meta),
            "initialOpinions": tuple(
                {
                    "participant": slot,
                    "summary": _required_text(by_key[f"INITIAL#{slot}"], "summary"),
                    "proposal": _required_text(by_key[f"INITIAL#{slot}"], "proposal"),
                }
                for slot in PARTICIPANT_SLOTS
            ),
            "finalProposals": tuple(
                {
                    "participant": slot,
                    "title": _required_text(by_key[f"FINAL#{slot}"], "title"),
                    "proposal": _required_text(by_key[f"FINAL#{slot}"], "proposal"),
                }
                for slot in PARTICIPANT_SLOTS
            ),
            "votes": tuple(
                {
                    "voter": slot,
                    "candidate": _required_text(by_key[f"VOTE#{slot}"], "candidate"),
                    "reason": _required_text(by_key[f"VOTE#{slot}"], "reason"),
                }
                for slot in PARTICIPANT_SLOTS
            ),
            "result": self._result(meta),
            "finalDecision": {
                "winner": decision_winner,
                "victoryMessage": by_key["DECISION"].get("victory_message"),
                "decision": _required_text(by_key["DECISION"], "decision"),
                "actions": _required_text_list(by_key["DECISION"], "actions"),
                "caveats": _required_text_list(by_key["DECISION"], "caveats"),
            },
        }
        try:
            result = RecordDetailResponse.model_validate(payload)
        except ValidationError as error:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503) from error
        if result.record_id != record_id:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        return result

    def get_rankings(self, *, now: datetime) -> RankingsResponse:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReadFailure("REQUEST_INVALID", 400)
        raw_items = self._reader.load_ranking_snapshots()
        by_kind: dict[str, DynamoItem] = {}
        for item in raw_items:
            kind = item.get("ranking_kind")
            if not isinstance(kind, str) or kind in by_kind:
                raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
            by_kind[kind] = item
        if set(by_kind) != {"wins", "requests"}:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
        wins_item = by_kind["wins"]
        requests_item = by_kind["requests"]
        generated_at = _validate_ranking_snapshot(wins_item, kind="wins")
        requests_generated_at = _validate_ranking_snapshot(requests_item, kind="requests")
        if generated_at != requests_generated_at:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)

        wins_raw = cast(list[DynamoItem], wins_item["entries"])
        requests_raw = cast(list[DynamoItem], requests_item["entries"])
        requester_keys = tuple(_required_text(item, "requester_key") for item in requests_raw)
        profiles = self._reader.load_profiles(requester_keys=requester_keys)
        variants: dict[ParticipantSlot, FallbackVariant] = {
            "participant-a": "cyan",
            "participant-b": "pink",
            "participant-c": "lavender",
        }
        wins = tuple(
            RankingEntry(
                rank=cast(int, item["rank"]),
                display_name=_required_text(item, "display_name"),
                avatar=self._avatar(
                    asset_key=PARTICIPANT_AVATAR_ASSET_KEYS[
                        cast(ParticipantSlot, item["participant"])
                    ],
                    alt=f"{_required_text(item, 'display_name')}のアバター",
                    fallback_variant=variants[cast(ParticipantSlot, item["participant"])],
                    prefix="participants/",
                ),
                count=cast(int, item["count"]),
            )
            for item in wins_raw
        )
        requests: list[RankingEntry] = []
        for item in requests_raw:
            requester_key = _required_text(item, "requester_key")
            profile = profiles.get(requester_key)
            display_name = (
                profile.display_name
                if profile is not None
                else _required_text(item, "display_name")
            )
            requests.append(
                RankingEntry(
                    rank=cast(int, item["rank"]),
                    display_name=display_name,
                    avatar=self._avatar(
                        asset_key=profile.avatar_asset_key if profile is not None else None,
                        alt=f"{display_name}のアバター",
                        fallback_variant=_requester_variant(requester_key),
                        prefix="requesters/",
                    ),
                    count=cast(int, item["count"]),
                )
            )
        return RankingsResponse(
            schema_version=1,
            wins=wins,
            requests=tuple(requests),
            generated_at=generated_at,
        )

    def get_costs(self, *, period: CostPeriod, now: datetime) -> CostsResponse:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ReadFailure("REQUEST_INVALID", 400)
        try:
            costs, rates = self._reader.load_cost_ledger()
            view = build_cost_view(
                costs=costs,
                rates=rates,
                period=period,
                now=now,
            )
            return CostsResponse(
                schema_version=1,
                period=view.period,
                time_zone="Asia/Tokyo",
                start_date=view.start_date,
                end_date=view.end_date,
                currency="JPY",
                total=view.total_jpy,
                breakdown=CostBreakdown(
                    fargate=view.amounts_jpy["FARGATE"],
                    lambda_=view.amounts_jpy["LAMBDA"],
                    openai=view.amounts_jpy["OPENAI"],
                    other_aws=view.amounts_jpy["OTHER_AWS"],
                ),
                conversion=CostConversion(
                    source="frankfurter-v2",
                    method="daily-reference-rate",
                    base_currency="USD",
                    updated_at=view.conversion_updated_at,
                ),
                updated_at=view.updated_at,
                status=view.status,
            )
        except (CostDataInvalid, ValidationError) as error:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503) from error

    def _list_item(
        self,
        item: DynamoItem,
        profiles: dict[str, RequesterProfile],
    ) -> RecordListItem:
        _validate_meta_item(item)
        requester_key = _required_text(item, "requester_key")
        question = " ".join(_required_text(item, "question").split())[:160]
        if not question:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        try:
            return RecordListItem.model_validate(
                {
                    "schemaVersion": 1,
                    "recordId": _required_text(item, "record_id"),
                    "completedAt": _required_text(item, "completed_at"),
                    "questionPreview": question,
                    "requester": self._requester(
                        item,
                        profiles.get(requester_key),
                    ),
                    "participants": self._participants(item),
                    "result": self._result(item),
                }
            )
        except ValidationError as error:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503) from error

    def _requester(
        self,
        meta: DynamoItem,
        profile: RequesterProfile | None,
    ) -> dict[str, Any]:
        requester_key = _required_text(meta, "requester_key")
        display_name = (
            profile.display_name
            if profile is not None
            else _required_text(meta, "requester_display_name")
        )
        variant = _requester_variant(requester_key)
        avatar = self._avatar(
            asset_key=profile.avatar_asset_key if profile is not None else None,
            alt=f"{display_name}のアバター",
            fallback_variant=variant,
            prefix="requesters/",
        )
        return {"displayName": display_name, "avatar": avatar.model_dump(by_alias=True)}

    def _participants(self, meta: DynamoItem) -> tuple[dict[str, Any], ...]:
        raw = meta.get("participants")
        if not isinstance(raw, dict) or set(raw) != set(PARTICIPANT_SLOTS):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        variants: dict[ParticipantSlot, FallbackVariant] = {
            "participant-a": "cyan",
            "participant-b": "pink",
            "participant-c": "lavender",
        }
        result: list[dict[str, Any]] = []
        for slot in PARTICIPANT_SLOTS:
            profile = raw[slot]
            if not isinstance(profile, dict):
                raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
            display_name = _required_text(profile, "display_name")
            avatar = self._avatar(
                asset_key=PARTICIPANT_AVATAR_ASSET_KEYS[slot],
                alt=f"{display_name}のアバター",
                fallback_variant=variants[slot],
                prefix="participants/",
            )
            result.append(
                {
                    "slot": slot,
                    "displayName": display_name,
                    "avatar": avatar.model_dump(by_alias=True),
                }
            )
        return tuple(result)

    @staticmethod
    def _result(meta: DynamoItem) -> dict[str, Any]:
        raw_counts = meta.get("vote_counts")
        if not isinstance(raw_counts, dict) or set(raw_counts) != set(PARTICIPANT_SLOTS):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        counts: list[dict[str, Any]] = []
        for slot in PARTICIPANT_SLOTS:
            count = raw_counts[slot]
            if isinstance(count, bool) or not isinstance(count, int):
                raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
            counts.append({"participant": slot, "count": count})
        tie = meta.get("tie_break_applied")
        if not isinstance(tie, bool):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        return {
            "winner": _required_text(meta, "winner"),
            "voteCounts": tuple(counts),
            "tieBreakApplied": tie,
        }

    def _avatar(
        self,
        *,
        asset_key: str | None,
        alt: str,
        fallback_variant: FallbackVariant,
        prefix: str,
    ) -> ImageAvatarRef | PlaceholderAvatarRef:
        if asset_key is not None and _valid_asset_key(asset_key, prefix=prefix):
            return ImageAvatarRef(
                kind="image",
                url=self._reader.avatar_url(asset_key=asset_key),
                alt=alt,
                fallback_variant=fallback_variant,
            )
        return PlaceholderAvatarRef(
            kind="placeholder",
            alt=alt,
            fallback_variant=fallback_variant,
        )


def validate_list_query(query: ListQuery) -> ListQuery:
    if isinstance(query.limit, bool) or not 1 <= query.limit <= 50:
        raise ReadFailure("REQUEST_INVALID", 400)
    if query.sort not in {"newest", "oldest"}:
        raise ReadFailure("REQUEST_INVALID", 400)
    if query.winner is not None and query.winner not in PARTICIPANT_SLOTS:
        raise ReadFailure("REQUEST_INVALID", 400)
    return ListQuery(
        limit=query.limit,
        sort=query.sort,
        winner=query.winner,
        cursor=query.cursor,
    )


def _required_text(item: DynamoItem, field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
    return value


def _required_text_list(item: DynamoItem, field: str) -> tuple[str, ...]:
    value = item.get(field)
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry.strip() for entry in value
    ):
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
    return tuple(cast(str, entry) for entry in value)


def _requester_variant(requester_key: str) -> FallbackVariant:
    variants: tuple[FallbackVariant, ...] = ("cyan", "pink", "lavender")
    return variants[hashlib.sha256(requester_key.encode()).digest()[0] % len(variants)]


def _valid_asset_key(value: str, *, prefix: str) -> bool:
    return (
        value.startswith(prefix)
        and len(value) <= 256
        and ".." not in value
        and not value.startswith("/")
        and all(character.isalnum() or character in "/._-" for character in value)
    )


def _is_record_id(value: str) -> bool:
    return len(value) == RECORD_ID_LENGTH and all(
        character.isalnum() or character in "_-" for character in value
    )


def _validate_cursor_key(value: Any, *, index_name: str) -> DynamoItem:
    expected_fields = {"PK", "SK", f"{index_name}pk", f"{index_name}sk"}
    if index_name not in {"gsi1", "gsi2"} or not isinstance(value, dict):
        raise ValueError("cursor key is invalid")
    if set(value) != expected_fields or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("cursor key is invalid")
    return cast(DynamoItem, dict(value))


def _validate_meta_item(item: DynamoItem) -> None:
    record_id = item.get("record_id")
    if (
        item.get("schema_version") != 1
        or item.get("record_type") != "archive_meta"
        or not isinstance(record_id, str)
        or not _is_record_id(record_id)
        or item.get("PK") != f"RECORD#{record_id}"
        or item.get("SK") != "META"
    ):
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)


def _validate_list_projection(
    item: DynamoItem,
    *,
    index_name: str,
    winner: ParticipantSlot | None,
) -> None:
    _validate_meta_item(item)
    record_id = _required_text(item, "record_id")
    completed_text = _required_text(item, "completed_at")
    stored_winner = _required_text(item, "winner")
    if stored_winner not in PARTICIPANT_SLOTS:
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
    try:
        completed_at = TypeAdapter(AwareDatetime).validate_python(completed_text).astimezone(UTC)
    except OverflowError, ValidationError:
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503) from None
    if completed_at.isoformat() != completed_text:
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)

    expected_sort_key = f"{completed_text}#{record_id}"
    if index_name == "gsi1":
        if winner is not None or item.get("gsi1pk") != "ARCHIVE#COMPLETED":
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        if item.get("gsi1sk") != expected_sort_key:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
    elif index_name == "gsi2":
        if winner is None or stored_winner != winner:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        if item.get("gsi2pk") != f"WINNER#{stored_winner}":
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        if item.get("gsi2sk") != expected_sort_key:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
    else:
        raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)


def _validate_archive_items(items: dict[str, DynamoItem], *, record_id: str) -> None:
    expected_types = {
        "META": "archive_meta",
        "DECISION": "final_decision",
        "PROJECTION#V1": "projection_marker",
        **{f"INITIAL#{slot}": "initial_opinion" for slot in PARTICIPANT_SLOTS},
        **{f"FINAL#{slot}": "final_proposal" for slot in PARTICIPANT_SLOTS},
        **{f"VOTE#{slot}": "vote" for slot in PARTICIPANT_SLOTS},
    }
    for sk, item in items.items():
        if (
            item.get("schema_version") != 1
            or item.get("record_id") != record_id
            or item.get("PK") != f"RECORD#{record_id}"
            or item.get("record_type") != expected_types[sk]
        ):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)


def _validate_ranking_snapshot(item: DynamoItem, *, kind: Literal["wins", "requests"]) -> datetime:
    expected_pk = "RANKING#WINS" if kind == "wins" else "RANKING#REQUESTS"
    expected_fields = {
        "PK",
        "SK",
        "schema_version",
        "record_type",
        "ranking_kind",
        "generated_at",
        "archive_count",
        "entries",
    }
    archive_count = item.get("archive_count")
    entries = item.get("entries")
    if (
        set(item) != expected_fields
        or item.get("PK") != expected_pk
        or item.get("SK") != "CURRENT"
        or item.get("schema_version") != 1
        or item.get("record_type") != "ranking_snapshot"
        or item.get("ranking_kind") != kind
        or isinstance(archive_count, bool)
        or not isinstance(archive_count, int)
        or archive_count < 0
        or not isinstance(entries, list)
    ):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    try:
        generated_at = (
            TypeAdapter(AwareDatetime)
            .validate_python(_required_text(item, "generated_at"))
            .astimezone(UTC)
        )
    except OverflowError, ValidationError:
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503) from None
    if generated_at.isoformat() != item["generated_at"]:
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    if kind == "wins":
        _validate_win_entries(cast(list[DynamoItem], entries), archive_count=archive_count)
    else:
        _validate_request_entries(cast(list[DynamoItem], entries), archive_count=archive_count)
    return generated_at


def _validate_win_entries(entries: list[DynamoItem], *, archive_count: int) -> None:
    if (archive_count == 0 and entries) or (archive_count > 0 and len(entries) != 3):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    participants: list[str] = []
    sortable: list[tuple[int, str, str]] = []
    counts: list[int] = []
    for item in entries:
        if set(item) != {"participant", "display_name", "count", "rank"}:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
        participant = _required_text(item, "participant")
        if participant not in PARTICIPANT_SLOTS:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
        display_name = _required_text(item, "display_name")
        count, rank = _ranking_numbers(item)
        participants.append(participant)
        counts.append(count)
        sortable.append((-count, display_name, participant))
        if rank < 1:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    if (
        (entries and set(participants) != set(PARTICIPANT_SLOTS))
        or sum(counts) != archive_count
        or sortable != sorted(sortable)
        or not _valid_competition_ranks(entries)
    ):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)


def _validate_request_entries(entries: list[DynamoItem], *, archive_count: int) -> None:
    if len(entries) > 10 or (archive_count == 0 and entries) or (archive_count > 0 and not entries):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    keys: list[str] = []
    sortable: list[tuple[int, str, str]] = []
    total = 0
    for item in entries:
        if set(item) != {"requester_key", "display_name", "count", "rank"}:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
        key = _required_text(item, "requester_key")
        display_name = _required_text(item, "display_name")
        count, rank = _ranking_numbers(item)
        if count < 1 or rank < 1:
            raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
        keys.append(key)
        total += count
        sortable.append((-count, display_name, key))
    if (
        len(set(keys)) != len(keys)
        or total > archive_count
        or sortable != sorted(sortable)
        or not _valid_competition_ranks(entries)
    ):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)


def _ranking_numbers(item: DynamoItem) -> tuple[int, int]:
    count = item.get("count")
    rank = item.get("rank")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or isinstance(rank, bool)
        or not isinstance(rank, int)
    ):
        raise ReadFailure("INSIGHTS_UNAVAILABLE", 503)
    return count, rank


def _valid_competition_ranks(entries: list[DynamoItem]) -> bool:
    previous_count: int | None = None
    previous_rank = 0
    for index, item in enumerate(entries, start=1):
        count = cast(int, item["count"])
        expected_rank = previous_rank if count == previous_count else index
        if item["rank"] != expected_rank:
            return False
        previous_count = count
        previous_rank = expected_rank
    return True


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReadFailure("REQUEST_INVALID", 400)
    return value.astimezone(UTC)
