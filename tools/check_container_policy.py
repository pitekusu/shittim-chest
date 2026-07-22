#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate the pinned DHI references and shared container runtime policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH: Final = REPOSITORY_ROOT / "container-policy.json"
DEFAULT_DOCKERFILE_PATH: Final = REPOSITORY_ROOT / "Dockerfile"
MAX_POLICY_BYTES: Final = 64 * 1024
DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
REFERENCE_PATTERN: Final = re.compile(
    r"^dhi\.io/python:(?P<tag>3\.14\.6-debian13(?P<dev>-dev)?)@(?P<digest>sha256:[0-9a-f]{64})$"
)
FROM_PATTERN: Final = re.compile(
    r"^FROM\s+(?P<reference>\S+)\s+AS\s+(?P<stage>[A-Za-z0-9._-]+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
UV_REFERENCE: Final = (
    "ghcr.io/astral-sh/uv:0.11.31@"
    "sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437"
)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """The image-defined identity used consistently by Docker and ECS."""

    username: str
    groupname: str
    uid: int
    gid: int
    home: str

    @property
    def user_spec(self) -> str:
        """Return the numeric Docker/ECS user specification."""

        return f"{self.uid}:{self.gid}"


@dataclass(frozen=True, slots=True)
class HeartbeatTmpfs:
    """The only writable production filesystem path."""

    path: str
    size_mib: int
    mode: str
    mount_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContainerPolicy:
    """Strictly validated container policy shared by tests and CDK."""

    builder_reference: str
    builder_arm64_manifest_digest: str
    runtime_reference: str
    runtime_arm64_manifest_digest: str
    identity: RuntimeIdentity
    heartbeat_tmpfs: HeartbeatTmpfs


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(data: dict[str, object], field: str, label: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return value


def _integer(data: dict[str, object], field: str, label: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label}.{field} must be an integer")
    return value


def _validate_reference(reference: str, *, dev: bool) -> None:
    match = REFERENCE_PATTERN.fullmatch(reference)
    if match is None or bool(match.group("dev")) is not dev:
        variant = "builder" if dev else "runtime"
        raise ValueError(f"invalid pinned DHI {variant} reference")


def load_container_policy(path: Path = DEFAULT_POLICY_PATH) -> ContainerPolicy:
    """Load the single container policy with strict field and value checks."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"container policy must be a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) > MAX_POLICY_BYTES:
        raise ValueError("container policy is too large")
    try:
        root = json.loads(payload, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid container policy JSON: {error}") from error
    root = _object(root, "container policy")
    if set(root) != {"schema_version", "dhi", "runtime_identity", "heartbeat_tmpfs"}:
        raise ValueError("container policy has unexpected root fields")
    if root["schema_version"] != 1:
        raise ValueError("unsupported container policy schema_version")

    dhi = _object(root["dhi"], "dhi")
    if set(dhi) != {"builder", "runtime"}:
        raise ValueError("dhi requires builder and runtime")
    builder = _object(dhi["builder"], "dhi.builder")
    runtime = _object(dhi["runtime"], "dhi.runtime")
    expected_image_fields = {"reference", "arm64_manifest_digest"}
    if set(builder) != expected_image_fields or set(runtime) != expected_image_fields:
        raise ValueError("DHI image entries have unexpected fields")
    builder_reference = _string(builder, "reference", "dhi.builder")
    runtime_reference = _string(runtime, "reference", "dhi.runtime")
    _validate_reference(builder_reference, dev=True)
    _validate_reference(runtime_reference, dev=False)
    builder_manifest = _string(builder, "arm64_manifest_digest", "dhi.builder")
    runtime_manifest = _string(runtime, "arm64_manifest_digest", "dhi.runtime")
    if DIGEST_PATTERN.fullmatch(builder_manifest) is None:
        raise ValueError("invalid builder ARM64 manifest digest")
    if DIGEST_PATTERN.fullmatch(runtime_manifest) is None:
        raise ValueError("invalid runtime ARM64 manifest digest")

    identity_data = _object(root["runtime_identity"], "runtime_identity")
    if set(identity_data) != {"username", "groupname", "uid", "gid", "home"}:
        raise ValueError("runtime_identity has unexpected fields")
    identity = RuntimeIdentity(
        username=_string(identity_data, "username", "runtime_identity"),
        groupname=_string(identity_data, "groupname", "runtime_identity"),
        uid=_integer(identity_data, "uid", "runtime_identity"),
        gid=_integer(identity_data, "gid", "runtime_identity"),
        home=_string(identity_data, "home", "runtime_identity"),
    )
    if identity != RuntimeIdentity("nonroot", "nonroot", 65532, 65532, "/home/nonroot"):
        raise ValueError("runtime_identity does not match the inspected DHI runtime")

    tmpfs_data = _object(root["heartbeat_tmpfs"], "heartbeat_tmpfs")
    if set(tmpfs_data) != {"path", "size_mib", "mode", "mount_options"}:
        raise ValueError("heartbeat_tmpfs has unexpected fields")
    options = tmpfs_data["mount_options"]
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise ValueError("heartbeat_tmpfs.mount_options must be a string array")
    tmpfs = HeartbeatTmpfs(
        path=_string(tmpfs_data, "path", "heartbeat_tmpfs"),
        size_mib=_integer(tmpfs_data, "size_mib", "heartbeat_tmpfs"),
        mode=_string(tmpfs_data, "mode", "heartbeat_tmpfs"),
        mount_options=tuple(cast(list[str], options)),
    )
    if tmpfs != HeartbeatTmpfs(
        "/tmp/shittim-chest",  # noqa: S108 - intentional isolated Fargate tmpfs
        1,
        "0700",
        ("nosuid", "nodev", "noexec"),
    ):
        raise ValueError("heartbeat_tmpfs does not match the production contract")

    return ContainerPolicy(
        builder_reference=builder_reference,
        builder_arm64_manifest_digest=builder_manifest,
        runtime_reference=runtime_reference,
        runtime_arm64_manifest_digest=runtime_manifest,
        identity=identity,
        heartbeat_tmpfs=tmpfs,
    )


def validate_dockerfile(policy: ContainerPolicy, dockerfile: Path) -> None:
    """Require Dockerfile stages and numeric identities to match the policy."""

    text = dockerfile.read_text(encoding="utf-8")
    stages = [
        (match.group("stage").lower(), match.group("reference"))
        for match in FROM_PATTERN.finditer(text)
    ]
    expected = [
        ("uv", UV_REFERENCE),
        ("builder", policy.builder_reference),
        ("runtime-base", policy.runtime_reference),
        ("production", "runtime-base"),
        ("fault-test", "production"),
        ("break-glass", policy.builder_reference),
    ]
    if stages != expected:
        raise ValueError("Dockerfile stages do not match container-policy.json")
    if f"USER {policy.identity.user_spec}" not in text:
        raise ValueError("Dockerfile USER does not match the DHI runtime identity")
    if "10001" in text:
        raise ValueError("legacy UID/GID 10001 is forbidden")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE_PATH)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        policy = load_container_policy(args.policy)
        validate_dockerfile(policy, args.dockerfile)
    except (OSError, ValueError) as error:
        print(f"container policy check failed: {error}", file=sys.stderr)
        return 1
    print("container policy and Dockerfile are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
