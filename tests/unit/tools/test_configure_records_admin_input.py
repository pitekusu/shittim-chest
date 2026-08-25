"""Tests for the private Records administrator input setup."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient
from tools import configure_records_admin_input as setup

VALID_DISCORD_ID = "123456789" + "012345678"


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name=setup.AWS_REGION,
        config=Config(signature_version=UNSIGNED),
    )


def secret_reader(values: list[str]) -> setup.SecretReader:
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


def test_parameter_name_is_the_exact_private_admin_path() -> None:
    assert setup.parameter_names() == ("/shittim-chest/production/records/admin/discord-user-id",)


def test_collects_valid_discord_id_without_repr_leak() -> None:
    pending = setup.collect_pending_setup(
        missing_parameters=frozenset(setup.parameter_names()),
        secret_reader=secret_reader([VALID_DISCORD_ID, VALID_DISCORD_ID]),
    )

    assert repr(pending) == "PendingSetup()"
    assert pending.parameters == {setup.ADMIN_DISCORD_ID_PARAMETER: VALID_DISCORD_ID}


@pytest.mark.parametrize("value", ["", "eel.type.power", "123", "1" * 21])
def test_rejects_non_snowflake_admin_identity(value: str) -> None:
    with pytest.raises(setup.SetupError, match="discord_identifier_invalid"):
        setup.collect_pending_setup(
            missing_parameters=frozenset(setup.parameter_names()),
            secret_reader=secret_reader([value]),
        )


def test_rejects_mismatched_confirmation_without_returning_private_input() -> None:
    with pytest.raises(setup.SetupError) as caught:
        setup.collect_pending_setup(
            missing_parameters=frozenset(setup.parameter_names()),
            secret_reader=secret_reader([VALID_DISCORD_ID, "9" * 17]),
        )

    assert caught.value.code == "discord_identifier_confirmation_mismatch"


def test_rejects_unknown_parameter_request() -> None:
    with pytest.raises(setup.SetupError, match="unexpected_parameter_requested"):
        setup.collect_pending_setup(missing_parameters=frozenset({"/unexpected"}))


def test_put_never_overwrites_and_returns_only_metadata() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "put_parameter",
            {"Version": 1, "Tier": "Standard"},
            {
                "Name": setup.ADMIN_DISCORD_ID_PARAMETER,
                "Value": VALID_DISCORD_ID,
                "Description": "Private administrator identity for Shittim Chest Records",
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
        result = setup.put_parameters(
            sdk,
            {setup.ADMIN_DISCORD_ID_PARAMETER: VALID_DISCORD_ID},
        )

    assert result[setup.ADMIN_DISCORD_ID_PARAMETER].version == 1


def test_metadata_check_never_gets_or_decrypts_parameter_values() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_parameters",
            {
                "Parameters": [
                    {
                        "Name": setup.ADMIN_DISCORD_ID_PARAMETER,
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

    assert result[setup.ADMIN_DISCORD_ID_PARAMETER].version == 2
