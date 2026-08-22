"""DynamoDB adapters for ranking aggregation and atomic snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.rankings import RankingSnapshot


class DynamoRankingSource:
    """Read every completed Archive metadata row through GSI1 pagination."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

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


class DynamoRankingSnapshotStore:
    """Replace both ranking snapshots in one DynamoDB transaction."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

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
        actions: list[TransactWriteItemTypeDef] = [
            {"Put": {"TableName": self._table_name, "Item": marshal_item(item)}}
            for item in (wins, requests)
        ]
        self._client.transact_write_items(TransactItems=actions)
