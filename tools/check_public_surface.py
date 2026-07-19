#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reject private identifiers and representative credentials in public files."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DENY_PATTERNS = {
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(
        rb"\b(?:gh[opusr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    "OpenAI-style key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Discord token": re.compile(
        rb"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}\b"
    ),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "absolute home path": re.compile(rb"(?m)(?:/var)?/home/[A-Za-z0-9._-]+/"),
    "AWS account ID": re.compile(rb"(?<![A-Za-z0-9])(?!0{12}(?![0-9]))[0-9]{12}(?![A-Za-z0-9])"),
    "Discord snowflake": re.compile(rb"(?<![A-Za-z0-9])[0-9]{17,20}(?![A-Za-z0-9])"),
    "email address": re.compile(
        rb"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b"
    ),
}


def repository_paths(root: Path) -> tuple[Path, ...]:
    """List tracked and non-ignored untracked paths from Git."""

    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required for public repository scanning")
    try:
        result = subprocess.run(  # noqa: S603 - resolved absolute executable, fixed arguments
            [
                git,
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git file listing failed: {detail}") from error
    return tuple(Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\0") if raw)


def candidate_files(
    root: Path,
    *,
    relative_paths: Iterable[Path] | None = None,
) -> list[Path]:
    """Resolve public files without allowing tracked files to hide in ignored names."""

    candidates: list[Path] = []
    paths = repository_paths(root) if relative_paths is None else tuple(relative_paths)
    for relative_path in paths:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError(f"invalid repository path: {relative_path}")
        path = root / relative_path
        if path.is_symlink():
            raise RuntimeError(f"symlink is not allowed in public repository: {path}")
        if not path.is_file():
            raise RuntimeError(f"listed repository file is missing or not regular: {path}")
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
