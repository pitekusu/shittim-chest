#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Synchronize the canonical Obsidian design notes into repository docs/."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SOURCE_ENVIRONMENT_VARIABLE = "SHITTIM_DOCS_SOURCE"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = REPOSITORY_ROOT / "docs"
EXPECTED_FILES = (
    "00_シッテムの箱_ドキュメント索引.md",
    "01_要求仕様書・基本設計書.md",
    "02_議論事項・意思決定記録.md",
    "10_アプリケーション・Python詳細設計.md",
    "11_Discord詳細設計.md",
    "12_OpenAI・プロンプト詳細設計.md",
    "13_DynamoDB・データ整合性詳細設計.md",
    "14_AWS・CDK詳細設計.md",
    "15_GitHub・CI-CD詳細設計.md",
    "16_セキュリティ・プライバシー詳細設計.md",
    "17_運用保守・監視・障害対応設計.md",
    "18_試験・品質保証設計.md",
    "19_実装計画・トレーサビリティ.md",
    "20_実装・試験・検証記録.md",
)
ALLOWED_DESTINATION_EXTRAS = {"LICENSE.md", "README.md"}
SECRET_PATTERNS = {
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[opusr]_[A-Za-z0-9_]{30,}\b"),
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Discord token": re.compile(
        rb"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b"
    ),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "absolute home path": re.compile(rb"(?m)(?:/var)?/home/[A-Za-z0-9._-]+/"),
    "Discord snowflake": re.compile(rb"(?<![A-Za-z0-9])[0-9]{17,20}(?![A-Za-z0-9])"),
    "email address": re.compile(
        rb"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
    ),
}


class SyncError(RuntimeError):
    """Raised when the canonical documents cannot be mirrored safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="update docs/ from Obsidian")
    action.add_argument("--check", action="store_true", help="verify an exact mirror")
    parser.add_argument(
        "--source",
        type=Path,
        help=f"canonical public-doc directory (or set {SOURCE_ENVIRONMENT_VARIABLE})",
    )
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    return parser.parse_args()


def validate_directory(path: Path, *, expected_names: set[str]) -> None:
    if path.is_symlink():
        raise SyncError(f"symlink directory is not allowed: {path}")
    if not path.is_dir():
        raise SyncError(f"directory does not exist: {path}")
    actual_names = {entry.name for entry in path.iterdir()}
    unexpected = sorted(actual_names - expected_names)
    if unexpected:
        raise SyncError(f"unexpected files in {path}: {', '.join(unexpected)}")


def read_safe_file(path: Path) -> bytes:
    if path.is_symlink():
        raise SyncError(f"symlink file is not allowed: {path}")
    if not path.is_file():
        raise SyncError(f"required file is missing: {path}")
    data = path.read_bytes()
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(data):
            raise SyncError(f"possible {label} detected in {path.name}")
    return data


def source_documents(source: Path) -> dict[str, bytes]:
    expected = set(EXPECTED_FILES)
    validate_directory(source, expected_names=expected)
    return {name: read_safe_file(source / name) for name in EXPECTED_FILES}


def write_mirror(documents: dict[str, bytes], destination: Path) -> None:
    if destination.is_symlink():
        raise SyncError(f"symlink directory is not allowed: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    allowed = set(EXPECTED_FILES) | ALLOWED_DESTINATION_EXTRAS
    validate_directory(destination, expected_names=allowed)
    for name, data in documents.items():
        target = destination / name
        if target.is_symlink():
            raise SyncError(f"symlink file is not allowed: {target}")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)


def check_mirror(documents: dict[str, bytes], destination: Path) -> None:
    allowed = set(EXPECTED_FILES) | ALLOWED_DESTINATION_EXTRAS
    validate_directory(destination, expected_names=allowed)
    mismatches: list[str] = []
    for name, source_data in documents.items():
        target = destination / name
        try:
            destination_data = read_safe_file(target)
        except SyncError as error:
            mismatches.append(str(error))
            continue
        if destination_data != source_data:
            mismatches.append(f"content differs: {name}")
    if mismatches:
        raise SyncError("mirror check failed:\n- " + "\n- ".join(mismatches))


def main() -> int:
    args = parse_args()
    try:
        configured_source = args.source or os.environ.get(SOURCE_ENVIRONMENT_VARIABLE)
        if configured_source is None:
            raise SyncError(
                f"source is required: pass --source or set {SOURCE_ENVIRONMENT_VARIABLE}"
            )
        source = Path(configured_source).expanduser().absolute()
        documents = source_documents(source)
        destination = args.destination.expanduser().absolute()
        if args.write:
            write_mirror(documents, destination)
            print(f"mirrored {len(documents)} documents to {destination}")
        else:
            check_mirror(documents, destination)
            print(f"mirror is current: {len(documents)} documents")
    except SyncError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
