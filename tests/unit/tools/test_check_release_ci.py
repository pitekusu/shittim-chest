# SPDX-License-Identifier: MIT
"""Release authorization must use successful jobs from the current main attempt."""

import json
import subprocess
from copy import deepcopy

import pytest
from tools import check_release_ci
from tools.check_release_ci import latest_main_run, require_jobs

SHA = "a" * 40
RUN = {
    "id": 10,
    "run_attempt": 2,
    "head_sha": SHA,
    "head_branch": "main",
    "event": "push",
    "run_started_at": "2026-09-20T15:00:00Z",
    "status": "completed",
    "conclusion": "success",
}


def test_github_pages_requests_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [{"jobs": []}, {"jobs": [{"name": "tests"}]}]
    monkeypatch.setattr(check_release_ci.shutil, "which", lambda _: "/usr/bin/gh")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        assert command == ["/usr/bin/gh", "api", "--paginate", "--slurp", "repos/example/jobs"]
        return subprocess.CompletedProcess(command, 0, json.dumps(pages))

    monkeypatch.setattr(check_release_ci.subprocess, "run", fake_run)
    assert check_release_ci.github_pages("repos/example/jobs") == pages


def test_run_selection_uses_all_pages_and_excludes_pr_and_other_shas() -> None:
    pages = [
        {
            "workflow_runs": [
                {**RUN, "id": 99, "event": "pull_request"},
                {**RUN, "id": 98, "head_sha": "b" * 40},
            ]
        },
        {"workflow_runs": [RUN]},
    ]
    assert latest_main_run(pages, SHA) == RUN


@pytest.mark.parametrize(
    "field,value", [("event", "pull_request"), ("head_branch", "branch"), ("head_sha", "b" * 40)]
)
def test_a_same_named_check_from_an_untrusted_run_cannot_authorize_release(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError, match="no main workflow"):
        latest_main_run([{"workflow_runs": [{**RUN, field: value}]}], SHA)


@pytest.mark.parametrize(
    "status,conclusion",
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
    ],
)
def test_an_older_success_cannot_hide_a_newer_unsuccessful_run(
    status: str, conclusion: str | None
) -> None:
    newer = {**RUN, "id": 11, "status": status, "conclusion": conclusion}
    with pytest.raises(ValueError, match="latest main workflow"):
        latest_main_run([{"workflow_runs": [RUN, newer]}], SHA)


@pytest.mark.parametrize(
    "status,conclusion", [("queued", None), ("completed", "failure"), ("completed", "cancelled")]
)
def test_rerunning_an_older_run_cannot_hide_its_failure_behind_a_newer_run_id(
    status: str, conclusion: str | None
) -> None:
    rerun = {**RUN, "id": 9, "run_attempt": 3, "status": status, "conclusion": conclusion}
    if status == "completed":
        rerun["run_started_at"] = "2026-09-20T16:00:00Z"
    with pytest.raises(ValueError, match="latest main workflow"):
        latest_main_run([{"workflow_runs": [RUN, rerun]}], SHA)


@pytest.mark.parametrize("field,value", [("id", True), ("run_attempt", 0), ("run_attempt", "2")])
def test_invalid_run_identity_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="invalid workflow"):
        latest_main_run([{"workflow_runs": [{**RUN, field: value}]}], SHA)


def test_required_jobs_can_span_multiple_pages() -> None:
    require_jobs(
        [
            {"jobs": [{"name": "tests", "status": "completed", "conclusion": "success"}]},
            {"jobs": [{"name": "records-gate", "status": "completed", "conclusion": "success"}]},
        ],
        ("tests", "records-gate"),
    )


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
def test_required_jobs_fail_closed(conclusion: str | None) -> None:
    with pytest.raises(ValueError, match="required check"):
        require_jobs(
            [{"jobs": [{"name": "tests", "status": "completed", "conclusion": conclusion}]}],
            ("tests",),
        )


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_jobs_fail_closed(count: int) -> None:
    job = {"name": "grype", "status": "completed", "conclusion": "success"}
    with pytest.raises(ValueError, match="required check"):
        require_jobs([{"jobs": [deepcopy(job) for _ in range(count)]}], ("grype",))


@pytest.mark.parametrize(
    "missing", [name for names in check_release_ci.WORKFLOW_CHECKS.values() for name in names]
)
def test_every_required_check_must_exist_in_the_selected_attempt(missing: str) -> None:
    names = next(names for names in check_release_ci.WORKFLOW_CHECKS.values() if missing in names)
    jobs = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in names
        if name != missing
    ]
    with pytest.raises(ValueError, match="required check"):
        require_jobs([{"jobs": jobs}], names)


def test_cli_rejects_a_non_main_workflow_before_calling_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "pitekusu/shittim-chest")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1/merge")
    monkeypatch.setenv("GITHUB_SHA", SHA)
    monkeypatch.setenv("GITHUB_WORKFLOW_SHA", SHA)
    with pytest.raises(ValueError, match="immutable main"):
        check_release_ci.main()
