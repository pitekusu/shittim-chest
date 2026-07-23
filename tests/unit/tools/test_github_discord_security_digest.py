"""Tests for the complete daily dependency and security digest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from tools.github_discord_notifications.github_api import GitHubApiError
from tools.github_discord_notifications.models import JsonObject, JsonValue
from tools.github_discord_notifications.security_digest import (
    collect_digest,
    render_dependabot,
    render_security,
    run_security_digest,
)

NOW = datetime(2026, 7, 23, 0, 37, tzinfo=UTC)
MAIN_SHA = "abcdef0123456789"


def dependabot_alert(*, severity: str = "high", number: int = 1) -> JsonObject:
    return {
        "number": number,
        "dependency": {"package": {"name": f"package-{number}"}},
        "security_advisory": {"severity": severity},
        "security_vulnerability": {"first_patched_version": {"identifier": "2.0"}},
        "vulnerable_manifest_path": "uv.lock",
    }


def code_alert(*, severity: str = "high", tool: str = "CodeQL") -> JsonObject:
    return {
        "number": 2,
        "rule": {"security_severity_level": severity, "description": "unsafe **rule**"},
        "tool": {"name": tool},
    }


def dependabot_pull() -> JsonObject:
    return {"number": 7, "user": {"login": "dependabot[bot]"}}


@dataclass
class FakeGitHub:
    alerts: list[JsonObject] = field(default_factory=lambda: [dependabot_alert()])
    code_alerts: list[JsonObject] = field(default_factory=lambda: [code_alert()])
    pulls: list[JsonObject] = field(default_factory=lambda: [dependabot_pull()])
    analyses: list[JsonObject] = field(
        default_factory=lambda: [
            {"tool": {"name": "CodeQL"}, "commit_sha": MAIN_SHA, "error": ""},
            {"tool": {"name": "Grype"}, "commit_sha": MAIN_SHA, "error": ""},
        ]
    )
    check_runs: list[JsonObject] = field(
        default_factory=lambda: [{"name": "tests", "status": "completed", "conclusion": "success"}]
    )
    fail_path: str | None = None
    old_workflows: set[str] = field(default_factory=set)
    codeql_conclusion: str = "success"
    requests: list[str] = field(default_factory=list)

    def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject:
        self._record(path)
        if path == "commits/main":
            return {"sha": MAIN_SHA}
        if path.startswith("pulls/"):
            return {
                "number": 7,
                "title": "Update dependency",
                "html_url": "https://github.com/example/project/pull/7",
                "updated_at": "2026-07-22T00:00:00Z",
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "pullsha"},
            }
        if path.startswith("actions/workflows/"):
            filename = path.split("/")[2]
            updated = NOW - timedelta(days=9 if filename in self.old_workflows else 1)
            return {
                "workflow_runs": [
                    {
                        "updated_at": updated.isoformat(),
                        "conclusion": self.codeql_conclusion,
                    }
                ]
            }
        raise AssertionError(f"unexpected object request: {path}")

    def paginate_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonObject]:
        self._record(path)
        return {
            "dependabot/alerts": self.alerts,
            "code-scanning/alerts": self.code_alerts,
            "pulls": self.pulls,
            "code-scanning/analyses": self.analyses,
        }[path]

    def paginate_keyed_array(
        self,
        path: str,
        *,
        key: str,
        query: dict[str, str] | None = None,
    ) -> list[JsonObject]:
        self._record(path)
        if path == "actions/workflows":
            assert key == "workflows"
            return [
                {
                    "id": 99,
                    "name": "CodeQL",
                    "path": "dynamic/github-code-scanning/codeql",
                }
            ]
        assert key == "check_runs"
        return self.check_runs

    def _record(self, path: str) -> None:
        self.requests.append(path)
        if self.fail_path == path:
            raise GitHubApiError("simulated API failure")


@dataclass
class FakeDiscord:
    messages: list[tuple[str, JsonObject]] = field(default_factory=list)

    def send(self, *, webhook_url: str, thread_id: str, payload: JsonObject) -> None:
        assert webhook_url.endswith("token-placeholder")
        self.messages.append((thread_id, payload))


def environment() -> dict[str, str]:
    return {
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/example/token-placeholder",
        "DISCORD_THREAD_DEPENDABOT": "201",
        "DISCORD_THREAD_SECURITY": "202",
        "DISCORD_ALERT_ROLE_ID": "301",
    }


def embed(payload: JsonObject) -> JsonObject:
    return cast(JsonObject, cast(list[JsonValue], payload["embeds"])[0])


def fields(payload: JsonObject) -> dict[str, str]:
    values = cast(list[JsonValue], embed(payload)["fields"])
    return {
        cast(str, cast(JsonObject, value)["name"]): cast(str, cast(JsonObject, value)["value"])
        for value in values
    }


def test_complete_digest_sends_one_embed_to_each_thread_after_all_reads() -> None:
    github = FakeGitHub()
    discord = FakeDiscord()
    results = run_security_digest(
        environment=environment(),
        github=github,
        discord=discord,
        now=NOW,
    )
    assert [result.logical_thread for result in results] == ["Dependabot", "セキュリティ"]
    assert [thread for thread, _ in discord.messages] == ["201", "202"]
    assert fields(discord.messages[0][1])["Open alerts"] == "1"
    assert fields(discord.messages[1][1])["Open code scanning alerts"] == "1"
    assert all(
        payload["allowed_mentions"] == {"parse": [], "roles": ["301"]}
        for _, payload in discord.messages
    )
    assert github.requests[-1].startswith("actions/workflows/99/runs")


def test_zero_alerts_and_no_open_prs_are_explicit_and_do_not_mention() -> None:
    github = FakeGitHub(alerts=[], code_alerts=[], pulls=[])
    discord = FakeDiscord()
    run_security_digest(
        environment=environment(),
        github=github,
        discord=discord,
        now=NOW,
    )
    assert fields(discord.messages[0][1])["Open alerts"] == "0"
    assert "Open alertはありません" in fields(discord.messages[0][1])["上位alert"]
    assert all(payload["allowed_mentions"] == {"parse": []} for _, payload in discord.messages)


def test_stale_workflow_and_missing_current_analyses_are_warnings() -> None:
    github = FakeGitHub(
        code_alerts=[],
        analyses=[],
        old_workflows={"dependency-graph.yml", "tool-versions.yml"},
    )
    data = collect_digest(github=github, now=NOW)
    assert len(data.monitoring_warnings) == 4
    rendered = render_security(data=data, now=NOW)
    assert rendered.color == 0xF1C40F
    assert "Dependency Graph" in rendered.fields[-1].value
    assert "CodeQL" in rendered.fields[-1].value
    assert "Grype" in rendered.fields[-1].value


def test_failed_codeql_workflow_is_reported_even_when_analysis_exists() -> None:
    data = collect_digest(github=FakeGitHub(codeql_conclusion="failure"), now=NOW)
    assert "CodeQL workflow: latest main run failure" in data.monitoring_warnings


def test_api_failure_sends_count_free_monitor_failure_then_raises() -> None:
    github = FakeGitHub(fail_path="code-scanning/alerts")
    discord = FakeDiscord()
    with pytest.raises(GitHubApiError, match="simulated"):
        run_security_digest(
            environment=environment(),
            github=github,
            discord=discord,
            now=NOW,
        )
    assert [thread for thread, _ in discord.messages] == ["202"]
    payload = discord.messages[0][1]
    text = str(embed(payload))
    assert "monitoring failed" in text
    assert "Open alerts" not in text
    assert payload["allowed_mentions"] == {"parse": [], "roles": ["301"]}


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "checks:unknown"),
        ([{"status": "in_progress", "conclusion": None}], "checks:pending"),
        ([{"status": "completed", "conclusion": "failure"}], "checks:failed"),
    ],
)
def test_dependabot_check_and_mergeability_states(checks: list[JsonObject], expected: str) -> None:
    github = FakeGitHub(check_runs=checks)
    data = collect_digest(github=github, now=NOW)
    assert data.dependabot_pulls[0].checks == expected
    assert data.dependabot_pulls[0].mergeability == "mergeable/clean"


def test_pull_older_than_seven_days_is_stalled() -> None:
    class StalePullGitHub(FakeGitHub):
        def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject:
            result = super().get_object(path, query=query)
            if path.startswith("pulls/"):
                result["updated_at"] = "2026-07-01T00:00:00Z"
                result["mergeable"] = None
                result["mergeable_state"] = None
            return result

    data = collect_digest(github=StalePullGitHub(), now=NOW)
    assert data.dependabot_pulls[0].stale is True
    assert data.dependabot_pulls[0].mergeability == "unknown/unknown"


def test_top_five_alerts_are_severity_sorted_and_bounded() -> None:
    alerts = [
        dependabot_alert(severity=severity, number=index)
        for index, severity in enumerate(
            ["low", "medium", "high", "critical", "moderate", "high", "low"], start=1
        )
    ]
    data = collect_digest(github=FakeGitHub(alerts=alerts), now=NOW)
    rendered = render_dependabot(data=data, now=NOW)
    top = next(field.value for field in rendered.fields if field.name == "上位alert")
    assert len(top.splitlines()) == 5
    assert top.startswith("critical")
