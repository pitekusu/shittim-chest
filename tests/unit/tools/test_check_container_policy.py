"""Tests for the shared DHI and runtime identity policy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tools.check_container_policy import (
    DEFAULT_DOCKERFILE_PATH,
    DEFAULT_POLICY_PATH,
    load_container_policy,
    validate_dockerfile,
    validate_uv_reference,
)

_UV_FROM = re.compile(
    r"^FROM ghcr\.io/astral-sh/uv:(?P<version>\d+\.\d+\.\d+)@"
    r"(?P<digest>sha256:[0-9a-f]{64}) AS uv$",
    re.MULTILINE,
)


def test_repository_container_policy_matches_dockerfile() -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)

    assert policy.identity.user_spec == "65532:65532"
    assert policy.heartbeat_tmpfs.path == "/tmp/shittim-chest"  # noqa: S108
    validate_dockerfile(policy, DEFAULT_DOCKERFILE_PATH)


def test_policy_rejects_legacy_runtime_identity(tmp_path: Path) -> None:
    document = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    document["runtime_identity"]["uid"] = 10001
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="inspected DHI runtime"):
        load_container_policy(path)


def test_dockerfile_must_match_pinned_policy(tmp_path: Path) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(
            policy.runtime_reference,
            policy.runtime_reference[:-1] + "0",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stages do not match"):
        validate_dockerfile(policy, dockerfile)


def test_dependabot_uv_digest_bump_does_not_require_python_constant(tmp_path: Path) -> None:
    """Dockerfile remains the sole exact pin for the uv image digest."""

    policy = load_container_policy(DEFAULT_POLICY_PATH)
    source = DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8")
    match = _UV_FROM.search(source)
    assert match is not None
    bumped = source.replace(
        match.group(0),
        "FROM ghcr.io/astral-sh/uv:0.11.99@"
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa AS uv",
        1,
    )
    assert bumped != source
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(bumped, encoding="utf-8")

    validate_dockerfile(policy, dockerfile)


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/astral-sh/uv:0.11.32",
        "ghcr.io/astral-sh/uv:latest@sha256:" + ("a" * 64),
        "docker.io/astral-sh/uv:0.11.32@sha256:" + ("a" * 64),
        "ghcr.io/astral-sh/uv:0.12.0@sha256:" + ("a" * 64),
        "ghcr.io/astral-sh/uv:0.11.7@sha256:" + ("a" * 64),
    ],
)
def test_uv_reference_rejects_unpinned_or_out_of_range_images(reference: str) -> None:
    with pytest.raises(ValueError, match="uv"):
        validate_uv_reference(reference)


def test_uv_reference_accepts_allowed_digest_pin() -> None:
    validate_uv_reference(
        "ghcr.io/astral-sh/uv:0.11.32@sha256:"
        "df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c"
    )
