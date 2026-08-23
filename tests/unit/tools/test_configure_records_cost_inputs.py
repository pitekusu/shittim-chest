"""Tests for the private Records cost input setup."""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient
from tools import configure_records_cost_inputs as setup


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name=setup.AWS_REGION,
        config=Config(signature_version=UNSIGNED),
    )


def secret_reader(values: list[str]) -> setup.SecretReader:
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


def test_parameter_names_are_the_exact_two_secure_string_paths() -> None:
    assert setup.parameter_names() == (
        "/shittim-chest/production/records/openai/admin-key",
        "/shittim-chest/production/records/openai/project-id",
    )


def test_collects_only_missing_values_without_repr_leak() -> None:
    pending = setup.collect_pending_setup(
        missing_parameters=frozenset(setup.parameter_names()),
        secret_reader=secret_reader(["admin-private", "project-private"]),
    )

    assert repr(pending) == "PendingSetup()"
    assert pending.parameters == {
        setup.ADMIN_KEY_PARAMETER: "admin-private",
        setup.PROJECT_ID_PARAMETER: "project-private",
    }


def test_rejects_empty_and_unknown_inputs() -> None:
    with pytest.raises(setup.SetupError, match="private_value_missing"):
        setup.collect_pending_setup(
            missing_parameters=frozenset({setup.ADMIN_KEY_PARAMETER}),
            secret_reader=secret_reader(["  "]),
        )
    with pytest.raises(setup.SetupError, match="unexpected_parameter_requested"):
        setup.collect_pending_setup(missing_parameters=frozenset({"/unexpected"}))


def test_put_parameters_never_overwrites_and_applies_exact_tags() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "put_parameter",
            {"Version": 1, "Tier": "Standard"},
            {
                "Name": setup.ADMIN_KEY_PARAMETER,
                "Value": "private-value",
                "Description": "Private cost input for Shittim Chest Records",
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
        metadata = setup.put_parameters(sdk, {setup.ADMIN_KEY_PARAMETER: "private-value"})

    assert metadata == {
        setup.ADMIN_KEY_PARAMETER: setup.ParameterMetadata(
            name=setup.ADMIN_KEY_PARAMETER,
            type="SecureString",
            tier="Standard",
            version=1,
        )
    }


def test_metadata_check_never_requests_parameter_values() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_parameters",
            {
                "Parameters": [
                    {
                        "Name": setup.ADMIN_KEY_PARAMETER,
                        "Type": "SecureString",
                        "Tier": "Standard",
                        "Version": 2,
                    },
                    {
                        "Name": setup.PROJECT_ID_PARAMETER,
                        "Type": "SecureString",
                        "Tier": "Standard",
                        "Version": 1,
                    },
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
        metadata = setup.parameter_metadata(sdk)

    assert metadata[setup.ADMIN_KEY_PARAMETER].version == 2
    assert metadata[setup.PROJECT_ID_PARAMETER].tier == "Standard"


def test_rejects_existing_parameter_with_unsafe_metadata() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_parameters",
            {
                "Parameters": [
                    {
                        "Name": setup.ADMIN_KEY_PARAMETER,
                        "Type": "String",
                        "Tier": "Standard",
                        "Version": 1,
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
        with pytest.raises(setup.SetupError, match="existing_parameter_is_not_secure_string"):
            setup.parameter_metadata(sdk)
