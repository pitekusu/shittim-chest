# SPDX-License-Identifier: MIT
"""The monthly roster driver resumes a merged PR using GitHub's current response."""

from __future__ import annotations

import json

import pytest
from tools import maintain_student_gacha_roster as roster


@pytest.mark.parametrize("oid", ("a" * 40, None))
def test_resume_merged_pr_without_rest_merge_commit_sha(
    monkeypatch: pytest.MonkeyPatch, oid: str | None
) -> None:
    branch = f"{roster.BRANCH_PREFIX}123456789012-1234567890123456"

    def read_pull(endpoint: str) -> dict[str, object]:
        assert endpoint == "pulls/123"
        return {
            "head": {"ref": branch, "repo": {"full_name": roster.REPO}},
            "base": {"ref": "main", "repo": {"full_name": roster.REPO}},
            "draft": False,
            "merged": True,
        }

    def read_merge_commit(*args: str) -> str:
        assert args == ("pr", "view", "123", "-R", roster.REPO, "--json", "state,mergeCommit")
        return json.dumps({"state": "MERGED", "mergeCommit": {"oid": oid} if oid else None})

    monkeypatch.setattr(roster, "api", read_pull)
    monkeypatch.setattr(roster, "gh", read_merge_commit)

    assert roster.merge_ready(123, {"branch": branch}) == oid
