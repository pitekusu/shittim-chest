"""Tests for the shared DHI and runtime identity policy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tools.check_container_policy import (
    DEFAULT_DOCKERFILE_PATH,
    DEFAULT_DOCKERIGNORE_PATH,
    DEFAULT_POLICY_PATH,
    dockerfile_stage_reference,
    load_container_policy,
    validate_dhi_reference,
    validate_dockerfile,
    validate_dockerignore,
    validate_uv_reference,
)

_UV_FROM = re.compile(
    r"^FROM ghcr\.io/astral-sh/uv:(?P<version>\d+\.\d+\.\d+)@"
    r"(?P<digest>sha256:[0-9a-f]{64}) AS uv$",
    re.MULTILINE,
)
_RUNTIME_FROM = re.compile(
    r"^FROM dhi\.io/python:3\.14\.6-debian13@"
    r"(?P<digest>sha256:[0-9a-f]{64}) AS runtime-base$",
    re.MULTILINE,
)


def test_repository_container_policy_matches_dockerfile() -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)

    assert policy.identity.user_spec == "65532:65532"
    assert policy.heartbeat_tmpfs.path == "/tmp/shittim-chest"  # noqa: S108
    assert policy.builder_tag == "3.14.6-debian13-dev"
    assert policy.runtime_tag == "3.14.6-debian13"
    validate_dockerfile(policy, DEFAULT_DOCKERFILE_PATH)
    validate_dockerignore(DEFAULT_DOCKERIGNORE_PATH)
    assert dockerfile_stage_reference("runtime").startswith("dhi.io/python:3.14.6-debian13@")


def test_policy_rejects_legacy_runtime_identity(tmp_path: Path) -> None:
    document = json.loads(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    document["runtime_identity"]["uid"] = 10001
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="inspected DHI runtime"):
        load_container_policy(path)


def test_policy_rejects_legacy_schema_with_digests(tmp_path: Path) -> None:
    document = {
        "schema_version": 1,
        "dhi": {
            "builder": {
                "reference": "dhi.io/python:3.14.6-debian13-dev@sha256:" + ("a" * 64),
                "arm64_manifest_digest": "sha256:" + ("b" * 64),
            },
            "runtime": {
                "reference": "dhi.io/python:3.14.6-debian13@sha256:" + ("c" * 64),
                "arm64_manifest_digest": "sha256:" + ("d" * 64),
            },
        },
        "runtime_identity": {
            "username": "nonroot",
            "groupname": "nonroot",
            "uid": 65532,
            "gid": 65532,
            "home": "/home/nonroot",
        },
        "heartbeat_tmpfs": {
            "path": "/tmp/shittim-chest",  # noqa: S108 - fixture mirrors production contract
            "size_mib": 1,
            "mode": "0700",
            "mount_options": ["nosuid", "nodev", "noexec"],
        },
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_container_policy(path)


def test_dockerfile_rejects_wrong_stage_graph(tmp_path: Path) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(
            "AS production",
            "AS prod",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="approved order"):
        validate_dockerfile(policy, dockerfile)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("ARG SOURCE_DATE_EPOCH=0", "ARG BUILD_EPOCH=0", "default SOURCE_DATE_EPOCH"),
        ("ARG SOURCE_DATE_EPOCH\n", "ARG BUILD_EPOCH\n", "consume it in the builder"),
        (
            'ENV SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH}"',
            'ENV SOURCE_DATE_EPOCH="1"',
            "bytecode compilation",
        ),
    ],
)
def test_dockerfile_requires_reproducible_bytecode_environment(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        validate_dockerfile(policy, dockerfile)


def test_dockerfile_requires_canonical_wheel_records(tmp_path: Path) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(
            (
                "python /tmp/canonicalize_wheel_records.py \\\n"
                '        --source-date-epoch "${SOURCE_DATE_EPOCH}" /app/.venv'
            ),
            "true",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonicalize installed wheel RECORD"):
        validate_dockerfile(policy, dockerfile)


def test_dockerfile_rejects_independently_cached_linked_venv_layers(tmp_path: Path) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(
            "COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv",
            "COPY --link --from=builder --chown=65532:65532 /app/.venv /app/.venv",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"production stage.*non-linked venv copy"):
        validate_dockerfile(policy, dockerfile)


@pytest.mark.parametrize(
    "marker",
    [
        "target=/tmp/source-venv,ro",
        "--sort=name",
        '--mtime="@${SOURCE_DATE_EPOCH}"',
        "--owner=65532 --group=65532 --numeric-owner --format=gnu .",
        "--numeric-owner --delay-directory-restore",
    ],
)
def test_dockerfile_requires_deterministic_break_glass_venv_transfer(
    tmp_path: Path,
    marker: str,
) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(marker, "unsafe", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="deterministic tar stream"):
        validate_dockerfile(policy, dockerfile)


def test_dockerfile_keeps_break_glass_package_install_out_of_final_stage(
    tmp_path: Path,
) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    text = DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8")
    dockerfile.write_text(
        text.replace(
            "FROM break-glass-tools AS break-glass\n",
            "FROM break-glass-tools AS break-glass\n\nRUN apt-get update\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not rerun package installation"):
        validate_dockerfile(policy, dockerfile)


@pytest.mark.parametrize(
    "cleanup",
    [
        "apt-get clean",
        "rm -rf /var/lib/apt/lists/* /var/log/apt/*",
        "rm -f /var/log/dpkg.log",
    ],
)
def test_dockerfile_requires_volatile_apt_state_cleanup(
    tmp_path: Path,
    cleanup: str,
) -> None:
    policy = load_container_policy(DEFAULT_POLICY_PATH)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8").replace(cleanup, "true", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="remove volatile apt and dpkg state"):
        validate_dockerfile(policy, dockerfile)


@pytest.mark.parametrize(
    "missing_rule",
    [
        "!tools/",
        "tools/*",
        "!tools/canonicalize_wheel_records.py",
    ],
)
def test_dockerignore_requires_canonicalizer_in_build_context(
    tmp_path: Path,
    missing_rule: str,
) -> None:
    dockerignore = tmp_path / ".dockerignore"
    rules = DEFAULT_DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    rules.remove(missing_rule)
    dockerignore.write_text("\n".join(rules) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must include only the wheel RECORD canonicalizer"):
        validate_dockerignore(dockerignore)


def test_dockerignore_requires_canonicalizer_rules_in_effective_order(tmp_path: Path) -> None:
    dockerignore = tmp_path / ".dockerignore"
    rules = DEFAULT_DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    tools_index = rules.index("!tools/")
    rules[tools_index : tools_index + 3] = [
        "!tools/canonicalize_wheel_records.py",
        "tools/*",
        "!tools/",
    ]
    dockerignore.write_text("\n".join(rules) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="effective order"):
        validate_dockerignore(dockerignore)


@pytest.mark.parametrize("missing_rule", ["**/__pycache__/", "**/*.py[cod]"])
def test_dockerignore_requires_python_bytecode_exclusions(
    tmp_path: Path,
    missing_rule: str,
) -> None:
    dockerignore = tmp_path / ".dockerignore"
    rules = DEFAULT_DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    rules.remove(missing_rule)
    dockerignore.write_text("\n".join(rules) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exclude Python bytecode"):
        validate_dockerignore(dockerignore)


@pytest.mark.parametrize("rule", ["**/__pycache__/", "**/*.py[cod]"])
def test_dockerignore_requires_bytecode_exclusions_after_source_include(
    tmp_path: Path,
    rule: str,
) -> None:
    dockerignore = tmp_path / ".dockerignore"
    rules = DEFAULT_DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    rules.remove(rule)
    rules.insert(rules.index("!src/**"), rule)
    dockerignore.write_text("\n".join(rules) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"follow !src/\*\*"):
        validate_dockerignore(dockerignore)


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


def test_dependabot_dhi_digest_bump_does_not_require_policy_digest(tmp_path: Path) -> None:
    """Dockerfile remains the sole exact pin for DHI image digests."""

    policy = load_container_policy(DEFAULT_POLICY_PATH)
    source = DEFAULT_DOCKERFILE_PATH.read_text(encoding="utf-8")
    match = _RUNTIME_FROM.search(source)
    assert match is not None
    bumped = source.replace(
        match.group(0),
        "FROM dhi.io/python:3.14.6-debian13@"
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
        "AS runtime-base",
        1,
    )
    assert bumped != source
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(bumped, encoding="utf-8")

    validate_dockerfile(policy, dockerfile)
    assert dockerfile_stage_reference("runtime", dockerfile).endswith("b" * 64)


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


@pytest.mark.parametrize(
    ("reference", "expected_tag", "dev"),
    [
        ("dhi.io/python:3.14.6-debian13", "3.14.6-debian13", False),
        ("dhi.io/python:latest@sha256:" + ("a" * 64), "3.14.6-debian13", False),
        (
            "dhi.io/python:3.14.6-debian13-dev@sha256:" + ("a" * 64),
            "3.14.6-debian13",
            False,
        ),
    ],
)
def test_dhi_reference_rejects_unpinned_or_wrong_tag(
    reference: str,
    expected_tag: str,
    dev: bool,
) -> None:
    with pytest.raises(ValueError, match=r"DHI|tag"):
        validate_dhi_reference(reference, expected_tag=expected_tag, dev=dev)
