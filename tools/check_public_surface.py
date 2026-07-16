#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reject private identifiers and representative credentials in public files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
DENY_PATTERNS = {
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(rb"\b(?:gh[opusr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Discord token": re.compile(
        rb"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b"
    ),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "absolute home path": re.compile(rb"(?m)(?:/var)?/home/[A-Za-z0-9._-]+/"),
    "Discord snowflake": re.compile(rb"(?<![0-9])[0-9]{17,20}(?![0-9])"),
    "email address": re.compile(
        rb"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
    ),
}


def candidate_files(root: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise RuntimeError(f"symlink is not allowed in public repository: {path}")
        if path.is_file():
            candidates.append(path)
    return sorted(candidates)


def main() -> int:
    findings: list[str] = []
    try:
        paths = candidate_files(REPOSITORY_ROOT)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    for path in paths:
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for label, pattern in DENY_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{path.relative_to(REPOSITORY_ROOT)}: possible {label}")

    if findings:
        print("public surface check failed:\n- " + "\n- ".join(findings), file=sys.stderr)
        return 1

    print(f"public surface is clean: {len(paths)} files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
