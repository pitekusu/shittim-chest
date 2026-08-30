"""DynamoDB Local integration for Records OAuth, Session, and read pagination."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import boto3
import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from tests.factories import NOW, completed_snapshot, presentation

from shittim_records.adapters import ArchiveRepository
from shittim_records.admin import AdminFailure, PromptRevisionSummary
from shittim_records.admin_adapters import DynamoPromptAuditStore
from shittim_records.archive import project_completed_debate
from shittim_records.auth import AuthFailure, OAuthState, SessionRecord
from shittim_records.auth_adapters import DynamoAuthStore
from shittim_records.cost_adapters import DynamoCostLedgerStore
from shittim_records.costs import ProviderDailyCost, ProviderDailyRate
from shittim_records.inspector_translation_adapters import DynamoInspectorTranslationStore
from shittim_records.inspector_translations import (
    InspectorJapaneseSummary,
    inspector_description,
)
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
        source=DynamoRankingSource(
            dynamodb_client,
            archive_table,
            statistics_table,
        ),
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

    description = inspector_description(
        vulnerability_id="CVE-2026-12345",
        description=(
            "A boundary validation flaw can cause the affected process to read outside "
            "its intended memory region."
        ),
    )
    translation = InspectorJapaneseSummary(
        key=description.key,
        vulnerability_id=description.vulnerability_id,
        source_sha256=description.source_sha256,
        summary_ja=(
            "入力値の境界確認が不十分なため、細工されたデータを処理すると、対象プロセスが本来の範囲外にある"
            "メモリを読み取る可能性があります。その結果、処理の異常終了や、プロセス内で扱われる情報の一部が"
            "意図せず露出するおそれがある脆弱性です。"
        ),
        translated_at=NOW,
    )
    translation_store = DynamoInspectorTranslationStore(dynamodb_client, statistics_table)
    translation_store.save((translation,))

    assert translation_store.load((description.key,)) == {description.key: translation}
    cached = dynamodb_client.get_item(
        TableName=statistics_table,
        Key=marshal_item(
            {
                "PK": "ADMIN#INSPECTOR_TRANSLATION",
                "SK": f"SUMMARY#{description.key}",
            }
        ),
        ConsistentRead=True,
    )
    assert description.description not in str(unmarshal_item(cached["Item"]))


def test_admin_prompt_audit_transactions_recovery_and_pagination(
    dynamodb_client: DynamoDBClient,
    table_names: tuple[str, str, str],
) -> None:
    statistics_table = table_names[2]
    store = DynamoPromptAuditStore(dynamodb_client, statistics_table)
    revisions = tuple(f"r01k3gqp6g0000000000000000{index}" for index in range(4))
    active_revision: str | None = None

    for index, revision in enumerate(revisions):
        idempotency_hash = f"{index + 1:x}" * 64
        request_hash = f"{index + 5:x}" * 64
        created_at = datetime(2026, 8, 24, 3, index, tzinfo=UTC)
        operation = store.begin_operation(
            idempotency_hash=idempotency_hash,
            request_hash=request_hash,
            revision=revision,
            created_at=created_at,
            action="publish",
            expected_base_revision=active_revision,
            source_revision=None,
        )
        assert store.get_pending_operation(request_hash) == operation

        if index == 1:
            with pytest.raises(AdminFailure) as conflict:
                store.begin_operation(
                    idempotency_hash="f" * 64,
                    request_hash="e" * 64,
                    revision="r01k3gqp6g00000000000000009",
                    created_at=created_at,
                    action="publish",
                    expected_base_revision=active_revision,
                    source_revision=None,
                )
            assert conflict.value.code == "PROMPT_REVISION_CONFLICT"
            assert store.get_pending_operation(request_hash) == operation

        summary = PromptRevisionSummary(
            revision=revision,
            created_at=created_at,
            action="publish",
            base_revision=active_revision,
            source_revision=None,
            checksum=f"{index + 9:x}" * 64,
        )
        store.complete_operation(operation=operation, summary=summary)
        completed = store.get_operation(idempotency_hash)
        assert completed is not None
        assert completed.complete is True
        assert store.get_summary(revision) == summary
        assert store.get_pending_operation_any() is None
        active_revision = revision

    first_page = store.list_summaries(limit=2, cursor=None)
    assert tuple(item.revision for item in first_page.items) == tuple(reversed(revisions[2:]))
    assert first_page.next_cursor == revisions[2]
    second_page = store.list_summaries(limit=2, cursor=first_page.next_cursor)
    assert tuple(item.revision for item in second_page.items) == tuple(reversed(revisions[:2]))
    if second_page.next_cursor is not None:
        assert store.list_summaries(limit=2, cursor=second_page.next_cursor).items == ()


class _NoopS3:
    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://media.example.invalid/signed"
