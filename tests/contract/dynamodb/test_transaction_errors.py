"""Fail-safe classification of DynamoDB transaction cancellations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.adapters.dynamodb.transaction_errors import (
    is_condition_only_cancellation,
)
from shittim_chest.application.ports import RepositoryUnavailable
from tests.contract.dynamodb.test_ingress_sdk_boundary import request

if TYPE_CHECKING:
    from botocore.exceptions import _ClientErrorResponseTypeDef


def client_error(reasons: object = None, *, include_reasons: bool = True) -> ClientError:
    response: dict[str, object] = {
        "Error": {"Code": "TransactionCanceledException", "Message": "redacted"}
    }
    if include_reasons:
        response["CancellationReasons"] = reasons
    return ClientError(
        cast("_ClientErrorResponseTypeDef", response),
        "TransactWriteItems",
    )


@pytest.mark.parametrize(
    "reasons",
    [
        [{"Code": "ConditionalCheckFailed"}],
        [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}],
    ],
)
def test_condition_only_cancellations_are_expected_conflicts(
    reasons: list[Mapping[str, str]],
) -> None:
    assert is_condition_only_cancellation(client_error(reasons))


@pytest.mark.parametrize(
    ("reasons", "include_reasons"),
    [
        ([{"Code": "TransactionConflict"}], True),
        ([{"Code": "ThrottlingError"}], True),
        ([{"Code": "ValidationError"}], True),
        ([{"Code": "Unknown"}], True),
        ([{"Code": 1}], True),
        ([], True),
        (None, False),
    ],
)
def test_other_or_malformed_cancellations_fail_safe_as_unavailable(
    reasons: object,
    include_reasons: bool,
) -> None:
    assert not is_condition_only_cancellation(
        client_error(reasons, include_reasons=include_reasons)
    )


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


@pytest.mark.asyncio
async def test_ingress_transaction_conflict_is_not_returned_as_user_rejection() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message="provider detail must stay private",
            http_status_code=400,
            modeled_fields={"CancellationReasons": [{"Code": "TransactionConflict"}]},
            expected_params={
                "TransactItems": ANY,
                "ClientRequestToken": ANY,
                "ReturnConsumedCapacity": "TOTAL",
            },
        )

        with pytest.raises(RepositoryUnavailable, match=r"^repository_unavailable$"):
            await repository.enqueue(request(1))
        stubber.assert_no_pending_responses()
