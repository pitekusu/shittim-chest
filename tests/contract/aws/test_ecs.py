"""Stubbed contracts for the configured ECS singleton runtime control."""

from __future__ import annotations

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ecs.client import ECSClient

from shittim_chest.adapters.aws.ecs import EcsServiceRuntimeControl
from shittim_chest.application.ports import EcsRuntimeUnavailable
from shittim_chest.application.scale_to_zero import EcsRuntimeSnapshot

CLUSTER_NAME = "runtime-cluster"
CLUSTER_ARN = f"cluster/{CLUSTER_NAME}"
SERVICE_NAME = "runtime-service"
SERVICE_ARN = f"service/{CLUSTER_NAME}/{SERVICE_NAME}"
DESCRIBE_PARAMS = {"cluster": CLUSTER_NAME, "services": [SERVICE_NAME]}


def client() -> ECSClient:
    return boto3.client(
        "ecs",
        region_name="ap-northeast-1",
        config=Config(signature_version=UNSIGNED),
    )


def service(
    *,
    desired: object = 0,
    running: object = 0,
    pending: object = 0,
    status: str = "ACTIVE",
    service_name: str = SERVICE_NAME,
    service_arn: str = SERVICE_ARN,
    cluster_arn: str = CLUSTER_ARN,
) -> dict[str, object]:
    return {
        "clusterArn": cluster_arn,
        "serviceArn": service_arn,
        "serviceName": service_name,
        "status": status,
        "desiredCount": desired,
        "runningCount": running,
        "pendingCount": pending,
    }


def control(sdk: ECSClient) -> EcsServiceRuntimeControl:
    return EcsServiceRuntimeControl(
        client=sdk,
        cluster=CLUSTER_NAME,
        service=SERVICE_NAME,
    )


@pytest.mark.asyncio
async def test_describe_returns_the_active_configured_singleton() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": [service(desired=1, running=1)]},
            DESCRIBE_PARAMS,
        )

        result = await control(sdk).describe()

        assert result == EcsRuntimeSnapshot(
            desired_count=1,
            running_count=1,
            pending_count=0,
        )
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_describe_treats_http_200_failure_entries_as_unavailable() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {
                "failures": [
                    {
                        "arn": SERVICE_ARN,
                        "reason": "MISSING",
                        "detail": "provider detail must not escape",
                    }
                ],
                "services": [],
            },
            DESCRIBE_PARAMS,
        )

        with pytest.raises(EcsRuntimeUnavailable) as caught:
            await control(sdk).describe()

    assert str(caught.value) == "ecs_runtime_unavailable"
    assert "provider" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "services",
    [
        [],
        [service(status="DRAINING")],
        [service(), service(service_name="other", service_arn=f"{SERVICE_ARN}-other")],
        [service(service_name="other")],
        [service(cluster_arn=f"{CLUSTER_ARN}-other")],
        [{}],
    ],
    ids=(
        "missing",
        "inactive",
        "multiple",
        "different-service",
        "different-cluster",
        "missing-fields",
    ),
)
async def test_describe_fails_closed_for_missing_or_unconfigured_services(
    services: list[dict[str, object]],
) -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": services},
            DESCRIBE_PARAMS,
        )

        with pytest.raises(
            EcsRuntimeUnavailable,
            match=r"^ecs_runtime_unavailable$",
        ):
            await control(sdk).describe()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("desiredCount", 2),
        ("runningCount", -1),
        ("pendingCount", True),
    ],
)
async def test_describe_rejects_non_singleton_or_boolean_counts(
    field: str,
    value: object,
) -> None:
    sdk = client()
    response_service = service()
    response_service[field] = value
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": [response_service]},
            DESCRIBE_PARAMS,
        )

        with pytest.raises(EcsRuntimeUnavailable):
            await control(sdk).describe()


@pytest.mark.asyncio
async def test_describe_rejects_running_and_pending_tasks_together() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {
                "failures": [],
                "services": [service(desired=1, running=1, pending=1)],
            },
            DESCRIBE_PARAMS,
        )

        with pytest.raises(EcsRuntimeUnavailable):
            await control(sdk).describe()


@pytest.mark.asyncio
async def test_set_desired_count_is_a_no_op_when_already_converged() -> None:
    sdk = client()
    expected = service(desired=1, running=1)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": [expected]},
            DESCRIBE_PARAMS,
        )

        result = await control(sdk).set_desired_count(1)

        assert result == EcsRuntimeSnapshot(1, 1, 0)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_set_desired_count_updates_only_the_configured_count() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": [service()]},
            DESCRIBE_PARAMS,
        )
        stubber.add_response(
            "update_service",
            {"service": service(desired=1, pending=1)},
            {
                "cluster": CLUSTER_NAME,
                "service": SERVICE_NAME,
                "desiredCount": 1,
            },
        )

        result = await control(sdk).set_desired_count(1)

        assert result == EcsRuntimeSnapshot(1, 0, 1)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_update_fails_closed_when_response_does_not_confirm_target() -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_services",
            {"failures": [], "services": [service()]},
            DESCRIBE_PARAMS,
        )
        stubber.add_response(
            "update_service",
            {"service": service()},
            {
                "cluster": CLUSTER_NAME,
                "service": SERVICE_NAME,
                "desiredCount": 1,
            },
        )

        with pytest.raises(EcsRuntimeUnavailable):
            await control(sdk).set_desired_count(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("desired_count", [-1, 2, True])
async def test_set_desired_count_rejects_non_singleton_targets(
    desired_count: int,
) -> None:
    sdk = client()
    with Stubber(sdk) as stubber, pytest.raises(ValueError, match="zero or one"):
        await control(sdk).set_desired_count(desired_count)
        stubber.assert_no_pending_responses()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["describe_services", "update_service"])
async def test_provider_transients_map_to_the_stable_boundary_error(operation: str) -> None:
    sdk = client()
    with Stubber(sdk) as stubber:
        if operation == "update_service":
            stubber.add_response(
                "describe_services",
                {"failures": [], "services": [service()]},
                DESCRIBE_PARAMS,
            )
            expected_params = {
                "cluster": CLUSTER_NAME,
                "service": SERVICE_NAME,
                "desiredCount": 1,
            }
        else:
            expected_params = DESCRIBE_PARAMS
        stubber.add_client_error(
            operation,
            service_error_code="ServerException",
            service_message="sensitive provider detail",
            http_status_code=500,
            expected_params=expected_params,
        )

        with pytest.raises(EcsRuntimeUnavailable) as caught:
            if operation == "update_service":
                await control(sdk).set_desired_count(1)
            else:
                await control(sdk).describe()

    assert str(caught.value) == "ecs_runtime_unavailable"
    assert "sensitive" not in str(caught.value)
