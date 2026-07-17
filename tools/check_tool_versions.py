#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate pinned release tools and compare them with official GitHub releases."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_CONFIG_BYTES: Final = 128 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
REPOSITORY_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_PATTERN: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
COMMON_FIELDS: Final = frozenset(
    {"archive_name", "archive_sha256", "repository", "tag_prefix", "version"}
)
BETTERLEAKS_FIELDS: Final = COMMON_FIELDS | frozenset(
    {
        "certificate_identity",
        "certificate_oidc_issuer",
        "checksums_name",
        "checksums_sha256",
        "signature_bundle_name",
        "signature_bundle_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class ToolPin:
    """One release tool pinned by version and immutable artifact digest."""

    name: str
    repository: str
    version: str
    tag_prefix: str
    archive_name: str
    archive_sha256: str

    @property
    def expected_tag(self) -> str:
        """Return the release tag corresponding to the pinned version."""

        return f"{self.tag_prefix}{self.version}"


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"tool version config must be a regular file: {path}")
    data = path.read_bytes()
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError(f"tool version config is too large: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"tool version config is not UTF-8: {path}") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid tool version JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("tool version config root must be an object")
    return value


def _require_string(data: Mapping[str, object], field: str, tool_name: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{tool_name}.{field} must be a non-empty string")
    return value


def load_tool_pins(path: Path) -> tuple[ToolPin, ...]:
    """Load and strictly validate the centralized release-tool configuration."""

    root = _read_json_object(path)
    if set(root) != {"schema_version", "tools"}:
        raise ValueError("tool version config requires only schema_version and tools")
    if root["schema_version"] != 1:
        raise ValueError("unsupported tool version schema_version")
    tools = root["tools"]
    if not isinstance(tools, dict) or not tools:
        raise ValueError("tools must be a non-empty object")

    pins: list[ToolPin] = []
    for name in sorted(tools):
        raw = tools[name]
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("tool entries must map string names to objects")
        expected_fields = BETTERLEAKS_FIELDS if name == "betterleaks" else COMMON_FIELDS
        if set(raw) != expected_fields:
            missing = sorted(expected_fields - set(raw))
            extra = sorted(set(raw) - expected_fields)
            raise ValueError(f"invalid fields for {name}: missing={missing}, extra={extra}")

        repository = _require_string(raw, "repository", name)
        version = _require_string(raw, "version", name)
        tag_prefix = _require_string(raw, "tag_prefix", name)
        archive_name = _require_string(raw, "archive_name", name)
        archive_sha256 = _require_string(raw, "archive_sha256", name)

        if REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise ValueError(f"invalid GitHub repository for {name}: {repository}")
        if VERSION_PATTERN.fullmatch(version) is None:
            raise ValueError(f"invalid semantic version for {name}: {version}")
        if tag_prefix not in {"v", ""}:
            raise ValueError(f"unsupported tag prefix for {name}: {tag_prefix}")
        if Path(archive_name).name != archive_name or version not in archive_name:
            raise ValueError(f"invalid archive name for {name}: {archive_name}")
        if SHA256_PATTERN.fullmatch(archive_sha256) is None:
            raise ValueError(f"invalid archive SHA-256 for {name}")

        if name == "betterleaks":
            _validate_betterleaks_metadata(raw, version)

        pins.append(
            ToolPin(
                name=name,
                repository=repository,
                version=version,
                tag_prefix=tag_prefix,
                archive_name=archive_name,
                archive_sha256=archive_sha256,
            )
        )
    return tuple(pins)


def _validate_betterleaks_metadata(data: Mapping[str, object], version: str) -> None:
    for field in ("checksums_sha256", "signature_bundle_sha256"):
        if SHA256_PATTERN.fullmatch(_require_string(data, field, "betterleaks")) is None:
            raise ValueError(f"invalid betterleaks.{field}")
    for field in ("checksums_name", "signature_bundle_name"):
        value = _require_string(data, field, "betterleaks")
        if Path(value).name != value:
            raise ValueError(f"invalid betterleaks.{field}")
    identity = _require_string(data, "certificate_identity", "betterleaks")
    expected_identity = (
        "https://github.com/betterleaks/betterleaks/.github/workflows/"
        f"release.yml@refs/tags/v{version}"
    )
    if identity != expected_identity:
        raise ValueError("betterleaks certificate identity does not match the pinned tag")
    if (
        _require_string(data, "certificate_oidc_issuer", "betterleaks")
        != "https://token.actions.githubusercontent.com"
    ):
        raise ValueError("unexpected Betterleaks certificate OIDC issuer")


def fetch_latest_release_tag(repository: str, token: str | None) -> str:
    """Fetch the latest stable release tag from the official GitHub API."""

    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"invalid GitHub repository: {repository}")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "shittim-chest-tool-version-check",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"GitHub release lookup failed for {repository}") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"GitHub release response is too large for {repository}")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid GitHub release response for {repository}") from error
    if not isinstance(value, dict) or not isinstance(value.get("tag_name"), str):
        raise RuntimeError(f"GitHub release response has no tag_name for {repository}")
    tag_name: str = value["tag_name"]
    return tag_name


def find_outdated_pins(
    pins: Sequence[ToolPin],
    fetch_tag: Callable[[str], str],
) -> tuple[str, ...]:
    """Return deterministic messages for pins that differ from latest stable releases."""

    outdated: list[str] = []
    for pin in pins:
        latest = fetch_tag(pin.repository)
        if latest != pin.expected_tag:
            outdated.append(
                f"{pin.name}: pinned {pin.expected_tag}, latest {latest} ({pin.repository})"
            )
    return tuple(outdated)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "latest"))
    parser.add_argument("config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pins = load_tool_pins(args.config)
        if args.mode == "validate":
            print(f"release tool pins are valid: {len(pins)} tools")
            return 0
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        outdated = find_outdated_pins(
            pins,
            lambda repository: fetch_latest_release_tag(repository, token),
        )
    except (RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    if outdated:
        print("release tool updates are available:\n- " + "\n- ".join(outdated), file=sys.stderr)
        return 1
    print(f"release tool pins match latest stable releases: {len(pins)} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
