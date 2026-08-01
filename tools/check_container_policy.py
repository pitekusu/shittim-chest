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
DEFAULT_DOCKERIGNORE_PATH: Final = REPOSITORY_ROOT / ".dockerignore"
MAX_POLICY_BYTES: Final = 64 * 1024
DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
FROM_PATTERN: Final = re.compile(
    r"^FROM\s+(?P<reference>\S+)\s+AS\s+(?P<stage>[A-Za-z0-9._-]+)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# Dockerfile is the sole pin for exact image digests. Dependabot updates those
# lines alone; policy only constrains registry, tag family, and digest form.
UV_REFERENCE_PATTERN: Final = re.compile(
    r"^ghcr\.io/astral-sh/uv:(?P<version>\d+\.\d+\.\d+)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)
UV_MIN_VERSION: Final = (0, 11, 8)
UV_MAX_VERSION_EXCLUSIVE: Final = (0, 12, 0)
DHI_REFERENCE_PATTERN: Final = re.compile(
    r"^dhi\.io/python:(?P<tag>3\.14\.6-debian13(?P<dev>-dev)?)"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
STAGE_ALIASES: Final = {
    "builder": "builder",
    "runtime": "runtime-base",
    "runtime-base": "runtime-base",
    "uv": "uv",
}


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

    builder_tag: str
    runtime_tag: str
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
    if root["schema_version"] != 2:
        raise ValueError("unsupported container policy schema_version")

    dhi = _object(root["dhi"], "dhi")
    if set(dhi) != {"builder_tag", "runtime_tag"}:
        raise ValueError("dhi requires builder_tag and runtime_tag")
    builder_tag = _string(dhi, "builder_tag", "dhi")
    runtime_tag = _string(dhi, "runtime_tag", "dhi")
    if builder_tag != "3.14.6-debian13-dev":
        raise ValueError("dhi.builder_tag must be the approved DHI builder tag")
    if runtime_tag != "3.14.6-debian13":
        raise ValueError("dhi.runtime_tag must be the approved DHI runtime tag")

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
        builder_tag=builder_tag,
        runtime_tag=runtime_tag,
        identity=identity,
        heartbeat_tmpfs=tmpfs,
    )


def parse_dockerfile_stages(dockerfile: Path) -> list[tuple[str, str]]:
    """Return ordered Dockerfile stages as (name, reference) pairs."""

    text = dockerfile.read_text(encoding="utf-8")
    return [
        (match.group("stage").lower(), match.group("reference"))
        for match in FROM_PATTERN.finditer(text)
    ]


def dockerfile_stage_reference(
    stage: str,
    dockerfile: Path = DEFAULT_DOCKERFILE_PATH,
) -> str:
    """Return one digest-pinned stage reference from the authoritative Dockerfile."""

    requested = STAGE_ALIASES.get(stage.casefold())
    if requested is None:
        raise ValueError(f"unknown Dockerfile stage: {stage}")
    for name, reference in parse_dockerfile_stages(dockerfile):
        if name == requested:
            return reference
    raise ValueError(f"Dockerfile is missing stage {requested}")


def _parse_semver_triplet(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        raise ValueError(f"invalid semantic version: {version}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def validate_uv_reference(reference: str) -> None:
    """Require a digest-pinned official uv image within the project version range."""

    match = UV_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError("Dockerfile uv stage must use a digest-pinned ghcr.io/astral-sh/uv image")
    version = _parse_semver_triplet(match.group("version"))
    if not UV_MIN_VERSION <= version < UV_MAX_VERSION_EXCLUSIVE:
        raise ValueError(
            "Dockerfile uv version "
            f"{match.group('version')} is outside the allowed range "
            f">={UV_MIN_VERSION[0]}.{UV_MIN_VERSION[1]}.{UV_MIN_VERSION[2]},"
            f"<{UV_MAX_VERSION_EXCLUSIVE[0]}.{UV_MAX_VERSION_EXCLUSIVE[1]}"
        )


def validate_dhi_reference(reference: str, *, expected_tag: str, dev: bool) -> None:
    """Require a digest-pinned DHI image with the approved tag family."""

    match = DHI_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError("Dockerfile DHI stage must use a digest-pinned dhi.io/python image")
    if match.group("tag") != expected_tag or bool(match.group("dev")) is not dev:
        variant = "builder" if dev else "runtime"
        raise ValueError(f"Dockerfile {variant} tag must be {expected_tag}")
    if DIGEST_PATTERN.fullmatch(match.group("digest")) is None:
        raise ValueError("Dockerfile DHI stage digest is invalid")


def validate_dockerfile(policy: ContainerPolicy, dockerfile: Path) -> None:
    """Require Dockerfile stages and numeric identities to match the policy."""

    text = dockerfile.read_text(encoding="utf-8")
    stages = parse_dockerfile_stages(dockerfile)
    if len(stages) != 6:
        raise ValueError("Dockerfile must declare the six approved stages")
    names = [name for name, _reference in stages]
    if names != [
        "uv",
        "builder",
        "runtime-base",
        "production",
        "fault-test",
        "break-glass",
    ]:
        raise ValueError("Dockerfile stages do not match the approved order")

    uv_reference = stages[0][1]
    builder_reference = stages[1][1]
    runtime_reference = stages[2][1]
    validate_uv_reference(uv_reference)
    validate_dhi_reference(builder_reference, expected_tag=policy.builder_tag, dev=True)
    validate_dhi_reference(runtime_reference, expected_tag=policy.runtime_tag, dev=False)
    source_date_args = re.findall(r"^ARG SOURCE_DATE_EPOCH(?:=0)?$", text, re.MULTILINE)
    if source_date_args != ["ARG SOURCE_DATE_EPOCH=0", "ARG SOURCE_DATE_EPOCH"]:
        raise ValueError(
            "Dockerfile must default SOURCE_DATE_EPOCH globally and consume it in the builder"
        )
    first_from = text.index("FROM ")
    if text.index("ARG SOURCE_DATE_EPOCH=0") > first_from:
        raise ValueError("Dockerfile SOURCE_DATE_EPOCH default must precede every stage")
    builder_start = text.index(f"FROM {builder_reference} AS builder")
    runtime_start = text.index(f"FROM {runtime_reference} AS runtime-base")
    builder_text = text[builder_start:runtime_start]
    if 'ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}"' not in builder_text:
        raise ValueError("Dockerfile builder must expose SOURCE_DATE_EPOCH to bytecode compilation")
    if (
        "COPY tools/canonicalize_wheel_records.py /tmp/canonicalize_wheel_records.py"
        not in builder_text
        or "python /tmp/canonicalize_wheel_records.py /app/.venv" not in builder_text
    ):
        raise ValueError("Dockerfile builder must canonicalize installed wheel RECORD ordering")
    if stages[3] != ("production", "runtime-base"):
        raise ValueError("production stage must derive from runtime-base")
    if stages[4] != ("fault-test", "production"):
        raise ValueError("fault-test stage must derive from production")
    if stages[5] != ("break-glass", builder_reference):
        raise ValueError("break-glass stage must reuse the builder image pin")
    linked_venv_copy = "COPY --link --from=builder --chown=65532:65532 /app/.venv /app/.venv"
    if text.count(linked_venv_copy) != 2:
        raise ValueError(
            "production and break-glass stages must use independent linked venv layers"
        )
    break_glass_start = text.index(f"FROM {builder_reference} AS break-glass")
    break_glass_text = text[break_glass_start:]
    volatile_apt_cleanup = (
        "apt-get clean",
        "rm -rf /var/lib/apt/lists/* /var/log/apt/*",
        "rm -f /var/log/dpkg.log",
    )
    if any(marker not in break_glass_text for marker in volatile_apt_cleanup):
        raise ValueError("break-glass stage must remove volatile apt and dpkg state")
    if f"USER {policy.identity.user_spec}" not in text:
        raise ValueError("Dockerfile USER does not match the DHI runtime identity")
    if "10001" in text:
        raise ValueError("legacy UID/GID 10001 is forbidden")


def validate_dockerignore(dockerignore: Path) -> None:
    """Require the RECORD canonicalizer and exclude generated source bytecode."""

    rules = dockerignore.read_text(encoding="utf-8").splitlines()
    required_rules = [
        "!tools/",
        "tools/*",
        "!tools/canonicalize_wheel_records.py",
    ]
    positions: list[int] = []
    for rule in required_rules:
        try:
            positions.append(rules.index(rule))
        except ValueError as error:
            raise ValueError(
                ".dockerignore must include only the wheel RECORD canonicalizer from tools/"
            ) from error
    if positions != sorted(positions):
        raise ValueError(
            ".dockerignore wheel RECORD canonicalizer rules must be in effective order"
        )
    try:
        source_include_position = rules.index("!src/**")
        bytecode_positions = [
            rules.index("**/__pycache__/"),
            rules.index("**/*.py[cod]"),
        ]
    except ValueError as error:
        raise ValueError(
            ".dockerignore must exclude Python bytecode after the !src/** inclusion"
        ) from error
    if (
        any(rules.count(rule) != 1 for rule in ("**/__pycache__/", "**/*.py[cod]"))
        or bytecode_positions != sorted(bytecode_positions)
        or any(position <= source_include_position for position in bytecode_positions)
    ):
        raise ValueError(
            ".dockerignore Python bytecode exclusions must follow !src/** in effective order"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE_PATH)
    parser.add_argument("--dockerignore", type=Path, default=DEFAULT_DOCKERIGNORE_PATH)
    parser.add_argument(
        "--print-reference",
        choices=sorted(STAGE_ALIASES),
        help="print one digest-pinned Dockerfile stage reference and exit",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.print_reference is not None:
            print(dockerfile_stage_reference(args.print_reference, args.dockerfile))
            return 0
        policy = load_container_policy(args.policy)
        validate_dockerfile(policy, args.dockerfile)
        validate_dockerignore(args.dockerignore)
    except (OSError, ValueError) as error:
        print(f"container policy check failed: {error}", file=sys.stderr)
        return 1
    print("container policy and Dockerfile are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
