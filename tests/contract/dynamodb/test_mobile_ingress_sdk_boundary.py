"""Mobile replay must not join receipt records from different phase snapshots."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber

from shittim_chest.adapters.dynamodb import (
    DynamoDbIngressRepository,
    ingress_request_sort_key,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_ingress_status_publication,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.adapters.dynamodb.control_records import CONTROL_RECORD_MANIFEST
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
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
