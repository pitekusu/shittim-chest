"""Contract checks for bounded ingress AWS client configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol, cast

import pytest
from botocore.config import Config

from shittim_chest.adapters.aws.clients import (
    CONTROL_RECORDS_CONNECT_TIMEOUT_SECONDS,
    CONTROL_RECORDS_READ_TIMEOUT_SECONDS,
    CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS,
    DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS,
    INGRESS_CONNECT_TIMEOUT_SECONDS,
    INGRESS_MAX_SERIAL_SDK_ROUNDS,
    INGRESS_READ_TIMEOUT_SECONDS,
    INGRESS_RESPONSE_MARGIN_SECONDS,
    INGRESS_TOTAL_MAX_ATTEMPTS,
    RECONCILER_CONNECT_TIMEOUT_SECONDS,
    RECONCILER_READ_TIMEOUT_SECONDS,
    RECONCILER_TOTAL_MAX_ATTEMPTS,
    STATUS_CONNECT_TIMEOUT_SECONDS,
    STATUS_READ_TIMEOUT_SECONDS,
    STATUS_TOTAL_MAX_ATTEMPTS,
    IngressSdkCancellationGate,
    activate_ingress_sdk_cancellation_gate,
    control_records_sdk_config,
    create_control_records_dynamodb_client,
    create_ingress_dynamodb_client,
    create_lambda_client,
    create_runtime_reconciler_dynamodb_client,
    create_runtime_reconciler_ecs_client,
    create_runtime_reconciler_lambda_client,
    create_ssm_client,
    create_status_dynamodb_client,
    create_status_ssm_client,
    current_ingress_sdk_cancellation_gate,
    ingress_sdk_config,
    runtime_reconciler_sdk_config,
    status_sdk_config,
)
from shittim_chest.application.ports import IngressExecutionDeadlineExceeded
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


def test_runtime_reconciler_sdk_config_has_bounded_standard_retries() -> None:
    config = config_view(runtime_reconciler_sdk_config())

    assert config.connect_timeout == RECONCILER_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == RECONCILER_READ_TIMEOUT_SECONDS
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": RECONCILER_TOTAL_MAX_ATTEMPTS,
    }
    assert RECONCILER_TOTAL_MAX_ATTEMPTS == 3
    assert config.tcp_keepalive is True


def test_control_records_sdk_config_has_bounded_standard_retries() -> None:
    config = config_view(control_records_sdk_config())

    assert config.connect_timeout == CONTROL_RECORDS_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == CONTROL_RECORDS_READ_TIMEOUT_SECONDS
    assert config.retries == {
        "mode": "standard",
        "total_max_attempts": CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS,
    }
    assert CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS == 2
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


def test_cancelled_invocation_gate_rejects_ingress_clients_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    dynamodb = create_ingress_dynamodb_client(region_name=DEFAULT_AWS_REGION)
    lambda_client = create_lambda_client(region_name=DEFAULT_AWS_REGION)
    sends: list[str] = []

    def forbidden_send(**_: object) -> None:
        sends.append("send")
        raise AssertionError("cancelled ingress SDK calls must not reach before-send")

    dynamodb.meta.events.register("before-send.dynamodb", forbidden_send)
    lambda_client.meta.events.register("before-send.lambda", forbidden_send)
    gate = IngressSdkCancellationGate()
    with activate_ingress_sdk_cancellation_gate(gate):
        assert current_ingress_sdk_cancellation_gate() is gate
        gate.cancel()
        with pytest.raises(
            IngressExecutionDeadlineExceeded,
            match="ingress_execution_deadline_exceeded",
        ):
            dynamodb.list_tables(Limit=1)
        with pytest.raises(
            IngressExecutionDeadlineExceeded,
            match="ingress_execution_deadline_exceeded",
        ):
            lambda_client.list_functions(MaxItems=1)

    assert sends == []
    assert current_ingress_sdk_cancellation_gate() is None


def test_one_context_gate_reaches_parallel_ingress_worker_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    dynamodb = create_ingress_dynamodb_client(region_name=DEFAULT_AWS_REGION)
    lambda_client = create_lambda_client(region_name=DEFAULT_AWS_REGION)
    gate = IngressSdkCancellationGate()
    gate.cancel()

    async def parallel_calls() -> tuple[object, ...]:
        return tuple(
            await asyncio.gather(
                asyncio.to_thread(dynamodb.list_tables, Limit=1),
                asyncio.to_thread(lambda_client.list_functions, MaxItems=1),
                return_exceptions=True,
            )
        )

    with activate_ingress_sdk_cancellation_gate(gate):
        results = asyncio.run(parallel_calls())

    assert len(results) == 2
    assert all(isinstance(result, IngressExecutionDeadlineExceeded) for result in results)


def test_ingress_gate_does_not_attach_to_ssm_or_status_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    ssm = create_ssm_client(region_name=DEFAULT_AWS_REGION)
    status_dynamodb = create_status_dynamodb_client(region_name=DEFAULT_AWS_REGION)

    class SendReached(RuntimeError):
        pass

    def observe_send(**_: object) -> None:
        raise SendReached

    ssm.meta.events.register("before-send.ssm", observe_send)
    status_dynamodb.meta.events.register("before-send.dynamodb", observe_send)
    gate = IngressSdkCancellationGate()
    gate.cancel()
    with activate_ingress_sdk_cancellation_gate(gate):
        with pytest.raises(SendReached):
            ssm.describe_parameters(MaxResults=1)
        with pytest.raises(SendReached):
            status_dynamodb.list_tables(Limit=1)


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


def test_runtime_reconciler_ecs_factory_uses_tokyo_and_the_bounded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    clients = (
        create_runtime_reconciler_dynamodb_client(region_name=DEFAULT_AWS_REGION),
        create_runtime_reconciler_ecs_client(region_name=DEFAULT_AWS_REGION),
        create_runtime_reconciler_lambda_client(region_name=DEFAULT_AWS_REGION),
    )

    assert {client.meta.region_name for client in clients} == {DEFAULT_AWS_REGION}
    for client in clients:
        config = config_view(client.meta.config)
        assert config.connect_timeout == RECONCILER_CONNECT_TIMEOUT_SECONDS
        assert config.read_timeout == RECONCILER_READ_TIMEOUT_SECONDS
        assert config.retries["total_max_attempts"] == RECONCILER_TOTAL_MAX_ATTEMPTS


def test_control_records_factory_uses_tokyo_and_the_bounded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    client = create_control_records_dynamodb_client(region_name=DEFAULT_AWS_REGION)

    assert client.meta.region_name == DEFAULT_AWS_REGION
    config = config_view(client.meta.config)
    assert config.connect_timeout == CONTROL_RECORDS_CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == CONTROL_RECORDS_READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == CONTROL_RECORDS_TOTAL_MAX_ATTEMPTS


@pytest.mark.parametrize(
    "factory",
    [
        create_control_records_dynamodb_client,
        create_ingress_dynamodb_client,
        create_lambda_client,
        create_runtime_reconciler_dynamodb_client,
        create_runtime_reconciler_ecs_client,
        create_runtime_reconciler_lambda_client,
        create_ssm_client,
        create_status_dynamodb_client,
        create_status_ssm_client,
    ],
)
def test_client_factories_reject_padded_or_empty_regions(factory: _ClientFactory) -> None:
    with pytest.raises(ValueError, match="Region"):
        factory(region_name=" ")
