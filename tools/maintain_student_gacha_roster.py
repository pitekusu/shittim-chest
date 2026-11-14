#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Advance a monthly student roster PR and release using the host's gh login."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from shutil import which
from typing import Any

from tools.check_release_ci import WORKFLOW_CHECKS, latest_main_run, require_jobs
from tools.update_student_gacha_roster import ROSTER_PATH

REPO = "pitekusu/shittim-chest"
ROSTER = ROSTER_PATH.relative_to(ROSTER_PATH.parents[2]).as_posix()
BRANCH_PREFIX = "codex/student-gacha-roster/"
RELEASE = "student-gacha-roster-release.yml"
PR_WORKFLOWS = {
    "ci.yml": "CI",
    "records-ci.yml": "Records CI",
    "codeql.yml": "CodeQL",
}
REQUIRED_CHECKS = frozenset(
    (PR_WORKFLOWS[workflow], job) for workflow, jobs in WORKFLOW_CHECKS.items() for job in jobs
)
TITLE = "生徒募集の名簿を更新する"
WAIT_SECONDS = 60


def gh(*args: str, request: dict[str, Any] | None = None, pending: bool = False) -> str:
    executable = which("gh")
    if executable is None:
        raise RuntimeError("GitHub CLI is unavailable")
    result = subprocess.run(  # noqa: S603 - fixed CLI; API values are arguments or JSON
        [executable, *args],
        input=None if request is None else json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"},
    )
    if pending and "no checks reported" in result.stderr.lower():
        return "[]"
    if result.returncode and not (pending and result.returncode in {1, 8} and result.stdout):
        raise RuntimeError(f"GitHub CLI failed ({args[0]}): {result.stderr.strip()}")
    return result.stdout


def api(endpoint: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    result = gh(
        "api",
        f"repos/{REPO}/{endpoint}",
        "-H",
        "X-GitHub-Api-Version: 2026-03-10",
        "--method",
        method,
        *(() if body is None else ("--input", "-")),
        request=body,
    )
    return json.loads(result) if result else None


def commit_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("invalid GitHub commit SHA")
    return value


def main_sha() -> str:
    return commit_sha(api("git/ref/heads/main")["object"]["sha"])


def read_roster(sha: str) -> tuple[str, bytes]:
    entry = api(f"contents/{ROSTER}?ref={commit_sha(sha)}")
    if entry["type"] != "file" or entry["encoding"] != "base64":
        raise RuntimeError("GitHub returned an unexpected roster entry")
    return commit_sha(entry["sha"]), base64.b64decode(entry["content"])


def candidate(state_directory: Path, base: str) -> tuple[str, bytes, bytes]:
    blob, original = read_roster(base)
    with tempfile.TemporaryDirectory(dir=state_directory) as temporary:
        path = Path(temporary) / ROSTER_PATH.name
        path.write_bytes(original)
        subprocess.run(  # noqa: S603 - local trusted updater and private temporary input path
            [sys.executable, "-m", "tools.update_student_gacha_roster", "--roster", str(path)],
            check=True,
            timeout=150,
        )
        return blob, original, path.read_bytes()


def store(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state) + "\n", encoding="utf-8")
    temporary.replace(path)


def find_pr(branch: str) -> int | None:
    matches = api(f"pulls?state=all&base=main&head=pitekusu:{branch}")
    if len(matches) > 1:
        raise RuntimeError("more than one PR matches the roster branch")
    return matches[0]["number"] if matches else None


