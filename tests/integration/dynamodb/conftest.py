"""Function-scoped DynamoDB Local tables with production-compatible keys."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import boto3
import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient


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
        aws_secret_access_key="local",  # noqa: S106 - DynamoDB Local requires a dummy signature.
    )


@pytest.fixture
def dynamodb_table(dynamodb_client: DynamoDBClient) -> Iterator[str]:
    table_name = f"shittim-chest-test-{uuid.uuid4().hex}"
    dynamodb_client.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
            {"AttributeName": "gsi2pk", "AttributeType": "S"},
            {"AttributeName": "gsi2sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi2",
                "KeySchema": [
                    {"AttributeName": "gsi2pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi2sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb_client.get_waiter("table_exists").wait(TableName=table_name)
    try:
        yield table_name
    finally:
        dynamodb_client.delete_table(TableName=table_name)
