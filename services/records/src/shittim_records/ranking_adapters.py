"""DynamoDB adapters for ranking aggregation and atomic snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.rankings import AffectionProfileSeed, RankingSnapshot

AFFECTION_PAGE_SIZE = 50
MAX_DYNAMODB_ITEM_BYTES = 400 * 1024
AFFECTION_CURSOR_RETENTION = timedelta(hours=1)
AFFECTION_RETIREMENT_GRACE = timedelta(minutes=15)
AFFECTION_GENERATION_RETENTION = AFFECTION_CURSOR_RETENTION + AFFECTION_RETIREMENT_GRACE
MAX_BATCH_WRITE_ATTEMPTS = 5


class DynamoRankingSource:
    """Read every completed Archive metadata row through GSI1 pagination."""

    def __init__(
        self,
        client: DynamoDBClient,
        table_name: str,
        statistics_table_name: str | None = None,
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._statistics_table = statistics_table_name

    def list_completed_meta(self) -> tuple[DynamoItem, ...]:
        items: list[DynamoItem] = []
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self._table_name,
            IndexName="gsi1",
            KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "gsi1pk"},
            ExpressionAttributeValues=marshal_item({":pk": "ARCHIVE#COMPLETED"}),
            ScanIndexForward=True,
        )
        for page in pages:
            items.extend(unmarshal_item(item) for item in page.get("Items", []))
        return tuple(items)

    def list_affection_profiles(self) -> tuple[DynamoItem, ...]:
        if self._statistics_table is None:
            return ()
        items: list[DynamoItem] = []
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self._statistics_table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues=marshal_item({":pk": "AFFECTION#PROFILE"}),
            ConsistentRead=True,
        )
        for page in pages:
            items.extend(unmarshal_item(item) for item in page.get("Items", []))
        return tuple(items)

    def seed_default_affection_profiles(
        self,
        seeds: tuple[AffectionProfileSeed, ...],
        *,
        updated_at: datetime,
    ) -> None:
        if self._statistics_table is None:
            if seeds:
                raise RuntimeError("affection statistics table is unavailable")
            return
        items: tuple[DynamoItem, ...] = tuple(
            cast(
                DynamoItem,
                {
                    "PK": "AFFECTION#PROFILE",
                    "SK": seed.requester_key,
                    "schema_version": 1,
                    "record_type": "affection_profile",
                    "source_version": 0,
                    "display_name": seed.display_name,
                    "scores": {
                        "participant-a": 500,
                        "participant-b": 500,
                        "participant-c": 500,
                    },
                    "updated_at": updated_at.isoformat(),
                },
            )
            for seed in seeds
        )
        for offset in range(0, len(items), 100):
            pending = items[offset : offset + 100]
            for attempt in range(3):
                if not pending:
                    break
                try:
                    self._client.transact_write_items(
                        TransactItems=[
                            {
                                "Put": {
                                    "TableName": self._statistics_table,
                                    "Item": marshal_item(item),
                                    "ConditionExpression": (
                                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                                    ),
                                }
                            }
                            for item in pending
                        ],
                        ClientRequestToken=_seed_token(pending, attempt=attempt),
                    )
                    pending = ()
                except ClientError as error:
                    if (
                        error.response.get("Error", {}).get("Code")
                        != "TransactionCanceledException"
                    ):
                        raise
                    existing = self._existing_affection_keys(
                        tuple(str(item["SK"]) for item in pending)
                    )
                    if not existing:
                        raise RuntimeError("affection seed transaction failed") from error
                    pending = tuple(item for item in pending if item["SK"] not in existing)
            if pending:
                raise RuntimeError("affection seed transaction did not converge")

    def _existing_affection_keys(self, keys: tuple[str, ...]) -> set[str]:
        if self._statistics_table is None or not keys:
            return set()
        request_items: dict[str, Any] = {
            self._statistics_table: {
                "Keys": [marshal_item({"PK": "AFFECTION#PROFILE", "SK": key}) for key in keys],
                "ConsistentRead": True,
            }
        }
        found: set[str] = set()
        for _attempt in range(5):
            response = self._client.batch_get_item(RequestItems=request_items)
            for raw in response.get("Responses", {}).get(self._statistics_table, []):
                item = unmarshal_item(raw)
                key = item.get("SK")
                if isinstance(key, str):
                    found.add(key)
            unprocessed = response.get("UnprocessedKeys", {})
            if not unprocessed:
                return found
            request_items = unprocessed
        raise RuntimeError("affection seed read did not converge")


class DynamoRankingSnapshotStore:
    """Replace the complete ranking generation in one DynamoDB transaction."""

    def __init__(
        self,
        client: DynamoDBClient,
        table_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._table_name = table_name
        self._clock = clock or (lambda: datetime.now(UTC))

    def save_rankings(self, snapshot: RankingSnapshot) -> None:
        generated_at = snapshot.generated_at.isoformat()
        wins: DynamoItem = {
            "PK": "RANKING#WINS",
            "SK": "CURRENT",
            "schema_version": 1,
            "record_type": "ranking_snapshot",
            "ranking_kind": "wins",
            "generated_at": generated_at,
            "archive_count": snapshot.archive_count,
            "entries": [
                {
                    "participant": entry.participant,
                    "display_name": entry.display_name,
                    "count": entry.count,
                    "rank": entry.rank,
                }
                for entry in snapshot.wins
            ],
        }
        requests: DynamoItem = {
            "PK": "RANKING#REQUESTS",
            "SK": "CURRENT",
            "schema_version": 1,
            "record_type": "ranking_snapshot",
            "ranking_kind": "requests",
            "generated_at": generated_at,
            "archive_count": snapshot.archive_count,
            "entries": [
                {
                    "requester_key": entry.requester_key,
                    "display_name": entry.display_name,
                    "count": entry.count,
                    "rank": entry.rank,
                }
                for entry in snapshot.requests
            ],
        }
        pointer: DynamoItem | None = None
        previous_pointer: DynamoItem | None = None
        if snapshot.affection:
            previous_pointer = self._load_affection_pointer()
            immutable_items, catalog, pointer = _affection_generation_items(snapshot)
            self._put_catalog(catalog)
            for item in immutable_items:
                self._put_immutable(item)
        seed_checkpoint: DynamoItem = {
            "PK": "AFFECTION#SEED",
            "SK": "CURRENT",
            "schema_version": 1,
            "record_type": "affection_seed_checkpoint",
            "generated_at": generated_at,
            "archive_count": snapshot.archive_count,
            "profile_count": snapshot.affection_profile_count,
            "complete": True,
        }
        generated_items = (wins, requests)
        switch_time: datetime | None = None
        if pointer is not None:
            switch_time = self._clock()
            if switch_time.tzinfo is None or switch_time.utcoffset() is None:
                raise ValueError("ranking snapshot clock must be timezone-aware")
            switch_time = switch_time.astimezone(UTC)
            generated_items = (*generated_items, seed_checkpoint, pointer)
            retired = _retired_catalog_item(
                previous_pointer,
                active_generation_id=cast(str, pointer["generation_id"]),
                retired_at=switch_time,
            )
            if retired is not None:
                generated_items = (*generated_items, retired)
        actions: list[TransactWriteItemTypeDef] = []
        for item in generated_items:
            put: dict[str, Any] = {
                "TableName": self._table_name,
                "Item": marshal_item(item),
            }
            if item is pointer:
                previous_generation = (
                    previous_pointer.get("generation_id") if previous_pointer is not None else None
                )
                put["ExpressionAttributeNames"] = {"#generation": "generation_id"}
                if _is_generation_id(previous_generation):
                    put["ConditionExpression"] = "#generation = :previous"
                    put["ExpressionAttributeValues"] = marshal_item(
                        {":previous": previous_generation}
                    )
                else:
                    put["ConditionExpression"] = "attribute_not_exists(#generation)"
            actions.append(cast(Any, {"Put": put}))
        self._client.transact_write_items(TransactItems=actions)
        if pointer is not None and switch_time is not None:
            # The content-free catalog remains until a later refresh can converge.
            with suppress(BotoCoreError, ClientError, RuntimeError):
                self._cleanup_affection_generations(
                    active_generation_id=cast(str, pointer["generation_id"]),
                    now=switch_time,
                )

    def _load_affection_pointer(self) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": "RANKING#AFFECTION", "SK": "CURRENT"}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)

    def _put_immutable(self, item: DynamoItem) -> None:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(item),
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                ConsistentRead=True,
            )
            existing = response.get("Item")
            if existing is None or unmarshal_item(existing) != item:
                raise RuntimeError("immutable affection ranking page conflicts") from error

    def _put_catalog(self, item: DynamoItem) -> None:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(item),
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            response = self._client.get_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                ConsistentRead=True,
            )
            existing = response.get("Item")
            if existing is None or not _same_catalog_generation(unmarshal_item(existing), item):
                raise RuntimeError("affection ranking catalog conflicts") from error

    def _cleanup_affection_generations(
        self,
        *,
        active_generation_id: str,
        now: datetime,
    ) -> None:
        paginator = self._client.get_paginator("query")
        pages = paginator.paginate(
            TableName=self._table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues=marshal_item({":pk": "RANKING#AFFECTION#CATALOG"}),
            ConsistentRead=True,
        )
        for page in pages:
            for raw in page.get("Items", []):
                item = unmarshal_item(raw)
                candidate = _cleanup_candidate(
                    item,
                    active_generation_id=active_generation_id,
                    now=now,
                )
                if candidate is None:
                    continue
                generation_id, page_count = candidate
                partition_key = f"RANKING#AFFECTION#GEN#{generation_id}"
                self._batch_delete(
                    (
                        {"PK": partition_key, "SK": "META"},
                        *(
                            {"PK": partition_key, "SK": f"PAGE#{index:06d}"}
                            for index in range(page_count)
                        ),
                    )
                )
                self._client.delete_item(
                    TableName=self._table_name,
                    Key=marshal_item(
                        {
                            "PK": "RANKING#AFFECTION#CATALOG",
                            "SK": generation_id,
                        }
                    ),
                )

    def _batch_delete(self, keys: tuple[DynamoItem, ...]) -> None:
        for offset in range(0, len(keys), 25):
            pending: dict[str, Any] = {
                self._table_name: [
                    {"DeleteRequest": {"Key": marshal_item(key)}}
                    for key in keys[offset : offset + 25]
                ]
            }
            for _attempt in range(MAX_BATCH_WRITE_ATTEMPTS):
                response = self._client.batch_write_item(RequestItems=pending)
                unprocessed = response.get("UnprocessedItems", {})
                if not unprocessed:
                    break
                pending = cast(dict[str, Any], unprocessed)
            else:
                raise RuntimeError("affection ranking cleanup did not converge")


def _affection_generation_items(
    snapshot: RankingSnapshot,
) -> tuple[tuple[DynamoItem, ...], DynamoItem, DynamoItem]:
    if len(snapshot.affection) != 3:
        raise ValueError("affection snapshot must contain exactly three participant rankings")
    if any(
        len(ranking.entries) != snapshot.affection_profile_count for ranking in snapshot.affection
    ):
        raise ValueError("affection snapshot profile count is inconsistent")
    generated_at = snapshot.generated_at.isoformat()
    canonical = {
        "generated_at": generated_at,
        "profile_count": snapshot.affection_profile_count,
        "rankings": [
            {
                "participant": ranking.participant,
                "display_name": ranking.display_name,
                "entries": [
                    {
                        "requester_key": entry.requester_key,
                        "display_name": entry.display_name,
                        "score": entry.score,
                        "rank": entry.rank,
                    }
                    for entry in ranking.entries
                ],
            }
            for ranking in snapshot.affection
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    generation_id = checksum[:32]
    partition_key = f"RANKING#AFFECTION#GEN#{generation_id}"
    page_count = (snapshot.affection_profile_count + AFFECTION_PAGE_SIZE - 1) // AFFECTION_PAGE_SIZE
    meta: DynamoItem = {
        "PK": partition_key,
        "SK": "META",
        "schema_version": 1,
        "record_type": "affection_ranking_generation",
        "generation_id": generation_id,
        "generated_at": generated_at,
        "profile_count": snapshot.affection_profile_count,
        "page_count": page_count,
        "page_size": AFFECTION_PAGE_SIZE,
        "checksum": checksum,
        "participants": [
            {
                "participant": ranking.participant,
                "display_name": ranking.display_name,
            }
            for ranking in snapshot.affection
        ],
    }
    pages: list[DynamoItem] = []
    for page_index in range(page_count):
        start = page_index * AFFECTION_PAGE_SIZE
        stop = min(start + AFFECTION_PAGE_SIZE, snapshot.affection_profile_count)
        page: DynamoItem = {
            "PK": partition_key,
            "SK": f"PAGE#{page_index:06d}",
            "schema_version": 1,
            "record_type": "affection_ranking_page",
            "generation_id": generation_id,
            "page_index": page_index,
            "offset": start,
            "entry_count": stop - start,
            "rankings": [
                {
                    "participant": ranking.participant,
                    "entries": [
                        {
                            "requester_key": entry.requester_key,
                            "display_name": entry.display_name,
                            "score": entry.score,
                            "rank": entry.rank,
                        }
                        for entry in ranking.entries[start:stop]
                    ],
                }
                for ranking in snapshot.affection
            ],
        }
        _validate_item_size(page)
        pages.append(page)
    _validate_item_size(meta)
    pointer: DynamoItem = {
        "PK": "RANKING#AFFECTION",
        "SK": "CURRENT",
        "schema_version": 1,
        "record_type": "affection_ranking_pointer",
        "generation_id": generation_id,
        "generated_at": generated_at,
        "profile_count": snapshot.affection_profile_count,
        "page_count": page_count,
        "checksum": checksum,
    }
    catalog: DynamoItem = {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": generation_id,
        "schema_version": 1,
        "record_type": "affection_ranking_catalog",
        "generation_id": generation_id,
        "created_at": generated_at,
        "page_count": page_count,
        "checksum": checksum,
    }
    return (meta, *pages), catalog, pointer


def _retired_catalog_item(
    pointer: DynamoItem | None,
    *,
    active_generation_id: str,
    retired_at: datetime,
) -> DynamoItem | None:
    if pointer is None:
        return None
    generation_id = pointer.get("generation_id")
    page_count = pointer.get("page_count")
    checksum = pointer.get("checksum")
    created_at = pointer.get("generated_at")
    if (
        pointer.get("schema_version") != 1
        or pointer.get("record_type") != "affection_ranking_pointer"
        or not _is_generation_id(generation_id)
        or generation_id == active_generation_id
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or not _is_checksum(checksum)
        or _parse_utc(created_at) is None
    ):
        return None
    return {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": generation_id,
        "schema_version": 1,
        "record_type": "affection_ranking_catalog",
        "generation_id": generation_id,
        "created_at": created_at,
        "page_count": page_count,
        "checksum": checksum,
        "retire_after": (retired_at.astimezone(UTC) + AFFECTION_GENERATION_RETENTION).isoformat(),
    }


def _same_catalog_generation(existing: DynamoItem, expected: DynamoItem) -> bool:
    return {key: value for key, value in existing.items() if key != "retire_after"} == expected


def _cleanup_candidate(
    item: DynamoItem,
    *,
    active_generation_id: str,
    now: datetime,
) -> tuple[str, int] | None:
    generation_id = item.get("generation_id")
    page_count = item.get("page_count")
    created_at = _parse_utc(item.get("created_at"))
    retire_after = _parse_utc(item.get("retire_after")) if "retire_after" in item else None
    if (
        item.get("PK") != "RANKING#AFFECTION#CATALOG"
        or item.get("SK") != generation_id
        or item.get("schema_version") != 1
        or item.get("record_type") != "affection_ranking_catalog"
        or not _is_generation_id(generation_id)
        or generation_id == active_generation_id
        or isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 0
        or created_at is None
        or not _is_checksum(item.get("checksum"))
        or ("retire_after" in item and retire_after is None)
    ):
        return None
    cleanup_after = retire_after or (created_at + AFFECTION_GENERATION_RETENTION)
    if cleanup_after > now.astimezone(UTC):
        return None
    return cast(str, generation_id), page_count


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        return None
    return parsed.astimezone(UTC)


def _is_generation_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_checksum(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_item_size(item: DynamoItem) -> None:
    raw = marshal_item(item)
    if len(json.dumps(raw, separators=(",", ":")).encode()) > MAX_DYNAMODB_ITEM_BYTES:
        raise ValueError("affection ranking page exceeds DynamoDB item size limit")


def _seed_token(items: tuple[DynamoItem, ...], *, attempt: int) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [(item["SK"], item["updated_at"]) for item in items],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"affection-seed-{attempt}-{digest[:16]}"
