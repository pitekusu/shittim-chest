"""Stubbed Parameter Store read contracts."""

from __future__ import annotations

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.aws.ssm import SsmParameterReader
from shittim_chest.application.ports import ParameterReadUnavailable

PARAMETER_NAME = "/shittim-chest/production/runtime/v0001"


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_decryption", [True, False])
async def test_reader_uses_the_exact_name_and_decryption_choice(
    with_decryption: bool,
) -> None:
    sdk = client()
    reader = SsmParameterReader(client=sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_parameter",
            {
                "Parameter": {
                    "Name": PARAMETER_NAME,
                    "Type": "SecureString",
                    "Value": "configured-value",
                    "Version": 1,
                    "DataType": "text",
                }
            },
            {"Name": PARAMETER_NAME, "WithDecryption": with_decryption},
        )

        assert (
            await reader.get_parameter(
                PARAMETER_NAME,
                with_decryption=with_decryption,
            )
            == "configured-value"
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_reader_maps_parameter_errors_without_exposing_name_or_value() -> None:
    sdk = client()
    reader = SsmParameterReader(client=sdk)
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "get_parameter",
            service_error_code="ParameterNotFound",
            service_message="secret-like-value",
            http_status_code=400,
            expected_params={"Name": PARAMETER_NAME, "WithDecryption": True},
        )

        with pytest.raises(ParameterReadUnavailable) as caught:
            await reader.get_parameter(PARAMETER_NAME)
        assert str(caught.value) == "parameter_read_unavailable"
        assert PARAMETER_NAME not in str(caught.value)
        assert "secret-like-value" not in str(caught.value)


@pytest.mark.asyncio
async def test_reader_rejects_a_malformed_response() -> None:
    sdk = client()
    reader = SsmParameterReader(client=sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_parameter",
            {},
            {"Name": PARAMETER_NAME, "WithDecryption": True},
        )

        with pytest.raises(ParameterReadUnavailable):
            await reader.get_parameter(PARAMETER_NAME)


@pytest.mark.asyncio
async def test_reader_rejects_an_empty_or_padded_name_before_the_sdk_call() -> None:
    sdk = client()
    reader = SsmParameterReader(client=sdk)

    with pytest.raises(ValueError, match="parameter name"):
        await reader.get_parameter(f" {PARAMETER_NAME}")
