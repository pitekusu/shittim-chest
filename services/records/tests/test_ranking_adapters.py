"""DynamoDB ranking source and atomic snapshot adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.ranking_adapters import DynamoRankingSnapshotStore, DynamoRankingSource
from shittim_records.rankings import (
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
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.paginator = FakePaginator(pages or [])
        self.transactions: list[dict[str, Any]] = []

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "query"
        return self.paginator

    def transact_write_items(self, **kwargs: Any) -> None:
        self.transactions.append(kwargs)


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

    DynamoRankingSnapshotStore(cast(Any, client), "statistics").save_rankings(snapshot)

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
