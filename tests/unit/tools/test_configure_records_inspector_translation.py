"""Tests for the private Inspector translation API-key setup."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient
from tools import configure_records_inspector_translation as setup

PRIVATE_KEY = "private-inspector-translation-key"


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name=setup.AWS_REGION,
        config=Config(signature_version=UNSIGNED),
    )


def secret_reader(values: list[str]) -> setup.SecretReader:
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


def test_parameter_name_is_the_exact_private_translation_path() -> None:
    assert setup.parameter_names() == (
        "/shittim-chest/production/records/openai/inspector-translation-api-key",
    )


def test_collects_confirmed_key_without_repr_leak() -> None:
    pending = setup.collect_pending_setup(
        missing_parameters=frozenset(setup.parameter_names()),
        secret_reader=secret_reader([PRIVATE_KEY, PRIVATE_KEY]),
    )

    assert repr(pending) == "PendingSetup()"
    assert pending.parameters == {setup.API_KEY_PARAMETER: PRIVATE_KEY}


@pytest.mark.parametrize("values", [["", ""], [PRIVATE_KEY, "different"]])
def test_rejects_missing_or_mismatched_key(values: list[str]) -> None:
    with pytest.raises(setup.SetupError):
        setup.collect_pending_setup(
            missing_parameters=frozenset(setup.parameter_names()),
            secret_reader=secret_reader(values),
        )


def test_put_never_overwrites_and_returns_only_metadata() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "put_parameter",
            {"Version": 1, "Tier": "Standard"},
            {
                "Name": setup.API_KEY_PARAMETER,
                "Value": PRIVATE_KEY,
                "Description": "Private OpenAI API key for Inspector description translation",
                "Type": "SecureString",
                "Tier": "Standard",
                "Overwrite": False,
                "Tags": [
                    {"Key": "Project", "Value": "shittim-chest"},
                    {"Key": "Environment", "Value": "production"},
                    {"Key": "Component", "Value": "records"},
                ],
            },
        )
        result = setup.put_parameters(sdk, {setup.API_KEY_PARAMETER: PRIVATE_KEY})

    assert result[setup.API_KEY_PARAMETER].version == 1


def test_metadata_check_never_gets_or_decrypts_parameter_values() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_parameters",
            {
                "Parameters": [
                    {
                        "Name": setup.API_KEY_PARAMETER,
                        "Type": "SecureString",
                        "Tier": "Standard",
                        "Version": 2,
                    }
                ]
            },
            {
                "ParameterFilters": [
                    {
                        "Key": "Path",
                        "Option": "Recursive",
                        "Values": [setup.PARAMETER_ROOT],
                    }
                ]
            },
        )
        result = setup.parameter_metadata(sdk)

    assert result[setup.API_KEY_PARAMETER].version == 2
