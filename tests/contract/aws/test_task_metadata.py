"""Contract tests for the trusted ECS task metadata identity boundary."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from shittim_chest.adapters.aws.task_metadata import (
    ECS_CONTAINER_METADATA_URI_V4,
    EcsTaskMetadataUnavailable,
    ecs_task_instance_id,
)

TASK_ID = "0123456789abcdef0123456789abcdef"
BASE_URI = "http://169.254.170.2/v4/example-token"


def metadata(*, task_arn: object) -> bytes:
    return json.dumps({"TaskARN": task_arn, "FutureField": "accepted"}).encode()


def test_resolves_long_task_arn_and_requests_only_the_task_path() -> None:
    requested: list[str] = []

    def fetch(url: str) -> bytes:
        requested.append(url)
        return metadata(
            task_arn=f"arn:aws:ecs:ap-northeast-1:account:task/example-cluster/{TASK_ID}"
        )

    instance_id = ecs_task_instance_id(
        {ECS_CONTAINER_METADATA_URI_V4: f"{BASE_URI}/"},
        fetch=fetch,
    )

    assert instance_id == f"ecs-task-{TASK_ID}"
    assert requested == [f"{BASE_URI}/task"]


def test_accepts_legacy_task_arn_resource_shape() -> None:
    instance_id = ecs_task_instance_id(
        {ECS_CONTAINER_METADATA_URI_V4: BASE_URI},
        fetch=lambda _url: metadata(task_arn=f"arn:aws:ecs:ap-northeast-1:account:task/{TASK_ID}"),
    )

    assert instance_id == f"ecs-task-{TASK_ID}"


@pytest.mark.parametrize(
    "uri",
    [
        "",
        f" {BASE_URI}",
        "https://169.254.170.2/v4/token",
        "http://127.0.0.1/v4/token",
        "http://user" + "@169.254.170.2/v4/token",
        "http://169.254.170.2:8080/v4/token",
        "http://169.254.170.2/v3/token",
        "http://169.254.170.2/v4/token?query=value",
        "http://169.254.170.2/v4/token#fragment",
    ],
)
def test_rejects_untrusted_metadata_uri_without_fetching(uri: str) -> None:
    called = False

    def fetch(_url: str) -> bytes:
        nonlocal called
        called = True
        return metadata(task_arn="unused")

    with pytest.raises(EcsTaskMetadataUnavailable) as error:
        ecs_task_instance_id({ECS_CONTAINER_METADATA_URI_V4: uri}, fetch=fetch)

    assert str(error.value) == "ecs_task_metadata_unavailable"
    assert not called


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not-json",
        b"[]",
        b"{}",
        metadata(task_arn=1),
        metadata(task_arn=f"arn:aws:s3:ap-northeast-1:account:task/{TASK_ID}"),
        metadata(task_arn="arn:aws:ecs:ap-northeast-1:account:service/example"),
        metadata(task_arn="arn:aws:ecs:ap-northeast-1:account:task/not-a-task-id"),
        b"x" * 65_537,
    ],
)
def test_malformed_metadata_fails_closed(payload: bytes) -> None:
    with pytest.raises(EcsTaskMetadataUnavailable):
        ecs_task_instance_id(
            {ECS_CONTAINER_METADATA_URI_V4: BASE_URI},
            fetch=lambda _url: payload,
        )


def test_missing_environment_or_transport_failure_is_content_free() -> None:
    def unavailable(_url: str) -> bytes:
        raise OSError("provider detail must not escape")

    calls: tuple[tuple[dict[str, str], Callable[[str], bytes]], ...] = (
        ({}, lambda _url: b"unused"),
        ({ECS_CONTAINER_METADATA_URI_V4: BASE_URI}, unavailable),
    )
    for environ, fetch in calls:
        with pytest.raises(EcsTaskMetadataUnavailable) as error:
            ecs_task_instance_id(environ, fetch=fetch)
        assert str(error.value) == "ecs_task_metadata_unavailable"
