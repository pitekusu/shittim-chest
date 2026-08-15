#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Classify changed paths for isolated Runtime and Records CI work."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

RUNTIME_CONTEXT_FILES = frozenset(
    {
        ".dockerignore",
        "Dockerfile",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "tests/__init__.py",
        "tests/fixtures/container_process.py",
        "tools/canonicalize_wheel_records.py",
        "tools/transfer_tree_deterministically.py",
    }
)
RUNTIME_CONTEXT_PREFIXES = ("src/",)
RUNTIME_VALIDATION_FILES = frozenset(
    {
        ".github/tool-versions.json",
        ".github/workflows/ci.yml",
        "security/container-risk-acceptance.json",
        "tools/classify_ci_paths.py",
    }
)
RUNTIME_VALIDATION_PREFIXES = (
    "tests/unit/tools/test_classify_ci_paths.py",
    "tests/unit/tools/test_check_container",
    "tests/unit/tools/test_check_image_sbom.py",
    "tools/check_container",
    "tools/check_image_sbom.py",
    "tools/report_grype.py",
    "tools/run_container_gate.py",
)
RECORDS_PREFIXES = (
    ".github/workflows/records-",
    "apps/records-web/",
    "contracts/records/",
    "services/records/",
    "infra/bin/shittim-records.ts",
    "infra/lib/records-",
    "infra/test/records-",
)
RECORDS_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        "docs/00_シッテムの箱_ドキュメント索引.md",
        "docs/24_シッテムの箱 議事録設計.md",
        "docs/README.md",
        "tests/unit/tools/test_classify_ci_paths.py",
        "tools/classify_ci_paths.py",
        "tools/sync_docs.py",
    }
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _normalized(path: str) -> str:
    normalized = PurePosixPath(path).as_posix()
    if normalized == "." or normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"changed path is outside the repository: {path}")
    return normalized.removeprefix("./")


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    normalized = tuple(_normalized(path) for path in paths if path.strip())
    runtime = any(
        path in RUNTIME_CONTEXT_FILES
        or path in RUNTIME_VALIDATION_FILES
        or path.startswith(RUNTIME_CONTEXT_PREFIXES)
        or path.startswith(RUNTIME_VALIDATION_PREFIXES)
        for path in normalized
    )
    records = any(path in RECORDS_FILES or path.startswith(RECORDS_PREFIXES) for path in normalized)
    return {"runtime_container": runtime, "records": records}


def changed_paths(base: str, head: str) -> tuple[str, ...]:
    if COMMIT_SHA.fullmatch(base) is None or COMMIT_SHA.fullmatch(head) is None:
        raise ValueError("base and head must be full lowercase commit SHAs")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    result = subprocess.run(  # noqa: S603 - fixed executable and validated commit SHAs
        [
            git,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=ACMRD",
            f"{base}...{head}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(path for path in result.stdout.split("\0") if path)


def _write_github_output(path: Path, values: dict[str, bool]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={str(value).lower()}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    values = classify_paths(changed_paths(args.base, args.head))
    if args.github_output is not None:
        _write_github_output(args.github_output, values)
    print(json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
