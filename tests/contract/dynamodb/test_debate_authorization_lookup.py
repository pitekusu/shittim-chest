"""Strong, narrow DynamoDB reads for HTTP component authorization."""

from __future__ import annotations

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.debate_lookup import (
    DynamoDbDebateAuthorizationLookup,
)
from shittim_chest.adapters.dynamodb.serializer import CURRENT_SCHEMA_VERSION, DynamoItem
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.domain import AttemptId, DebateId, DebatePhase


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def meta(debate_id: DebateId, attempt_id: AttemptId) -> DynamoItem:
    return {
        "PK": f"DEBATE#{debate_id}",
        "SK": "META",
        "record_type": "debate_meta",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "debate_id": str(debate_id),
        "current_attempt_id": str(attempt_id),
        "current_phase": DebatePhase.DISCUSSING.value,
        "requester_id": "requester-id",
        "guild_id": "guild-id",
        "channel_id": "channel-id",
        "thread_id": "thread-id",
        "control_panel_message_id": "panel-id",
        "question": "this field must not cross the authorization port",
    }


def attempt_meta(debate_id: DebateId, attempt_id: AttemptId) -> DynamoItem:
    return {
        "PK": f"DEBATE#{debate_id}",
        "SK": f"ATTEMPT#{attempt_id}#META",
        "record_type": "attempt_meta",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "attempt_id": str(attempt_id),
        "phase": DebatePhase.FAILED.value,
    }


def expected_transaction(debate_id: DebateId, attempt_id: AttemptId) -> dict[str, object]:
    partition_key = f"DEBATE#{debate_id}"
    return {
        "TransactItems": [
            {
                "Get": {
                    "TableName": "test-table",
                    "Key": marshal_item({"PK": partition_key, "SK": "META"}),
                }
            },
            {
                "Get": {
                    "TableName": "test-table",
                    "Key": marshal_item(
                        {
                            "PK": partition_key,
                            "SK": f"ATTEMPT#{attempt_id}#META",
                        }
                    ),
                }
            },
        ],
        "ReturnConsumedCapacity": "NONE",
    }


@pytest.mark.asyncio
async def test_lookup_reads_only_current_debate_meta() -> None:
    sdk = client()
    lookup = DynamoDbDebateAuthorizationLookup(client=sdk, table_name="test-table")
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {"Responses": [{"Item": marshal_item(meta(debate_id, attempt_id))}, {}]},
            expected_transaction(debate_id, attempt_id),
        )

        result = await lookup.get(debate_id, attempt_id)
        stubber.assert_no_pending_responses()

    assert result is not None
    assert result.debate_id == debate_id
    assert result.attempt_id == attempt_id
    assert result.phase is DebatePhase.DISCUSSING
    assert result.requester_id == "requester-id"
    assert not hasattr(result, "question")


@pytest.mark.asyncio
async def test_lookup_reads_legacy_attempt_phase_in_the_same_transaction() -> None:
    sdk = client()
    lookup = DynamoDbDebateAuthorizationLookup(client=sdk, table_name="test-table")
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    legacy_meta = meta(debate_id, attempt_id)
    del legacy_meta["current_phase"]
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "transact_get_items",
            {
                "Responses": [
                    {"Item": marshal_item(legacy_meta)},
                    {"Item": marshal_item(attempt_meta(debate_id, attempt_id))},
                ]
            },
            expected_transaction(debate_id, attempt_id),
        )

        result = await lookup.get(debate_id, attempt_id)
        stubber.assert_no_pending_responses()

    assert result is not None
    assert result.phase is DebatePhase.FAILED


@pytest.mark.asyncio
async def test_lookup_maps_provider_failure_without_provider_text() -> None:
    sdk = client()
    lookup = DynamoDbDebateAuthorizationLookup(client=sdk, table_name="test-table")
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "transact_get_items",
            service_error_code="InternalServerError",
            service_message="sensitive provider detail",
            http_status_code=500,
            expected_params=expected_transaction(debate_id, attempt_id),
        )

        with pytest.raises(RepositoryUnavailable, match=r"^repository_unavailable$") as caught:
            await lookup.get(debate_id, attempt_id)
        assert "sensitive" not in str(caught.value)
