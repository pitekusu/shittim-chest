#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Require the latest complete main CI attempts for the immutable release SHA."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

WORKFLOW_CHECKS = {
    "ci.yml": (
        "quality",
        "tests",
        "security",
        "package",
        "cdk",
        "docs-public-safety",
        "container-arm64",
        "grype",
        "android-gate",
    ),
    "records-ci.yml": ("records-gate",),
    "codeql.yml": ("Analyze (python)", "Analyze (javascript-typescript)", "Analyze (actions)"),
}


def latest_main_run(pages: list[dict[str, Any]], sha: str) -> dict[str, Any]:
    candidates = [
        run
        for page in pages
        for run in page["workflow_runs"]
        if run.get("head_sha") == sha
        and run.get("head_branch") == "main"
        and run.get("event") in {"push", "workflow_dispatch", "schedule"}
    ]
    if not candidates:
        raise ValueError("no main workflow run exists for the release SHA")
    run = max(candidates, key=lambda candidate: candidate["id"])
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("the latest main workflow run has not succeeded")
    for key in ("id", "run_attempt"):
        if type(run.get(key)) is not int or run[key] < 1:
            raise ValueError(f"invalid workflow {key}")
    return run


def require_jobs(pages: list[dict[str, Any]], required: tuple[str, ...]) -> None:
    jobs = [job for page in pages for job in page["jobs"]]
    for name in required:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1 or any(
            job.get("status") != "completed" or job.get("conclusion") != "success"
            for job in matches
        ):
            raise ValueError(f"required check did not succeed in this attempt: {name}")


def github_pages(endpoint: str) -> list[dict[str, Any]]:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("GitHub CLI is unavailable")
    result = subprocess.run(  # noqa: S603 - fixed CLI and validated repository/SHA/run IDs
        [gh, "api", "--paginate", "--slurp", endpoint],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or not pages or not all(isinstance(p, dict) for p in pages):
        raise ValueError("GitHub returned invalid paginated data")
    return pages


def main() -> None:
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    if (
        repository != "pitekusu/shittim-chest"
        or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_WORKFLOW_SHA") != sha
    ):
        raise ValueError("release must use its immutable main workflow SHA")
    for workflow, checks in WORKFLOW_CHECKS.items():
        run = latest_main_run(
            github_pages(
                f"repos/{repository}/actions/workflows/{workflow}/runs"
                f"?branch=main&head_sha={sha}&per_page=100"
            ),
            sha,
        )
        require_jobs(
            github_pages(
                f"repos/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}"
                "/jobs?per_page=100"
            ),
            checks,
        )
        print(
            f"{workflow}: required checks succeeded in run {run['id']}, "
            f"attempt {run['run_attempt']}"
        )


if __name__ == "__main__":
    main()