def prepare(path: Path, state: dict[str, Any], updated: bytes) -> int:
    branch, base = state["branch"], commit_sha(state["base"])
    if not re.fullmatch(r"codex/student-gacha-roster/[0-9a-f]{12}-[0-9a-f]{16}", branch):
        raise ValueError("invalid roster branch")
    existing = find_pr(branch)
    if existing is not None:
        return existing
    refs = api(f"git/matching-refs/heads/{branch}")
    ref = f"refs/heads/{branch}"
    matching = [item for item in refs if item["ref"] == ref]
    head = commit_sha(matching[0]["object"]["sha"]) if matching else None
    if head is None:
        api("git/refs", method="POST", body={"ref": ref, "sha": base})
        head = base
    if head == base:
        api(
            f"contents/{ROSTER}",
            method="PUT",
            body={
                "message": TITLE,
                "content": base64.b64encode(updated).decode("ascii"),
                "branch": branch,
                "sha": state["original_blob"],
            },
        )
    elif read_roster(head)[1] != updated:
        raise RuntimeError("unrecognized content already exists on the roster branch")
    created = api(
        "pulls",
        method="POST",
        body={
            "title": f"[codex] {TITLE}",
            "head": branch,
            "base": "main",
            "body": (
                "キャラクター一覧とNPC一覧を月次照合し、生徒募集の名簿を更新します。\n\n"
                "- 星1かつ入手欄に◇があるキャラクターはレアリティ3とします。\n"
                "- レアリティ0のアロナとプラナは対象外です。\n"
                "- 必須CIとCodeQLの成功後にsquash mergeし、名簿を配信します。\n"
            ),
        },
    )
    state["pr"] = created["number"]
    store(path, state)
    return created["number"]


def merge_ready(number: int, state: dict[str, Any]) -> str | None:
    pr = api(f"pulls/{number}")
    if (
        pr["head"]["ref"] != state["branch"]
        or pr["head"]["repo"]["full_name"] != REPO
        or pr["base"]["ref"] != "main"
        or pr["base"]["repo"]["full_name"] != REPO
        or pr["draft"]
    ):
        raise RuntimeError("the PR no longer matches the roster update")
    if pr["merged"]:
        return commit_sha(pr["merge_commit_sha"])
    if pr["state"] != "open":
        raise RuntimeError(f"roster PR #{number} was closed before merge")
    files = api(f"pulls/{number}/files?per_page=2")
    if pr["changed_files"] != 1 or [item["filename"] for item in files] != [ROSTER]:
        raise RuntimeError(f"roster PR #{number} must change exactly the roster")
    head = commit_sha(pr["head"]["sha"])
    _, data = read_roster(head)
    if hashlib.sha256(data).hexdigest() != state["digest"]:
        raise RuntimeError(f"roster PR #{number} content changed after validation")
    if pr["mergeable_state"] == "dirty":
        raise RuntimeError(f"roster PR #{number} has a merge conflict")
    if pr["mergeable_state"] == "behind":
        api(f"pulls/{number}/update-branch", method="PUT", body={"expected_head_sha": head})
        return None
    checks = json.loads(
        gh("pr", "checks", str(number), "-R", REPO, "--json", "name,bucket,workflow", pending=True)
    )
    statuses = {(check["workflow"], check["name"]): check["bucket"] for check in checks}
    passed = {check for check, status in statuses.items() if status == "pass"}
    failed = {
        f"{workflow}/{job}"
        for workflow, job in REQUIRED_CHECKS
        if statuses.get((workflow, job)) in {"fail", "cancel"}
    }
    if failed:
        raise RuntimeError(f"roster PR #{number} failed checks: {', '.join(sorted(failed))}")
    if passed >= REQUIRED_CHECKS and pr["mergeable_state"] in {"clean", "has_hooks"}:
        gh("pr", "merge", str(number), "-R", REPO, "--squash", "--match-head-commit", head)
    return None


