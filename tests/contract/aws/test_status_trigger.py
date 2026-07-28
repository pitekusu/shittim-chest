"""Stubbed asynchronous status-publisher invocation contracts."""

from __future__ import annotations

import json

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_lambda.client import LambdaClient

from shittim_chest.adapters.aws.status_trigger import (
    LambdaRuntimeReconciliationTrigger,
    LambdaStatusPublicationTrigger,
)
from shittim_chest.application.ports import (
    ReconciliationTriggerUnavailable,
    StatusTriggerUnavailable,
)

FUNCTION_NAME = "status-publisher"
INTERACTION_ID = "123"
PAYLOAD = b'{"schema_version":1,"interaction_id":"123"}'


def client() -> LambdaClient:
    return boto3.client(
        "lambda",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


@pytest.mark.asyncio
async def test_trigger_sends_only_the_canonical_interaction_identifier() -> None:
    sdk = client()
    trigger = LambdaStatusPublicationTrigger(client=sdk, function_name=FUNCTION_NAME)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "invoke",
            {"StatusCode": 202},
            {
                "FunctionName": FUNCTION_NAME,
                "InvocationType": "Event",
                "Payload": PAYLOAD,
            },
        )

        await trigger.request_publication(INTERACTION_ID)
        stubber.assert_no_pending_responses()

    event = json.loads(PAYLOAD)
    assert event == {"schema_version": 1, "interaction_id": INTERACTION_ID}
    serialized = PAYLOAD.decode("utf-8")
    for forbidden in ("question", "requester", "token", "username", "display_name"):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"StatusCode": 200}, {"StatusCode": 204}])
async def test_trigger_requires_the_async_invoke_202_response(
    response: dict[str, object],
) -> None:
    sdk = client()
    trigger = LambdaStatusPublicationTrigger(client=sdk, function_name=FUNCTION_NAME)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "invoke",
            response,
            {
                "FunctionName": FUNCTION_NAME,
                "InvocationType": "Event",
                "Payload": PAYLOAD,
            },
        )

        with pytest.raises(StatusTriggerUnavailable):
            await trigger.request_publication(INTERACTION_ID)


@pytest.mark.asyncio
async def test_trigger_maps_throttling_without_exposing_provider_content() -> None:
    sdk = client()
    trigger = LambdaStatusPublicationTrigger(client=sdk, function_name=FUNCTION_NAME)
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "invoke",
            service_error_code="TooManyRequestsException",
            service_message="sensitive invocation detail",
            http_status_code=429,
            expected_params={
                "FunctionName": FUNCTION_NAME,
                "InvocationType": "Event",
                "Payload": PAYLOAD,
            },
        )

        with pytest.raises(StatusTriggerUnavailable) as caught:
            await trigger.request_publication(INTERACTION_ID)
        assert str(caught.value) == "status_trigger_unavailable"
        assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interaction_id",
    ["", "0", "01", "-1", str(2**64), "not-a-snowflake"],
)
async def test_trigger_rejects_noncanonical_interaction_identifiers(
    interaction_id: str,
) -> None:
    sdk = client()
    trigger = LambdaStatusPublicationTrigger(client=sdk, function_name=FUNCTION_NAME)

    with pytest.raises(ValueError, match="canonical"):
        await trigger.request_publication(interaction_id)


@pytest.mark.asyncio
async def test_reconciler_trigger_uses_the_same_content_free_event_contract() -> None:
    sdk = client()
    trigger = LambdaRuntimeReconciliationTrigger(
        client=sdk,
        function_name="runtime-reconciler",
    )
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "invoke",
            {"StatusCode": 202},
            {
                "FunctionName": "runtime-reconciler",
                "InvocationType": "Event",
                "Payload": PAYLOAD,
            },
        )

        await trigger.request_reconciliation(INTERACTION_ID)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_reconciler_trigger_maps_non_202_to_content_free_failure() -> None:
    sdk = client()
    trigger = LambdaRuntimeReconciliationTrigger(
        client=sdk,
        function_name="runtime-reconciler",
    )
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "invoke",
            {"StatusCode": 200},
            {
                "FunctionName": "runtime-reconciler",
                "InvocationType": "Event",
                "Payload": PAYLOAD,
            },
        )

        with pytest.raises(
            ReconciliationTriggerUnavailable,
            match=r"^reconciliation_trigger_unavailable$",
        ):
            await trigger.request_reconciliation(INTERACTION_ID)
