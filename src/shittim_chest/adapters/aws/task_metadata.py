"""Resolve the immutable physical ECS task identity from local metadata."""

from __future__ import annotations

import http.client
import json
import os
import re
from collections.abc import Callable, Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

ECS_CONTAINER_METADATA_URI_V4 = "ECS_CONTAINER_METADATA_URI_V4"
_METADATA_HOST = "169.254.170.2"
_METADATA_TIMEOUT_SECONDS = 2.0
_MAX_METADATA_BYTES = 65_536
_TASK_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_METADATA_PATH_PATTERN = re.compile(r"/v4/[A-Za-z0-9_-]+")


class EcsTaskMetadataUnavailable(RuntimeError):
    """Signal that the physical Fargate task identity cannot be trusted."""

    def __init__(self) -> None:
        super().__init__("ecs_task_metadata_unavailable")


def ecs_task_instance_id(
    environ: Mapping[str, str] | None = None,
    *,
    fetch: Callable[[str], bytes] | None = None,
) -> str:
    """Return a stable owner derived from the current Fargate task ARN.

    The link-local endpoint is injected by ECS.  Production fails closed rather
    than falling back to a process-random owner, because replacement recovery
    must distinguish the old physical task from the sole current task.
    """

    source = os.environ if environ is None else environ
    try:
        task_url = _task_metadata_url(source[ECS_CONTAINER_METADATA_URI_V4])
        payload = (fetch or _fetch_metadata)(task_url)
        return f"ecs-task-{_task_id_from_payload(payload)}"
    except KeyError, OSError, TypeError, ValueError, http.client.HTTPException:
        raise EcsTaskMetadataUnavailable from None


def _task_metadata_url(raw_uri: str) -> str:
    if not raw_uri or raw_uri != raw_uri.strip():
        raise ValueError("invalid ECS metadata URI")
    parts = urlsplit(raw_uri)
    if (
        parts.scheme != "http"
        or parts.hostname != _METADATA_HOST
        or parts.port not in {None, 80}
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or _METADATA_PATH_PATTERN.fullmatch(parts.path.rstrip("/")) is None
    ):
        raise ValueError("invalid ECS metadata URI")
    task_parts = SplitResult(
        scheme="http",
        netloc=_METADATA_HOST,
        path=f"{parts.path.rstrip('/')}/task",
        query="",
        fragment="",
    )
    return urlunsplit(task_parts)


def _fetch_metadata(task_url: str) -> bytes:
    parts = urlsplit(task_url)
    connection = http.client.HTTPConnection(
        _METADATA_HOST,
        port=80,
        timeout=_METADATA_TIMEOUT_SECONDS,
    )
    try:
        connection.request("GET", parts.path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != http.client.OK:
            raise OSError("ECS metadata request failed")
        payload = response.read(_MAX_METADATA_BYTES + 1)
    finally:
        connection.close()
    if not payload or len(payload) > _MAX_METADATA_BYTES:
        raise ValueError("invalid ECS metadata size")
    return payload


def _task_id_from_payload(payload: bytes) -> str:
    if not payload or len(payload) > _MAX_METADATA_BYTES:
        raise ValueError("invalid ECS metadata size")
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise ValueError("invalid ECS task metadata")
    task_arn = value.get("TaskARN")
    if not isinstance(task_arn, str) or task_arn != task_arn.strip():
        raise ValueError("invalid ECS task ARN")
    arn_parts = task_arn.split(":", 5)
    if len(arn_parts) != 6 or arn_parts[0] != "arn" or arn_parts[2] != "ecs":
        raise ValueError("invalid ECS task ARN")
    resource_parts = arn_parts[5].split("/")
    if len(resource_parts) not in {2, 3} or resource_parts[0] != "task":
        raise ValueError("invalid ECS task ARN")
    task_id = resource_parts[-1]
    if _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError("invalid ECS task ID")
    return task_id


__all__ = (
    "ECS_CONTAINER_METADATA_URI_V4",
    "EcsTaskMetadataUnavailable",
    "ecs_task_instance_id",
)
