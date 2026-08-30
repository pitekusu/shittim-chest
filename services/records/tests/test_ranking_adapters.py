"""DynamoDB ranking source and atomic snapshot adapter tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.ranking_adapters import DynamoRankingSnapshotStore, DynamoRankingSource
from shittim_records.rankings import (
    AffectionProfileSeed,
    AffectionRankingEntry,
    ParticipantAffectionRanking,
    ParticipantRanking,
    RankingSnapshot,
    RequesterRanking,
)


class FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.pages


class FakeDynamo:
    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        *,
        fail_put_at: int | None = None,
        get_responses: list[dict[str, Any]] | None = None,
        leave_writes_unprocessed: bool = False,
    ) -> None:
        self.paginator = FakePaginator(pages or [])
        self.transactions: list[dict[str, Any]] = []
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.batch_get_calls: list[dict[str, Any]] = []
        self.batch_write_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.fail_put_at = fail_put_at
        self.get_responses = get_responses or []
        self.leave_writes_unprocessed = leave_writes_unprocessed

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "query"
        return self.paginator

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)

    def put_item(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        if self.fail_put_at == len(self.put_calls):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "PutItem",
            )

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        return self.get_responses.pop(0) if self.get_responses else {}

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_get_calls.append(kwargs)
        return {"Responses": {"statistics": []}}

    def batch_write_item(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_write_calls.append(kwargs)
        if self.leave_writes_unprocessed:
            return {"UnprocessedItems": kwargs["RequestItems"]}
        return {}

    def delete_item(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)


def snapshot_store(
    client: FakeDynamo,
    *,
    now: datetime = datetime(2026, 8, 22, tzinfo=UTC),
) -> DynamoRankingSnapshotStore:
    return DynamoRankingSnapshotStore(
        cast(Any, client),
        "statistics",
        clock=lambda: now,
    )


def test_source_queries_every_gsi1_page_in_ascending_order() -> None:
    client = FakeDynamo(
        [
            {"Items": [marshal_item({"PK": "RECORD#a", "SK": "META"})]},
            {"Items": [marshal_item({"PK": "RECORD#b", "SK": "META"})]},
        ]
    )

    result = DynamoRankingSource(cast(Any, client), "archive").list_completed_meta()

    assert result == (
        {"PK": "RECORD#a", "SK": "META"},
        {"PK": "RECORD#b", "SK": "META"},
    )
    assert client.paginator.calls == [
        {
            "TableName": "archive",
            "IndexName": "gsi1",
            "KeyConditionExpression": "#pk = :pk",
            "ExpressionAttributeNames": {"#pk": "gsi1pk"},
            "ExpressionAttributeValues": marshal_item({":pk": "ARCHIVE#COMPLETED"}),
            "ScanIndexForward": True,
        }
    ]


def test_store_replaces_both_snapshots_in_one_transaction() -> None:
    client = FakeDynamo()
    snapshot = RankingSnapshot(
        generated_at=datetime(2026, 8, 22, tzinfo=UTC),
        archive_count=3,
        wins=(ParticipantRanking("participant-a", "Arona", 3, 1),),
        requests=(RequesterRanking("requester", "Requester", 3, 1),),
    )

    snapshot_store(client).save_rankings(snapshot)

    assert len(client.transactions) == 1
    actions = client.transactions[0]["TransactItems"]
    assert len(actions) == 2
    items = [unmarshal_item(action["Put"]["Item"]) for action in actions]
    assert [(item["PK"], item["ranking_kind"]) for item in items] == [
        ("RANKING#WINS", "wins"),
        ("RANKING#REQUESTS", "requests"),
    ]
    assert all(item["generated_at"] == "2026-08-22T00:00:00+00:00" for item in items)
    assert all(item["archive_count"] == 3 for item in items)


def affection_snapshot(*, profile_count: int = 51) -> RankingSnapshot:
    rankings = tuple(
        ParticipantAffectionRanking(
            participant=cast(Any, slot),
            display_name=name,
            entries=tuple(
                AffectionRankingEntry(
                    requester_key=f"requester-{index:03d}",
                    display_name=f"Requester {index:03d}",
                    score=1000 - index,
                    rank=index + 1,
                )
                for index in range(profile_count)
            ),
        )
        for slot, name in (
            ("participant-a", "Arona"),
            ("participant-b", "Plana"),
            ("participant-c", "Participant C"),
        )
    )
    return RankingSnapshot(
        generated_at=datetime(2026, 8, 22, tzinfo=UTC),
        archive_count=profile_count,
        wins=(ParticipantRanking("participant-a", "Arona", profile_count, 1),),
        requests=(RequesterRanking("requester", "Requester", profile_count, 1),),
        affection=cast(Any, rankings),
        affection_profile_count=profile_count,
    )


def test_store_writes_immutable_pages_before_switching_active_pointer() -> None:
    client = FakeDynamo()

    snapshot_store(client).save_rankings(affection_snapshot())

    written = [unmarshal_item(call["Item"]) for call in client.put_calls]
    assert [item["SK"] for item in written] == [
        written[0]["generation_id"],
        "META",
        "PAGE#000000",
        "PAGE#000001",
    ]
    assert written[0]["PK"] == "RANKING#AFFECTION#CATALOG"
    assert written[2]["entry_count"] == 50
    assert written[3]["entry_count"] == 1
    first_page = cast(list[dict[str, Any]], written[2]["rankings"])
    second_page = cast(list[dict[str, Any]], written[3]["rankings"])
    assert all(len(ranking["entries"]) == 50 for ranking in first_page)
    assert all(len(ranking["entries"]) == 1 for ranking in second_page)

    assert len(client.transactions) == 1
    switched = [
        unmarshal_item(action["Put"]["Item"]) for action in client.transactions[0]["TransactItems"]
    ]
    assert [item["PK"] for item in switched] == [
        "RANKING#WINS",
        "RANKING#REQUESTS",
        "AFFECTION#SEED",
        "RANKING#AFFECTION",
    ]
    assert client.transactions[0]["TransactItems"][-1]["Put"]["ConditionExpression"] == (
        "attribute_not_exists(#generation)"
    )
    assert switched[-1]["record_type"] == "affection_ranking_pointer"
    assert "rankings" not in switched[-1]


def test_store_does_not_switch_pointer_when_immutable_page_write_fails() -> None:
    client = FakeDynamo(fail_put_at=3)

    with pytest.raises(ClientError):
        snapshot_store(client).save_rankings(affection_snapshot())

    assert client.transactions == []
    catalog = unmarshal_item(client.put_calls[0]["Item"])
    assert catalog["PK"] == "RANKING#AFFECTION#CATALOG"


def test_later_refresh_cleans_incomplete_generation_tracked_before_page_failure() -> None:
    failed = FakeDynamo(fail_put_at=4)
    original = affection_snapshot()
    with pytest.raises(ClientError):
        snapshot_store(failed).save_rankings(original)
    orphan_catalog = unmarshal_item(failed.put_calls[0]["Item"])
    orphan_generation = cast(str, orphan_catalog["generation_id"])
    assert unmarshal_item(failed.put_calls[2]["Item"])["SK"] == "PAGE#000000"

    later = datetime(2026, 8, 22, 1, 16, tzinfo=UTC)
    retry = FakeDynamo(pages=[{"Items": [marshal_item(orphan_catalog)]}])
    next_snapshot = replace(original, generated_at=later)

    snapshot_store(retry, now=later).save_rankings(next_snapshot)

    deleted = [
        unmarshal_item(request["DeleteRequest"]["Key"])
        for request in retry.batch_write_calls[0]["RequestItems"]["statistics"]
    ]
    assert deleted == [
        {"PK": f"RANKING#AFFECTION#GEN#{orphan_generation}", "SK": "META"},
        {"PK": f"RANKING#AFFECTION#GEN#{orphan_generation}", "SK": "PAGE#000000"},
        {"PK": f"RANKING#AFFECTION#GEN#{orphan_generation}", "SK": "PAGE#000001"},
    ]
    assert unmarshal_item(retry.delete_calls[0]["Key"]) == {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": orphan_generation,
    }


def test_store_retires_previous_generation_for_one_cursor_ttl_atomically() -> None:
    previous = {
        "PK": "RANKING#AFFECTION",
        "SK": "CURRENT",
        "schema_version": 1,
        "record_type": "affection_ranking_pointer",
        "generation_id": "a" * 32,
        "generated_at": "2026-08-21T23:45:00+00:00",
        "profile_count": 2,
        "page_count": 1,
        "checksum": "b" * 64,
    }
    client = FakeDynamo(get_responses=[{"Item": marshal_item(previous)}])

    snapshot_store(client).save_rankings(affection_snapshot())

    switched = [
        unmarshal_item(action["Put"]["Item"]) for action in client.transactions[0]["TransactItems"]
    ]
    pointer_put = client.transactions[0]["TransactItems"][-2]["Put"]
    retired = switched[-1]
    assert pointer_put["ConditionExpression"] == "#generation = :previous"
    assert unmarshal_item(pointer_put["ExpressionAttributeValues"]) == {":previous": "a" * 32}
    assert retired["PK"] == "RANKING#AFFECTION#CATALOG"
    assert retired["generation_id"] == "a" * 32
    assert retired["retire_after"] == "2026-08-22T01:15:00+00:00"


def test_store_cleans_expired_generation_pages_before_catalog_marker() -> None:
    expired = {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": "a" * 32,
        "schema_version": 1,
        "record_type": "affection_ranking_catalog",
        "generation_id": "a" * 32,
        "created_at": "2026-08-21T22:00:00+00:00",
        "page_count": 2,
        "checksum": "b" * 64,
        "retire_after": "2026-08-21T23:00:00+00:00",
    }
    client = FakeDynamo(pages=[{"Items": [marshal_item(expired)]}])

    snapshot_store(client).save_rankings(affection_snapshot())

    deleted = [
        unmarshal_item(request["DeleteRequest"]["Key"])
        for request in client.batch_write_calls[0]["RequestItems"]["statistics"]
    ]
    assert [item["SK"] for item in deleted] == ["META", "PAGE#000000", "PAGE#000001"]
    assert unmarshal_item(client.delete_calls[0]["Key"]) == {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": "a" * 32,
    }


def test_store_keeps_catalog_marker_when_best_effort_cleanup_does_not_converge() -> None:
    expired = {
        "PK": "RANKING#AFFECTION#CATALOG",
        "SK": "a" * 32,
        "schema_version": 1,
        "record_type": "affection_ranking_catalog",
        "generation_id": "a" * 32,
        "created_at": "2026-08-21T22:00:00+00:00",
        "page_count": 1,
        "checksum": "b" * 64,
        "retire_after": "2026-08-21T23:00:00+00:00",
    }
    client = FakeDynamo(
        pages=[{"Items": [marshal_item(expired)]}],
        leave_writes_unprocessed=True,
    )

    snapshot_store(client).save_rankings(affection_snapshot())

    assert len(client.batch_write_calls) == 5
    assert client.delete_calls == []


def test_seed_profiles_uses_bounded_transactions_instead_of_individual_puts() -> None:
    client = FakeDynamo()
    seeds = tuple(
        AffectionProfileSeed(
            requester_key=f"requester-{index:03d}",
            display_name=f"Requester {index:03d}",
        )
        for index in range(101)
    )

    DynamoRankingSource(
        cast(Any, client),
        "archive",
        "statistics",
    ).seed_default_affection_profiles(
        seeds,
        updated_at=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert [len(call["TransactItems"]) for call in client.transactions] == [100, 1]
    assert client.put_calls == []
