#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Classify changed paths for isolated Runtime, Records, and Android CI work."""

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
        "tools/check_ci_scope.py",
        "tests/unit/tools/test_check_ci_scope.py",
        "tools/containers/Dockerfile",
        "tools/container_images.py",
        ".github/tool-versions.json",
        ".github/workflows/ci.yml",
        "security/container-risk-acceptance.json",
        "security/container-risk-acceptance.schema.json",
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
RECORDS_FILES = frozenset(
    {
        "tests/unit/tools/test_build_records_web_artifact.py",
        "tools/build_records_web_artifact.py",
        "tools/sync_docs.py",
    }
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ANDROID_PREFIX = "apps/records-android/"
ANDROID_SHARED_FILES = frozenset(
    {
        "tools/check_ci_scope.py",
        "tests/unit/tools/test_check_ci_scope.py",
        ".github/workflows/ci.yml",
        "tools/classify_ci_paths.py",
        "tests/unit/tools/test_classify_ci_paths.py",
    }
)
SCOPES = (
    "core_tests",
    "core_package",
    "infra",
    "runtime_container",
    "android",
    "records_python",
    "records_contract",
    "records_web",
    "records_infra",
)
PIPELINE_FILES = ANDROID_SHARED_FILES | {
    ".github/workflows/records-ci.yml",
    ".github/tool-versions.json",
    "tools/check_notification_workflows.py",
    "tests/unit/tools/test_check_notification_workflows.py",
}
INFRA_FILES = frozenset({"package.json", "package-lock.json", "tsconfig.json", "cdk.json"})
SHARED_CONTAINER_FILES = frozenset(
    {
        "tools/containers/Dockerfile",
        "tools/container_images.py",
        "tools/run_dynamodb_local.py",
        "tests/unit/tools/test_container_images.py",
        "tests/unit/tools/test_run_dynamodb_local.py",
    }
)
WEB_LAMBDA_ASSETS = (
    "apps/records-web/index.html",
    "apps/records-web/src/assets/fonts/",
    "apps/records-web/third_party/line-seed/",
    "apps/records-web/THIRD_PARTY_NOTICES.md",
)


def _normalized(path: str) -> str:
    normalized = PurePosixPath(path).as_posix()
    if normalized == "." or normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"changed path is outside the repository: {path}")
    return normalized.removeprefix("./")


def _path_scopes(path: str) -> set[str]:
    if path in PIPELINE_FILES or path.startswith(".github/actions/"):
        return set(SCOPES)
    if path.startswith("docs/") or path == "AGENTS.md":
        return set()
    if path.startswith(ANDROID_PREFIX):
        return {"android"}
    if path.startswith("services/records/"):
        return {"records_python", "records_contract"}
    if path.startswith("contracts/records/"):
        return {"records_python", "records_contract", "records_web"}
    if path.startswith(WEB_LAMBDA_ASSETS):
        return {"records_python", "records_contract", "records_web"}
    if path.startswith("apps/records-web/"):
        return {"records_web"}
    if path.startswith("infra/") or path in INFRA_FILES:
        # records-infra owns only synthesis; the shared infra job owns all tests and audit.
        return {"infra", "records_infra"}
    if path in SHARED_CONTAINER_FILES:
        return {"core_tests", "runtime_container", "records_python", "records_contract"}
    if path in RUNTIME_CONTEXT_FILES or path.startswith(RUNTIME_CONTEXT_PREFIXES):
        # Records bundles and imports Core as a local Python dependency.
        return {
            "core_tests",
            "core_package",
            "runtime_container",
            "records_python",
            "records_contract",
        }
    if path in RUNTIME_VALIDATION_FILES or path.startswith(RUNTIME_VALIDATION_PREFIXES):
        return {"core_tests", "runtime_container"}
    if path in RECORDS_FILES or path.startswith(".github/workflows/records-"):
        return {
            "core_tests",
            "infra",
            "records_python",
            "records_contract",
            "records_web",
            "records_infra",
        }
    if path.startswith("tests/") or path == ".github/dependabot.yml":
        return {"core_tests"}
    if path in {
        "tools/check_docs.py",
        "tools/check_public_surface.py",
        "tools/check_tool_versions.py",
        ".github/actionlint.yaml",
        ".github/pull_request_template.md",
    } or path.startswith(".github/ISSUE_TEMPLATE/"):
        return {"core_tests"}
    # A new or unclassified input must never silently remove an existing verification.
    return set(SCOPES)


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    normalized = tuple(_normalized(path) for path in paths if path.strip())
    scopes = (
        set().union(*(_path_scopes(path) for path in normalized)) if normalized else set(SCOPES)
    )
    values = {scope: scope in scopes for scope in SCOPES}
    # Preserve the aggregate output names used by release routing and existing callers.
    values["core"] = any(values[name] for name in ("core_tests", "core_package", "infra"))
    values["records"] = any(values[name] for name in SCOPES if name.startswith("records_"))
    return values


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
    parser.add_argument("--all", action="store_true", dest="full", help="verify every scope")
    args = parser.parse_args()

    values = (
        classify_paths(()) if args.full else classify_paths(changed_paths(args.base, args.head))
    )
    if args.github_output is not None:
        _write_github_output(args.github_output, values)
    print(json.dumps(values, sort_keys=True))


if __name__ == "__main__":
    main()
