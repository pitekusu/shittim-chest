#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise secret scanners against generated positive and negative Git histories."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Scanner:
    """A secret scanner executable participating in the migration contract."""

    name: str
    executable: Path


GIT_OBJECT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


def synthetic_marker() -> str:
    """Create an invalid but structurally realistic token without storing it in source."""

    suffix = hashlib.sha256(b"shittim-chest synthetic scanner contract").hexdigest()[:36]
    return "".join(("gh", "p_", suffix))


def report_finding_count(path: Path) -> int:
    """Count findings in a scanner JSON report without exposing their contents."""

    if not path.exists():
        return 0
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"scanner report is not valid JSON: {path.name}") from error
    if value is None:
        return 0
    if not isinstance(value, list):
        raise RuntimeError(f"scanner report root must be an array: {path.name}")
    return len(value)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - caller supplies validated scanner path
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            input=input_text,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"scanner execution failed: {Path(command[0]).name}") from error


def _initialize_repository(path: Path, content: str) -> None:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required for scanner contract tests")
    path.mkdir()
    commands = (
        (git, "init", "--quiet"),
        (git, "config", "user.name", "Synthetic Scanner Contract"),
        (git, "config", "user.email", "scanner.invalid"),
    )
    for command in commands:
        result = _run(command, cwd=path)
        if result.returncode != 0:
            raise RuntimeError("could not initialize generated scanner repository")

    blob_result = _run((git, "hash-object", "-w", "--stdin"), cwd=path, input_text=content)
    blob = blob_result.stdout.strip()
    if blob_result.returncode != 0 or GIT_OBJECT_PATTERN.fullmatch(blob) is None:
        raise RuntimeError("could not create generated scanner fixture blob")
    commit_commands = (
        (git, "update-index", "--add", "--cacheinfo", f"100644,{blob},sample.env"),
        (git, "commit", "--quiet", "-m", "Add generated scanner fixture"),
    )
    for commit_command in commit_commands:
        result = _run(commit_command, cwd=path)
        if result.returncode != 0:
            raise RuntimeError("could not commit generated scanner fixture")


def _scan(scanner: Scanner, repository: Path, report: Path) -> tuple[int, int]:
    command = (
        str(scanner.executable),
        "git",
        "--redact",
        "--no-banner",
        "--report-format",
        "json",
        "--report-path",
        str(report),
        str(repository),
    )
    result = _run(command, cwd=repository)
    return result.returncode, report_finding_count(report)


def verify_scanners(scanners: Sequence[Scanner]) -> None:
    """Require each scanner to detect the generated token and accept placeholders."""

    if not scanners:
        raise RuntimeError("at least one scanner is required")
    for scanner in scanners:
        if scanner.executable.is_symlink() or not scanner.executable.is_file():
            raise RuntimeError(f"scanner executable is not a regular file: {scanner.name}")

    with tempfile.TemporaryDirectory(prefix="secret-scanner-contract-") as temporary:
        root = Path(temporary)
        positive = root / "positive"
        negative = root / "negative"
        _initialize_repository(positive, f"SYNTHETIC_TOKEN={synthetic_marker()}\n")
        _initialize_repository(negative, "OPENAI_API_KEY=replace-me\n")

        failures: list[str] = []
        for scanner in scanners:
            positive_status, positive_findings = _scan(
                scanner,
                positive,
                root / f"{scanner.name}-positive.json",
            )
            negative_status, negative_findings = _scan(
                scanner,
                negative,
                root / f"{scanner.name}-negative.json",
            )
            if positive_status != 1 or positive_findings < 1:
                failures.append(
                    f"{scanner.name} did not reject the generated secret "
                    f"(status={positive_status}, findings={positive_findings})"
                )
            if negative_status != 0 or negative_findings != 0:
                failures.append(
                    f"{scanner.name} rejected the safe placeholder "
                    f"(status={negative_status}, findings={negative_findings})"
                )
        if failures:
            raise RuntimeError("secret scanner contract failed:\n- " + "\n- ".join(failures))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--betterleaks", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verify_scanners((Scanner("betterleaks", args.betterleaks),))
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    print("secret scanner contract passed: Betterleaks positive and negative histories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
