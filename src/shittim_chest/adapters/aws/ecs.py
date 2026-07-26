"""Bounded control of one configured Amazon ECS singleton service."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.application.ports import EcsRuntimeUnavailable
from shittim_chest.application.scale_to_zero import EcsRuntimeSnapshot

if TYPE_CHECKING:
    from mypy_boto3_ecs.client import ECSClient


class EcsServiceRuntimeControl:
    """Inspect and change only the desired count of one ECS singleton service."""

    __slots__ = ("_client", "_cluster", "_service")

    def __init__(self, *, client: ECSClient, cluster: str, service: str) -> None:
        self._cluster = _require_identifier(cluster, label="ECS cluster")
        self._service = _require_identifier(service, label="ECS service")
        self._client = client

    async def describe(self) -> EcsRuntimeSnapshot:
        """Return the configured service counts without blocking the event loop."""

        return await asyncio.to_thread(self._describe)

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot:
        """Converge the configured service to zero or one task without redeploying it."""

        target = _require_singleton_count(desired_count, provider_value=False)
        return await asyncio.to_thread(self._set_desired_count, target)

    def _describe(self) -> EcsRuntimeSnapshot:
        try:
            response = self._client.describe_services(
                cluster=self._cluster,
                services=[self._service],
            )
        except BotoCoreError, ClientError:
            raise EcsRuntimeUnavailable from None

        try:
            return _snapshot_from_describe(
                response,
                expected_cluster=self._cluster,
                expected_service=self._service,
            )
        except _MalformedEcsResponse:
            raise EcsRuntimeUnavailable from None

    def _set_desired_count(self, target: int) -> EcsRuntimeSnapshot:
        current = self._describe()
        if current.desired_count == target:
            return current

        try:
            response = self._client.update_service(
                cluster=self._cluster,
                service=self._service,
                desiredCount=target,
            )
        except BotoCoreError, ClientError:
            raise EcsRuntimeUnavailable from None

        try:
            updated = _snapshot_from_update(
                response,
                expected_cluster=self._cluster,
                expected_service=self._service,
            )
            if updated.desired_count != target:
                raise _MalformedEcsResponse
            return updated
        except _MalformedEcsResponse:
            raise EcsRuntimeUnavailable from None


class _MalformedEcsResponse(Exception):
    pass


def _snapshot_from_describe(
    value: object,
    *,
    expected_cluster: str,
    expected_service: str,
) -> EcsRuntimeSnapshot:
    if not isinstance(value, Mapping):
        raise _MalformedEcsResponse

    response = value
    failures = response.get("failures", [])
    if not isinstance(failures, list) or failures:
        raise _MalformedEcsResponse

    services = response.get("services")
    if not isinstance(services, list) or len(services) != 1:
        raise _MalformedEcsResponse
    return _snapshot_from_service(
        services[0],
        expected_cluster=expected_cluster,
        expected_service=expected_service,
    )


def _snapshot_from_update(
    value: object,
    *,
    expected_cluster: str,
    expected_service: str,
) -> EcsRuntimeSnapshot:
    if not isinstance(value, Mapping):
        raise _MalformedEcsResponse
    return _snapshot_from_service(
        value.get("service"),
        expected_cluster=expected_cluster,
        expected_service=expected_service,
    )


def _snapshot_from_service(
    value: object,
    *,
    expected_cluster: str,
    expected_service: str,
) -> EcsRuntimeSnapshot:
    if not isinstance(value, Mapping):
        raise _MalformedEcsResponse

    service_name = _provider_identifier(value.get("serviceName"))
    service_arn = _provider_identifier(value.get("serviceArn"))
    cluster_arn = _provider_identifier(value.get("clusterArn"))
    status = value.get("status")
    if status != "ACTIVE":
        raise _MalformedEcsResponse
    if expected_service not in {service_name, service_arn}:
        raise _MalformedEcsResponse
    if expected_cluster != cluster_arn and expected_cluster != cluster_arn.rsplit("/", 1)[-1]:
        raise _MalformedEcsResponse

    desired_count = _require_singleton_count(value.get("desiredCount"), provider_value=True)
    running_count = _require_singleton_count(value.get("runningCount"), provider_value=True)
    pending_count = _require_singleton_count(value.get("pendingCount"), provider_value=True)
    if running_count + pending_count > 1:
        raise _MalformedEcsResponse
    return EcsRuntimeSnapshot(
        desired_count=desired_count,
        running_count=running_count,
        pending_count=pending_count,
    )


def _provider_identifier(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _MalformedEcsResponse
    return value


def _require_identifier(value: str, *, label: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{label} must not be empty or padded")
    return value


def _require_singleton_count(value: object, *, provider_value: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
        if provider_value:
            raise _MalformedEcsResponse
        raise ValueError("desired count must be either zero or one")
    return value
