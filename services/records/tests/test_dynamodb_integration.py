"""DynamoDB Local integration for Records OAuth, Session, and read pagination."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import boto3
import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.adapters import ArchiveRepository
from shittim_records.archive import project_completed_debate
from shittim_records.auth import AuthFailure, OAuthState, SessionRecord
from shittim_records.auth_adapters import DynamoAuthStore
from shittim_records.cost_adapters import DynamoCostLedgerStore
from shittim_records.costs import ProviderDailyCost, ProviderDailyRate
from shittim_records.ranking_adapters import DynamoRankingSnapshotStore, DynamoRankingSource
from shittim_records.rankings import RankingService
from shittim_records.read_adapters import DynamoRecordsReader


@pytest.fixture
def dynamodb_client() -> DynamoDBClient:
    endpoint = os.environ.get("DYNAMODB_ENDPOINT_URL")
    if endpoint is None:
        pytest.skip("DYNAMODB_ENDPOINT_URL is required for DynamoDB Local tests")
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        endpoint_url=endpoint,
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - DynamoDB Local dummy credential.
    )


@pytest.fixture
def table_names(dynamodb_client: DynamoDBClient) -> Iterator[tuple[str, str, str]]:
    suffix = uuid.uuid4().hex
    session_table = f"records-session-{suffix}"
    archive_table = f"records-archive-{suffix}"
    statistics_table = f"records-statistics-{suffix}"
    dynamodb_client.create_table(
        TableName=session_table,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.create_table(
        TableName=archive_table,
        AttributeDefinitions=[
            {"AttributeName": name, "AttributeType": "S"}
            for name in ("PK", "SK", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk")
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": index,
                "KeySchema": [
                    {"AttributeName": f"{index}pk", "KeyType": "HASH"},
                    {"AttributeName": f"{index}sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for index in ("gsi1", "gsi2")
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.create_table(
        TableName=statistics_table,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = dynamodb_client.get_waiter("table_exists")
    waiter.wait(TableName=session_table)
    waiter.wait(TableName=archive_table)
    waiter.wait(TableName=statistics_table)
    try:
        yield session_table, archive_table, statistics_table
    finally:
        dynamodb_client.delete_table(TableName=session_table)
        dynamodb_client.delete_table(TableName=archive_table)
        dynamodb_client.delete_table(TableName=statistics_table)


def test_oauth_claim_session_and_archive_pagination(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
) -> None:
    session_table, archive_table, statistics_table = table_names
    store = DynamoAuthStore(dynamodb_client, session_table)
    store.create_oauth_state(
        state_hash="state-hash",
        state=OAuthState(nonce_hash="nonce-hash", return_to="/", expires_at=200),
    )
    claimed = store.claim_oauth_state(
        state_hash="state-hash",
        nonce_hash="nonce-hash",
        now_epoch=100,
        claimed_at="2026-08-17T00:00:00+00:00",
    )
    assert claimed.return_to == "/"
    with pytest.raises(AuthFailure):
        store.claim_oauth_state(
            state_hash="state-hash",
            nonce_hash="nonce-hash",
            now_epoch=100,
            claimed_at="2026-08-17T00:00:01+00:00",
        )

    session = SessionRecord(
        requester_key="requester",
        display_name="Requester",
        avatar_asset_key=None,
        csrf_hash="csrf-hash",
        guild_verified_at="2026-08-17T00:00:00+00:00",
        expires_at=200,
    )
    store.create_session(session_hash="session-hash", session=session)
    assert store.get_session(session_hash="session-hash") == session
    profile_response = dynamodb_client.get_item(
        TableName=session_table,
        Key=marshal_item({"PK": "PROFILE#REQUESTER", "SK": "requester"}),
        ConsistentRead=True,
    )
    profile = unmarshal_item(profile_response["Item"])
    assert profile["display_name"] == "Requester"
    assert "expiresAt" not in profile
    store.delete_session(session_hash="session-hash")
    assert store.get_session(session_hash="session-hash") is None

    projection = project_completed_debate(
        completed_snapshot(),
        identity_hmac_key=b"records-test-key-that-is-longer-than-32-bytes",
        presentation=presentation(),
        projected_at=NOW,
    )
    assert ArchiveRepository(dynamodb_client, archive_table).put_projection(projection) is True
    assert ArchiveRepository(dynamodb_client, archive_table).put_projection(projection) is False
    reader = DynamoRecordsReader(
        dynamodb_client,
        cast(Any, _NoopS3()),
        archive_table_name=archive_table,
        statistics_table_name=statistics_table,
        session_table_name=session_table,
        media_bucket_name="media",
    )
    for _attempt in range(20):
        page = reader.list_meta(
            limit=1,
            sort="newest",
            winner=None,
            exclusive_start_key=None,
        )
        if page.items:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("DynamoDB Local GSI did not expose the Archive record")
    assert len(page.items) == 1
    assert page.items[0]["record_id"] == projection.record_id
    assert len(reader.load_record(record_id=projection.record_id)) == 12

    ranking = RankingService(
        source=DynamoRankingSource(dynamodb_client, archive_table),
        store=DynamoRankingSnapshotStore(dynamodb_client, statistics_table),
    ).refresh(now=NOW)
    assert ranking.archive_count == 1
    assert sum(entry.count for entry in ranking.wins) == 1
    assert len(reader.load_ranking_snapshots()) == 2

    cost_start = date(2026, 7, 25)
    cost_end = cost_start + timedelta(days=30)
    components = tuple(
        (name, Decimal("1") if name == "residual" else Decimal("0"))
        for name in (
            "cloudwatch",
            "public_ipv4",
            "dynamodb",
            "s3",
            "cloudfront",
            "api_gateway",
            "ecr",
            "inspector",
            "residual",
        )
    )
    cost_store = DynamoCostLedgerStore(dynamodb_client, statistics_table)
    cost_store.save_cost_window(
        source="AWS",
        costs=tuple(
            ProviderDailyCost(
                cost_date=cost_start + timedelta(days=index),
                category=category,
                amount_usd=Decimal("1"),
                estimated=False,
                components=components if category == "OTHER_AWS" else (),
            )
            for index in range(30)
            for category in ("FARGATE", "LAMBDA", "OTHER_AWS")
        ),
        next_date=cost_end,
        initial_complete=False,
        collected_at=NOW,
    )
    cost_store.save_rate_window(
        rates=tuple(
            ProviderDailyRate(cost_start + timedelta(days=index), Decimal("150"))
            for index in range(30)
        ),
        next_date=cost_end,
        initial_complete=False,
        collected_at=NOW,
    )
    checkpoint = cost_store.load_checkpoint("AWS")
    assert checkpoint is not None
    assert checkpoint.next_date == cost_end
    stored_costs, stored_rates = reader.load_cost_ledger()
    assert len(stored_costs) == 90
    assert len(stored_rates) == 30


class _NoopS3:
    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://media.example.invalid/signed"
