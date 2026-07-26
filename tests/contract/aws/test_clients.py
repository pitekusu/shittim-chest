"""Contract checks for bounded ingress AWS client configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, cast

import pytest
from botocore.config import Config

from shittim_chest.adapters.aws.clients import (
    DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS,
    INGRESS_CONNECT_TIMEOUT_SECONDS,
    INGRESS_MAX_SERIAL_SDK_ROUNDS,
    INGRESS_READ_TIMEOUT_SECONDS,
    INGRESS_RESPONSE_MARGIN_SECONDS,
    INGRESS_TOTAL_MAX_ATTEMPTS,
    STATUS_CONNECT_TIMEOUT_SECONDS,
    STATUS_READ_TIMEOUT_SECONDS,
    STATUS_TOTAL_MAX_ATTEMPTS,
    create_ingress_dynamodb_client,
    create_lambda_client,
    create_ssm_client,
    create_status_dynamodb_client,
    create_status_ssm_client,
    ingress_sdk_config,
    status_sdk_config,
)
from shittim_chest.config.models import DEFAULT_AWS_REGION


class _ConfigView(Protocol):
    connect_timeout: float
    read_timeout: float
    retries: Mapping[str, str | int]
    tcp_keepalive: bool


class _ClientFactory(Protocol):
    def __call__(self, *, region_name: str) -> object: ...


def config_view(config: Config) -> _ConfigView:
    return cast(_ConfigView, config)


def test_ingress_sdk_config_has_one_short_attempt() -> None:
    config = config_view(ingress_sdk_config())

    assert (
        INGRESS_MAX_SERIAL_SDK_ROUNDS
        * (INGRESS_CONNECT_TIMEOUT_SECONDS + INGRESS_READ_TIMEOUT_SECONDS)
        + INGRESS_RESPONSE_MARGIN_SECONDS
        < DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS
    )
    assert config.connect_timeout == INGRESS_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == INGRESS_READ_TIMEOUT_SECONDS
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": INGRESS_TOTAL_MAX_ATTEMPTS,
    }
    assert INGRESS_TOTAL_MAX_ATTEMPTS == 1
    assert config.tcp_keepalive is True


def test_status_sdk_config_has_bounded_standard_retries() -> None:
    config = config_view(status_sdk_config())

    assert config.connect_timeout == STATUS_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == STATUS_READ_TIMEOUT_SECONDS
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": STATUS_TOTAL_MAX_ATTEMPTS,
    }
    assert STATUS_TOTAL_MAX_ATTEMPTS == 3
    assert config.tcp_keepalive is True


def test_client_factories_use_tokyo_and_the_bounded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    clients = (
        create_ingress_dynamodb_client(region_name=DEFAULT_AWS_REGION),
        create_lambda_client(region_name=DEFAULT_AWS_REGION),
        create_ssm_client(region_name=DEFAULT_AWS_REGION),
    )

    assert {client.meta.region_name for client in clients} == {DEFAULT_AWS_REGION}
    for client in clients:
        config = config_view(client.meta.config)
        assert config.connect_timeout == INGRESS_CONNECT_TIMEOUT_SECONDS
        assert config.read_timeout == INGRESS_READ_TIMEOUT_SECONDS
        assert config.retries["total_max_attempts"] == 1


def test_status_client_factories_use_tokyo_and_the_status_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    clients = (
        create_status_dynamodb_client(region_name=DEFAULT_AWS_REGION),
        create_status_ssm_client(region_name=DEFAULT_AWS_REGION),
    )

    assert {client.meta.region_name for client in clients} == {DEFAULT_AWS_REGION}
    for client in clients:
        config = config_view(client.meta.config)
        assert config.connect_timeout == STATUS_CONNECT_TIMEOUT_SECONDS
        assert config.read_timeout == STATUS_READ_TIMEOUT_SECONDS
        assert config.retries["total_max_attempts"] == STATUS_TOTAL_MAX_ATTEMPTS


@pytest.mark.parametrize(
    "factory",
    [
        create_ingress_dynamodb_client,
        create_lambda_client,
        create_ssm_client,
        create_status_dynamodb_client,
        create_status_ssm_client,
    ],
)
def test_client_factories_reject_padded_or_empty_regions(factory: _ClientFactory) -> None:
    with pytest.raises(ValueError, match="Region"):
        factory(region_name=" ")
