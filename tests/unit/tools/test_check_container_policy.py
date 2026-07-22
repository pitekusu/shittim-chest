"""Tests for the shared DHI and runtime identity policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tools.check_container_policy import (
    DEFAULT_DOCKERFILE_PATH,
    DEFAULT_POLICY_PATH,
    load_container_policy,
    validate_dockerfile,
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
