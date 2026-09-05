#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate pinned release tools and compare them with official GitHub releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, cast

MAX_CONFIG_BYTES: Final = 128 * 1024
MAX_RESPONSE_BYTES: Final = 1024 * 1024
# The weekly workflow uses the runner's Python, not the application's Python 3.14.
LOOKUP_ERRORS: Final = (OSError, RuntimeError, ValueError)
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
    release_prefix: str = ""

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
    return cast(dict[str, object], value)


def _require_string(data: Mapping[str, object], field: str, tool_name: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{tool_name}.{field} must be a non-empty string")
    return value


def load_tool_pins(path: Path) -> tuple[ToolPin, ...]:
    """Load and strictly validate the centralized release-tool configuration."""

    root = _read_json_object(path)
    if set(root) not in ({"schema_version", "tools"}, {"schema_version", "tools", "sources"}):
        raise ValueError("tool version config requires schema_version, tools and optional sources")
    if root["schema_version"] != 1:
        raise ValueError("unsupported tool version schema_version")
    tools = root["tools"]
    if not isinstance(tools, dict) or not tools:
        raise ValueError("tools must be a non-empty object")
    tools = cast(dict[str, object], tools)

    pins: list[ToolPin] = []
    for name in sorted(tools):
        raw = tools[name]
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("tool entries must map string names to objects")
        raw = cast(dict[str, object], raw)
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


def load_source_pins(path: Path, repository_root: Path) -> tuple[ToolPin, ...]:
    """Read actual workflow pins; do not maintain a second copy of their versions."""

    sources = _read_json_object(path).get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("sources must be an object")
    pins: list[ToolPin] = []
    for name, source in sorted(sources.items()):
        if not isinstance(source, dict) or set(source) not in (
            {"repository", "tag_prefix", "files"},
            {"repository", "tag_prefix", "files", "release_prefix"},
        ):
            raise ValueError(f"invalid source fields for {name}")
        repository = _require_string(source, "repository", name)
        prefix = source["tag_prefix"]
        channel = source.get("release_prefix", "")
        if REPOSITORY_PATTERN.fullmatch(repository) is None or prefix not in ("", "v"):
            raise ValueError(f"invalid repository or tag prefix for {name}")
        if not isinstance(channel, str) or (
            channel and re.fullmatch(r"v?\d+(?:\.\d+)?\.", channel) is None
        ):
            raise ValueError(f"invalid release prefix for {name}")
        files = source["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError(f"source files missing for {name}")
        versions: set[str] = set()
        for glob, pattern in files.items():
            if not isinstance(glob, str) or Path(glob).is_absolute() or ".." in Path(glob).parts:
                raise ValueError(f"invalid source path for {name}")
            if not isinstance(pattern, str) or re.compile(pattern).groups != 1:
                raise ValueError(f"source pattern must capture one version for {name}")
            found: list[str] = []
            for file in sorted(repository_root.glob(glob)):
                if file.is_symlink() or not file.is_file():
                    raise ValueError(f"source must be a regular file for {name}")
                found.extend(re.findall(pattern, file.read_text(encoding="utf-8")))
            if not found or any(VERSION_PATTERN.fullmatch(v) is None for v in found):
                raise ValueError(f"version missing or invalid for {name} in {glob}")
            versions.update(found)
        if len(versions) != 1:
            raise ValueError(f"inconsistent source versions for {name}: {sorted(versions)}")
        version = versions.pop()
        if channel and not f"{prefix}{version}".startswith(channel):
            raise ValueError(f"{name} pin no longer matches its monitored release line")
        pins.append(ToolPin(name, repository, version, prefix, "", "", channel))
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


def _github_json(endpoint: str, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "shittim-chest-tool-version-check",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{endpoint}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError("GitHub release lookup failed") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise RuntimeError("GitHub release response is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid GitHub release response") from error
    return value


@cache
def fetch_latest_release_tag(repository: str, token: str | None, release_prefix: str = "") -> str:
    """Compare stable releases, or stable tags within an explicitly supported line."""

    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError(f"invalid GitHub repository: {repository}")
    if release_prefix:
        if re.fullmatch(r"v?\d+(?:\.\d+)?\.", release_prefix) is None:
            raise ValueError("invalid release prefix")
        value = _github_json(f"{repository}/git/matching-refs/tags/{release_prefix}", token)
        if not isinstance(value, list):
            raise RuntimeError("GitHub tags response must be an array")
        if any(
            not isinstance(item, dict) or not isinstance(item.get("ref"), str) for item in value
        ):
            raise RuntimeError("invalid GitHub tag reference")
        tags = [item["ref"].removeprefix("refs/tags/") for item in value]
        stable = [
            tag
            for tag in tags
            if tag.startswith(release_prefix) and VERSION_PATTERN.fullmatch(tag.removeprefix("v"))
        ]
        if not stable:
            raise RuntimeError("no stable tag in the supported release line")
        return max(stable, key=lambda tag: tuple(map(int, tag.removeprefix("v").split("."))))
    value = _github_json(f"{repository}/releases/latest", token)
    if not isinstance(value, dict) or not isinstance(value.get("tag_name"), str):
        raise RuntimeError(f"GitHub release response has no tag_name for {repository}")
    tag_name: str = value["tag_name"]
    if (
        value.get("prerelease") is not False
        or value.get("draft") is not False
        or not VERSION_PATTERN.fullmatch(tag_name.removeprefix("v"))
    ):
        raise RuntimeError(f"latest release is not a stable version for {repository}")
    return tag_name


def check_signer_bundle(repository_root: Path) -> tuple[str, ...]:
    """Detect replacement of the AWS installer served only at a mutable latest URL."""

    workflow = (repository_root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    base = "https://d2hvyiie56hcat.cloudfront.net"
    assets = {
        "ARCHIVE": "linux/arm64/installer/deb/latest/aws-signer-notation-cli_arm64.deb",
        "SIGNATURE": "linux/arm64/installer/deb/latest/aws-signer-notation-cli_arm64.deb.sig",
        "KEY": "linux/public.key",
    }
    updates: list[str] = []
    for field, relative in assets.items():
        match = re.search(rf"AWS_SIGNER_NOTATION_{field}_SHA256: ([0-9a-f]{{64}})", workflow)
        if match is None:
            raise ValueError(f"AWS Signer {field} checksum pin missing")
        digest = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(f"{base}/{relative}", timeout=30) as response:  # noqa: S310
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise RuntimeError("AWS Signer download exceeds limit")
                digest.update(chunk)
        if not size:
            raise RuntimeError("AWS Signer download is empty")
        if digest.hexdigest() != match[1]:
            updates.append(
                f"AWS Signer {field}: 配布物が変更されています。"
                "署名・version・checksumを一緒に確認してください。"
            )
    return tuple(updates)


def build_report(pins: Sequence[ToolPin], fetch_tag: Callable[[ToolPin], str]) -> tuple[str, int]:
    """Keep partial failures visible and distinguish updates from lookup failures."""

    rows = [
        "<!-- release-tool-versions -->",
        "# 固定ツールの更新確認",
        "",
        "| ツール | 現在 | 更新候補 | 確認先 |",
        "|---|---|---|---|",
    ]
    status = 0
    for pin in pins:
        url = f"https://github.com/{pin.repository}/releases"
        try:
            latest = fetch_tag(pin)
            if not VERSION_PATTERN.fullmatch(latest.removeprefix("v")):
                raise ValueError("invalid upstream version")
            candidate = "最新" if latest == pin.expected_tag else latest
            if candidate != "最新":
                status = max(status, 1)
        except LOOKUP_ERRORS:
            candidate = "取得失敗(未確認)"
            status = 2
        line = f"({pin.release_prefix}系列)" if pin.release_prefix else ""
        rows.append(f"| {pin.name}{line} | {pin.expected_tag} | {candidate} | [公式]({url}) |")
    rows += [
        "",
        "workflow内の固定値は実ファイルから取得しています。更新時もSHA・checksum・署名検証を維持します。",
        "uvなどの系列変更は別途互換性を確認し、無条件にlatestへ追従しません。",
    ]
    return "\n".join(rows) + "\n", status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "latest"))
    parser.add_argument("config", type=Path)
    parser.add_argument("--report", type=Path, help="Write a public-safe Markdown report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        pins = load_tool_pins(args.config)
        root = args.config.resolve().parents[1]
        source_pins = load_source_pins(args.config, root)
        pins += source_pins
        if args.mode == "validate":
            print(f"release tool pins are valid: {len(pins)} tools")
            return 0
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        report, status = build_report(
            pins,
            lambda pin: fetch_latest_release_tag(pin.repository, token, pin.release_prefix),
        )
        if source_pins:
            try:
                bundle_updates = check_signer_bundle(root)
                report += (
                    "\n## AWS Signer installer\n\n"
                    + (
                        "\n".join(bundle_updates)
                        if bundle_updates
                        else "配布物・署名・公開鍵のchecksumは固定値と一致しています。"
                    )
                    + "\n"
                )
                if bundle_updates:
                    status = max(status, 1)
            except LOOKUP_ERRORS:
                report += (
                    "\nAWS Signer installer: 取得失敗(未確認)。"
                    "既存の更新Issueを解決扱いにしません。\n"
                )
                status = 2
        if args.report:
            args.report.write_text(report, encoding="utf-8")
        print(report)
        return status
    except LOOKUP_ERRORS as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