def main_ci_passed(sha: str) -> bool:
    for workflow, checks in WORKFLOW_CHECKS.items():
        page = api(f"actions/workflows/{workflow}/runs?branch=main&head_sha={sha}&per_page=100")
        if not page["workflow_runs"] or any(
            run["status"] != "completed" for run in page["workflow_runs"]
        ):
            return False
        run = latest_main_run([page], sha)
        jobs = api(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")
        require_jobs([jobs], checks)
    return True


def release(path: Path, state: dict[str, Any]) -> bool:
    sha = state.get("dispatch_sha") or main_sha()
    if "run_id" not in state and state.get("dispatch_requested") != sha:
        sha = main_sha()
        if hashlib.sha256(read_roster(sha)[1]).hexdigest() != state["digest"]:
            raise RuntimeError("main contains a different roster than the merged PR")
        if not main_ci_passed(sha):
            return False
        state["dispatch_sha"] = sha
        store(path, state)

    if "run_id" not in state:
        runs = api(
            f"actions/workflows/{RELEASE}/runs?branch=main&head_sha={sha}"
            "&event=workflow_dispatch&per_page=1"
        )["workflow_runs"]
        run_id = runs[0]["id"] if runs else None
        if run_id is None:
            requested = state.get("dispatch_requested")
            if requested == sha and time.time() - state.get("requested_at", 0) < 600:
                return False
            # Reconcile an uncertain dispatch before making another request.
            state["dispatch_requested"] = sha
            state["requested_at"] = time.time()
            store(path, state)
            run = api(
                f"actions/workflows/{RELEASE}/dispatches",
                method="POST",
                body={"ref": "main", "inputs": {"expected_sha": sha}},
            )
            run_id = run["workflow_run_id"]
        state["run_id"] = run_id
        store(path, state)
    run = api(f"actions/runs/{state['run_id']}")
    if run["status"] != "completed":
        return False
    if run["conclusion"] != "success":
        if run["head_sha"] != sha or main_sha() != sha:
            # Recheck the current main and the merged roster on the next poll.
            for key in ("dispatch_sha", "dispatch_requested", "requested_at", "run_id"):
                state.pop(key, None)
            store(path, state)
            return False
        raise RuntimeError(
            f"roster release failed: {run['html_url']}. "
            "Resolve the failure, rerun that workflow, and restart this service."
        )
    if run["head_sha"] != sha:
        raise RuntimeError("release workflow reported success for an unexpected commit")
    print(f"Roster release verified by workflow: {run['html_url']}", flush=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="Compare sources without GitHub writes"
    )
    args = parser.parse_args()
    directory = args.state_directory
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pending.json"
    state: dict[str, Any]
    if args.check or not path.exists():
        base = main_sha()
        original_blob, original, updated = candidate(directory, base)
        if original == updated:
            print("Roster matches both Wiki pages")
            return
        digest = hashlib.sha256(updated).hexdigest()
        if args.check:
            print(f"Roster changes found (SHA-256 {digest})")
            return
        if main_sha() != base:
            raise RuntimeError("main moved during source verification; run the service again")
        state = {
            "base": base,
            "branch": f"{BRANCH_PREFIX}{base[:12]}-{digest[:16]}",
            "original_blob": original_blob,
            "digest": digest,
            "content": base64.b64encode(updated).decode("ascii"),
        }
        store(path, state)
    else:
        state = json.loads(path.read_text(encoding="utf-8"))

    gh("auth", "status", "--hostname", "github.com")
    if gh("api", "user", "--jq", ".login").strip() != "pitekusu":
        raise RuntimeError("GitHub CLI must authenticate as the repository owner")
    if "pr" not in state:
        updated = base64.b64decode(state["content"])
        if hashlib.sha256(updated).hexdigest() != state["digest"]:
            raise RuntimeError("saved roster bytes failed checksum verification")
        state["pr"] = prepare(path, state, updated)
        store(path, state)
    print(f"Roster PR: https://github.com/{REPO}/pull/{state['pr']}", flush=True)
    deadline = time.monotonic() + 95 * WAIT_SECONDS
    while time.monotonic() < deadline:
        if "merged" not in state:
            merged = merge_ready(state["pr"], state)
            if merged is not None:
                state["merged"] = merged
                store(path, state)
        if "merged" in state and release(path, state):
            path.unlink()
            return
        time.sleep(WAIT_SECONDS)
    raise RuntimeError("roster work is pending; restart this service to resume")


if __name__ == "__main__":
    main()
