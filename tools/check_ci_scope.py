#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fail closed when a required CI scope or its verification steps are missing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def check_scope(changes_result: str, required: str) -> bool:
    if changes_result != "success":
        raise ValueError("change classification did not succeed")
    if required not in {"true", "false"}:
        raise ValueError("change classification must explicitly publish true or false")
    return required == "true"


def check_dependencies(required: bool, results: object, names: list[str]) -> None:
    if not required:
        return
    if not isinstance(results, dict):
        raise ValueError("dependency results must be an object")
    for name in names:
        result = results.get(name)
        if not isinstance(result, dict) or result.get("result") != "success":
            raise ValueError(f"required dependency did not succeed: {name}")


def check_steps(required: bool, results: object, names: list[str]) -> None:
    if not isinstance(results, dict):
        raise ValueError("step results must be an object")
    expected = "success" if required else "skipped"
    for name in names:
        result = results.get(name)
        if not isinstance(result, dict) or any(
            result.get(field) != expected for field in ("outcome", "conclusion")
        ):
            raise ValueError(f"verification step must be {expected}: {name}")


def check_jobs(changes_result: str, scopes: object, results: object, jobs: list[str]) -> None:
    if not isinstance(scopes, dict) or not isinstance(results, dict):
        raise ValueError("job scopes and results must be objects")
    for mapping in jobs:
        job, scope = mapping.split("=", 1)
        required = check_scope(changes_result, scopes.get(scope, ""))
        expected = "success" if required else "skipped"
        result = results.get(job)
        if not isinstance(result, dict) or result.get("result") != expected:
            raise ValueError(f"verification job must be {expected}: {job}")
        print(f"{job}: {'verified' if required else 'not applicable'}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-dependency", action="append", default=[])
    parser.add_argument("--require-step", action="append", default=[])
    parser.add_argument(
        "--job", action="append", default=[], help="job=scope for an aggregate gate"
    )
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.job:
        check_jobs(
            os.environ.get("CI_CHANGES_RESULT", ""),
            json.loads(os.environ.get("CI_SCOPES", "{}")),
            json.loads(os.environ.get("CI_DEPENDENCY_RESULTS", "{}")),
            args.job,
        )
        return 0
    required = check_scope(
        os.environ.get("CI_CHANGES_RESULT", ""), os.environ.get("CI_REQUIRED", "")
    )
    check_dependencies(
        required,
        json.loads(os.environ.get("CI_DEPENDENCY_RESULTS", "{}")),
        args.require_dependency,
    )
    check_steps(required, json.loads(os.environ.get("CI_STEP_RESULTS", "{}")), args.require_step)
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            output.write(f"required={str(required).lower()}\n")
    if args.require_step:
        message = "Required verification succeeded." if required else "Scope is not applicable."
        print(message)
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a") as output:
                output.write(f"{message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
