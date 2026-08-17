"""Tests for the private Records authentication input setup."""

from __future__ import annotations

import json
from collections.abc import Iterator

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient
from tools import configure_records_auth_inputs as setup

CLIENT_ID = "1" * 18
GUILD_ID = "2" * 18


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name=setup.AWS_REGION,
        config=Config(signature_version=UNSIGNED),
    )


def secret_reader(values: list[str]) -> setup.SecretReader:
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


def test_parameter_names_are_the_exact_three_secure_string_paths() -> None:
    assert setup.parameter_names() == (
        "/shittim-chest/production/records/discord/oauth/v0001",
        "/shittim-chest/production/records/discord/client-secret",
        "/shittim-chest/production/records/session-key",
    )


def test_collects_validated_inputs_without_repr_leak() -> None:
    pending = setup.collect_pending_setup(
        missing_parameters=frozenset(setup.parameter_names()),
        secret_reader=secret_reader(
            [
                CLIENT_ID,
                GUILD_ID,
                "https://records.example.invalid/",
                "oauth-client-secret",
            ]
        ),
        session_key_factory=lambda: "s" * 43,
    )

    assert repr(pending) == "PendingSetup()"
    assert pending.callback_url == ("https://records.example.invalid/api/v1/auth/discord/callback")
    oauth = json.loads(pending.parameters[setup.OAUTH_CONFIG_PARAMETER])
    assert oauth == {
        "allowed_origin": "https://records.example.invalid",
        "client_id": CLIENT_ID,
        "guild_id": GUILD_ID,
        "oauth_callback_url": pending.callback_url,
        "schema_version": 1,
    }
    assert pending.parameters[setup.CLIENT_SECRET_PARAMETER] == "oauth-client-secret"
    assert pending.parameters[setup.SESSION_KEY_PARAMETER] == "s" * 43


def test_generates_only_the_missing_session_key_without_prompting() -> None:
    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("an existing private input must not be requested")

    pending = setup.collect_pending_setup(
        missing_parameters=frozenset({setup.SESSION_KEY_PARAMETER}),
        secret_reader=unexpected_prompt,
        session_key_factory=lambda: "k" * 43,
    )

    assert pending.parameters == {setup.SESSION_KEY_PARAMETER: "k" * 43}
    assert pending.callback_url == ""


@pytest.mark.parametrize(
    "values,code",
    [
        (
            ["not-a-discord-id", GUILD_ID, "https://records.example.invalid"],
            "discord_identifier_invalid",
        ),
        (
            [CLIENT_ID, GUILD_ID, "http://records.example.invalid"],
            "records_origin_invalid",
        ),
        (
            [
                CLIENT_ID,
                GUILD_ID,
                "https://user" + "@records.example.invalid",
            ],
            "records_origin_invalid",
        ),
    ],
)
def test_rejects_invalid_oauth_configuration(values: list[str], code: str) -> None:
    with pytest.raises(setup.SetupError) as caught:
        setup.collect_pending_setup(
            missing_parameters=frozenset({setup.OAUTH_CONFIG_PARAMETER}),
            secret_reader=secret_reader(values),
        )

    assert caught.value.code == code


def test_rejects_unknown_parameter_request() -> None:
    with pytest.raises(setup.SetupError) as caught:
        setup.collect_pending_setup(missing_parameters=frozenset({"/unexpected"}))

    assert caught.value.code == "unexpected_parameter_requested"


def test_put_parameters_never_overwrites_and_applies_exact_tags() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "put_parameter",
            {"Version": 1, "Tier": "Standard"},
            {
                "Name": setup.CLIENT_SECRET_PARAMETER,
                "Value": "private-value",
                "Description": "Private authentication input for Shittim Chest Records",
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
        setup.put_parameters(sdk, {setup.CLIENT_SECRET_PARAMETER: "private-value"})


def test_session_key_factory_must_return_at_least_32_bytes_base64url_length() -> None:
    with pytest.raises(setup.SetupError) as caught:
        setup.collect_pending_setup(
            missing_parameters=frozenset({setup.SESSION_KEY_PARAMETER}),
            session_key_factory=lambda: "short",
        )

    assert caught.value.code == "session_key_generation_failed"
