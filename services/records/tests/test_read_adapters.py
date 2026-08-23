"""AWS read adapter tests without network access."""

from __future__ import annotations

from typing import Any, cast

import pytest
from shittim_chest.adapters.dynamodb.codec import marshal_item

from shittim_records.auth import AuthFailure
from shittim_records.read_adapters import (
    DynamoRecordsReader,
    ReadConfigurationRepository,
)
from shittim_records.read_api import ReadFailure


class FakeDynamo:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.queries: list[dict[str, Any]] = []
        self.batch_calls: list[dict[str, Any]] = []
        self.transact_get_calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return self.responses.pop(0)

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_calls.append(kwargs)
        return self.responses.pop(0)

    def transact_get_items(self, **kwargs: Any) -> dict[str, Any]:
        self.transact_get_calls.append(kwargs)
        return self.responses.pop(0)


class FakeS3:
    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        assert operation == "get_object"
        assert kwargs["ExpiresIn"] == 300
        return "https://media.example.invalid/signed"


class FakeSsm:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"Names": ["session-key"], "WithDecryption": True}
        return self.response


def reader(client: FakeDynamo) -> DynamoRecordsReader:
    return DynamoRecordsReader(
        cast(Any, client),
        cast(Any, FakeS3()),
        archive_table_name="archive",
        statistics_table_name="statistics",
        session_table_name="sessions",
        media_bucket_name="media",
    )


def test_rankings_use_one_atomic_read_for_both_snapshots() -> None:
    wins = marshal_item({"PK": "RANKING#WINS", "SK": "CURRENT"})
    requests = marshal_item({"PK": "RANKING#REQUESTS", "SK": "CURRENT"})
    client = FakeDynamo([{"Responses": [{"Item": wins}, {"Item": requests}]}])

    result = reader(client).load_ranking_snapshots()

    assert result == (
        {"PK": "RANKING#WINS", "SK": "CURRENT"},
        {"PK": "RANKING#REQUESTS", "SK": "CURRENT"},
    )
    assert client.transact_get_calls == [
        {
            "TransactItems": [
                {
                    "Get": {
                        "TableName": "statistics",
                        "Key": marshal_item({"PK": "RANKING#WINS", "SK": "CURRENT"}),
                    }
                },
                {
                    "Get": {
                        "TableName": "statistics",
                        "Key": marshal_item({"PK": "RANKING#REQUESTS", "SK": "CURRENT"}),
                    }
                },
            ]
        }
    ]


def test_cost_ledger_queries_cost_and_fx_partitions_strongly_consistently() -> None:
    client = FakeDynamo(
        [
            {
                "Items": [
                    marshal_item(
                        {
                            "PK": "COST#DAILY",
                            "SK": "2026-08-22#OPENAI",
                            "schema_version": 1,
                            "record_type": "daily_cost",
                            "source": "OPENAI",
                            "cost_date": "2026-08-22",
                            "category": "OPENAI",
                            "amount_usd": "0.25",
                            "currency": "USD",
                            "estimated": False,
                            "collected_at": "2026-08-23T00:00:00+00:00",
                        }
                    )
                ]
            },
            {
                "Items": [
                    marshal_item(
                        {
                            "PK": "FX#DAILY",
                            "SK": "2026-08-22#USDJPY",
                            "schema_version": 1,
                            "record_type": "daily_exchange_rate",
                            "source": "FRANKFURTER",
                            "rate_date": "2026-08-22",
                            "base_currency": "USD",
                            "quote_currency": "JPY",
                            "rate": "150",
                            "collected_at": "2026-08-23T00:00:00+00:00",
                        }
                    )
                ]
            },
        ]
    )

    costs, rates = reader(client).load_cost_ledger()

    assert costs[0].amount_usd.as_tuple().exponent == -2
    assert rates[0].usd_jpy == 150
    assert [call["ConsistentRead"] for call in client.queries] == [True, True]
    assert [call["ExpressionAttributeValues"] for call in client.queries] == [
        marshal_item({":pk": "COST#DAILY"}),
        marshal_item({":pk": "FX#DAILY"}),
    ]


@pytest.mark.parametrize(("sort", "scan_forward"), (("newest", False), ("oldest", True)))
def test_list_uses_selected_gsi_and_sort_without_unused_expression_names(
    sort: str,
    scan_forward: bool,
) -> None:
    client = FakeDynamo([{"Items": []}])

    page = reader(client).list_meta(
        limit=12,
        sort=cast(Any, sort),
        winner="participant-b",
        exclusive_start_key=None,
    )

    call = client.queries[0]
    assert page.index_name == "gsi2"
    assert call["IndexName"] == "gsi2"
    assert call["ScanIndexForward"] is scan_forward
    assert call["ExpressionAttributeNames"] == {"#pk": "gsi2pk"}
    assert call["KeyConditionExpression"] == "#pk = :pk"


def test_detail_query_reads_all_pages_strongly_consistently() -> None:
    cursor = marshal_item({"PK": "RECORD#opaque", "SK": "META"})
    client = FakeDynamo(
        [
            {
                "Items": [marshal_item({"PK": "RECORD#opaque", "SK": "META"})],
                "LastEvaluatedKey": cursor,
            },
            {"Items": [marshal_item({"PK": "RECORD#opaque", "SK": "DECISION"})]},
        ]
    )

    result = reader(client).load_record(record_id="opaque")

    assert len(result) == 2
    assert all(call["ConsistentRead"] is True for call in client.queries)
    assert client.queries[1]["ExclusiveStartKey"] == cursor


@pytest.mark.parametrize("legacy_expiry", (None, 2_000_000_000))
def test_profiles_are_permanent_and_accept_legacy_expiry(legacy_expiry: int | None) -> None:
    profile = marshal_item(
        {
            "PK": "PROFILE#REQUESTER",
            "SK": "requester",
            "schema_version": 1,
            "record_type": "requester_profile",
            "display_name": "Requester",
            "avatar_asset_key": None,
            **({"expiresAt": legacy_expiry} if legacy_expiry is not None else {}),
        }
    )
    client = FakeDynamo(
        [
            {
                "Responses": {"sessions": []},
                "UnprocessedKeys": {
                    "sessions": {
                        "Keys": [marshal_item({"PK": "PROFILE#REQUESTER", "SK": "requester"})]
                    }
                },
            },
            {"Responses": {"sessions": [profile]}},
        ]
    )

    result = reader(client).load_profiles(requester_keys=("requester",))

    assert result["requester"].display_name == "Requester"
    assert len(client.batch_calls) == 2

    duplicate_client = FakeDynamo([{"Responses": {"sessions": [profile, profile]}}])
    with pytest.raises(ReadFailure):
        reader(duplicate_client).load_profiles(requester_keys=("requester",))


def test_presign_rejects_unowned_media_prefix() -> None:
    records = reader(FakeDynamo([]))
    assert records.avatar_url(asset_key="participants/a.webp").startswith("https://")
    with pytest.raises(ReadFailure):
        records.avatar_url(asset_key="private/a.webp")


def test_read_configuration_requires_one_exact_secure_value() -> None:
    repository = ReadConfigurationRepository(
        cast(Any, FakeSsm({"Parameters": [{"Name": "session-key", "Value": "s" * 32}]})),
        "session-key",
    )
    assert repository.load_session_key() == b"s" * 32

    missing = ReadConfigurationRepository(
        cast(Any, FakeSsm({"InvalidParameters": ["session-key"]})),
        "session-key",
    )
    with pytest.raises(AuthFailure):
        missing.load_session_key()
