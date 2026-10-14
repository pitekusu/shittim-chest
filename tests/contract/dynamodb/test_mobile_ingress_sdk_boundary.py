"""Mobile replay must not join receipt records from different phase snapshots."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from discord.utils import time_snowflake
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_ingress_status_publication,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.control_records import CONTROL_RECORD_MANIFEST
from shittim_chest.adapters.dynamodb.serializer import CURRENT_SCHEMA_VERSION, DynamoItem
from shittim_chest.application import (
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    IngressStatusPublication,
    StatusMessageState,
)
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.status_publication import render_public_status
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 10, 4, tzinfo=UTC)
OWNER = "a" * 43


def mobile_request(index: int, *, owner_key: str = OWNER) -> IngressRequest:
    return IngressRequest.mobile_debate(
        request_id=f"00000000-0000-4000-8000-a{index:011x}",
        owner_key=owner_key,
        application_id="application-id",
        question="Fictional question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        created_at=NOW + timedelta(microseconds=index),
    )


def mobile_pointer(request: IngressRequest, *, schema_version: int) -> DynamoItem:
    timestamp = request.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "PK": f"MOBILE_INGRESS#{request.owner_key}",
        "SK": f"REQUEST#{timestamp}#{request.mobile_request_id}",
        "record_type": "mobile_ingress_owner_pointer",
        "schema_version": schema_version,
        "record_schema_version": 1,
        "mobile_request_id": request.mobile_request_id,
        "request_sort_key": ingress_request_sort_key(request),
    }


def inventory_query() -> dict[str, object]:
    return {
        "TableName": "test-table",
        "KeyConditionExpression": "PK=:pk AND SK>=:since",
        "ExpressionAttributeValues": marshal_item(
            {":pk": f"MOBILE_INGRESS#{OWNER}", ":since": "REQUEST#2026-10-04T00:00:00.000000Z#"}
        ),
        "ScanIndexForward": False,
        "ConsistentRead": True,
        "Limit": 2,
    }


def expect_request(stubber: Stubber, request: IngressRequest, *, schema_version: int) -> None:
    item = {**serialize_ingress_request(request), "schema_version": schema_version}
    stubber.add_response(
        "get_item",
        {"Item": marshal_item(item)},
        {
            "TableName": "test-table",
            "Key": marshal_item({"PK": "CONTROL#INGRESS", "SK": ingress_request_sort_key(request)}),
            "ConsistentRead": True,
        },
    )


def client() -> DynamoDBClient:
    return boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


@pytest.mark.asyncio
async def test_mobile_inventory_lists_old_and_current_receipts_across_pages() -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    first, second, third = (mobile_request(index) for index in (1, 2, 3))
    old_pointer = mobile_pointer(second, schema_version=10)
    current_pointer = mobile_pointer(third, schema_version=CURRENT_SCHEMA_VERSION)
    cursor = {"PK": old_pointer["PK"], "SK": old_pointer["SK"]}

    with Stubber(sdk) as stubber:
        stubber.add_response(
            "query",
            {
                "Items": [marshal_item(current_pointer), marshal_item(old_pointer)],
                "LastEvaluatedKey": marshal_item(cursor),
            },
            inventory_query(),
        )
        expect_request(stubber, third, schema_version=CURRENT_SCHEMA_VERSION)
        # Request progress can advance under the new schema while its immutable
        # owner pointer keeps the schema used when the request was admitted.
        expect_request(stubber, second, schema_version=CURRENT_SCHEMA_VERSION)
        stubber.add_response(
            "query",
            {"Items": [marshal_item(mobile_pointer(first, schema_version=10))]},
            {**inventory_query(), "ExclusiveStartKey": marshal_item(cursor)},
        )
        expect_request(stubber, first, schema_version=10)

        page = await repository.list_mobile_requests(owner_key=OWNER, since=NOW, limit=2)
        assert page.requests == (third, second)
        assert page.next_created_at == second.created_at
        assert page.next_request_id == second.mobile_request_id
        remainder = await repository.list_mobile_requests(
            owner_key=OWNER,
            since=NOW,
            limit=2,
            cursor_created_at=page.next_created_at,
            cursor_request_id=page.next_request_id,
        )
        assert remainder.requests == (first,)
        assert remainder.next_created_at is None
        assert remainder.next_request_id is None
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 7),
        ("schema_version", CURRENT_SCHEMA_VERSION + 1),
        ("schema_version", True),
        ("record_schema_version", 2),
        ("record_schema_version", True),
    ],
)
async def test_mobile_inventory_rejects_unsupported_pointer_schemas(field: str, value: int) -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    pointer = {**mobile_pointer(mobile_request(1), schema_version=10), field: value}

    with Stubber(sdk) as stubber:
        stubber.add_response("query", {"Items": [marshal_item(pointer)]}, inventory_query())
        with pytest.raises(RepositoryConflict):
            await repository.list_mobile_requests(owner_key=OWNER, since=NOW, limit=2)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("tampering", ["request_owner", "public_request_id", "sort_key"])
async def test_mobile_inventory_rejects_tampered_legacy_pointer_identity(tampering: str) -> None:
    sdk = client()
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    source = mobile_request(1, owner_key="b" * 43 if tampering == "request_owner" else OWNER)
    pointer = mobile_pointer(source, schema_version=10)
    pointer["PK"] = f"MOBILE_INGRESS#{OWNER}"
    if tampering == "public_request_id":
        pointer["mobile_request_id"] = mobile_request(2).mobile_request_id
    elif tampering == "sort_key":
        pointer["SK"] = mobile_pointer(mobile_request(2), schema_version=10)["SK"]

    with Stubber(sdk) as stubber:
        stubber.add_response("query", {"Items": [marshal_item(pointer)]}, inventory_query())
        expect_request(stubber, source, schema_version=10)
        with pytest.raises(RepositoryConflict, match="pointer identity is inconsistent"):
            await repository.list_mobile_requests(owner_key=OWNER, since=NOW, limit=2)
        stubber.assert_no_pending_responses()


def operation(request: IngressRequest) -> IngressOperationResult:
    return IngressOperationResult(
        operation_id=request.operation_id,
        interaction_id=request.interaction_id,
        request_sort_key=ingress_request_sort_key(request),
        status=request.status,
        created_at=request.created_at,
        updated_at=request.updated_at,
        accepted_debate_id=request.accepted_debate_id,
        accepted_attempt_id=request.accepted_attempt_id,
        error_code=request.error_code,
    )


def expect_transaction(stubber: Stubber, items: tuple[DynamoItem, ...]) -> None:
    stubber.add_response(
        "transact_get_items",
        {"Responses": [{"Item": marshal_item(item)} for item in items]},
        {
            "TransactItems": [
                {
                    "Get": {
                        "TableName": "test-table",
                        "Key": marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                    }
                }
                for item in items
            ],
            "ReturnConsumedCapacity": "NONE",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admission_result", ["collision", "idempotent_response"])
async def test_mobile_retry_reads_one_bundle_after_concurrent_acceptance(
    admission_result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )
    repository = DynamoDbIngressRepository(client=sdk, table_name="test-table")
    source = IngressRequest.mobile_debate(
        request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        owner_key="a" * 43,
        application_id="application-id",
        question="Fictional question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        created_at=NOW,
    )
    source = replace(
        source,
        history_after_snowflake=str(time_snowflake(source.created_at - timedelta(seconds=1))),
    )
    accepted = replace(
        source,
        status=IngressStatus.ACCEPTED,
        status_message_state=StatusMessageState.ACCEPTED,
        updated_at=NOW + timedelta(seconds=1),
        accepted_debate_id=DebateId.new(),
        accepted_attempt_id=AttemptId.new(),
    )
    old_operation = serialize_ingress_operation_result(operation(source))
    new_operation = operation(accepted)
    publication = replace(
        IngressStatusPublication.prepared(
            source, content=render_public_status(accepted, accepted.status_message_state)
        ),
        desired_state=accepted.status_message_state,
        updated_at=accepted.updated_at,
        next_attempt_at=accepted.updated_at,
    )

    def admission_response(*_args: object, **_kwargs: object) -> bool:
        if admission_result == "collision":
            raise RepositoryConflict("existing mobile admission")
        return False

    monkeypatch.setattr(repository, "_transact", admission_response)
    with Stubber(sdk) as stubber:
        if admission_result == "collision":
            counter = next(
                spec.install_item
                for spec in CONTROL_RECORD_MANIFEST.activity_records
                if spec.record_type == "ingress_queue_counter"
            )
            expect_transaction(stubber, (old_operation, {**counter, "count": 1}))
        # The initial operation read locates the immutable request key. Admission
        # advances after that read, so the returned receipt must use the new bundle.
        stubber.add_response(
            "get_item",
            {"Item": marshal_item(old_operation)},
            {
                "TableName": "test-table",
                "Key": marshal_item({"PK": old_operation["PK"], "SK": old_operation["SK"]}),
                "ConsistentRead": True,
            },
        )
        expect_transaction(
            stubber,
            (
                serialize_ingress_operation_result(new_operation),
                serialize_ingress_request(accepted),
                serialize_ingress_status_publication(publication),
            ),
        )

        replay = await repository.enqueue_mobile(source)
        stubber.assert_no_pending_responses()

    assert not replay.created
    assert replay.request == accepted
    assert replay.operation == new_operation
