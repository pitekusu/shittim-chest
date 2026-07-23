# SPDX-License-Identifier: MIT
"""Daily Dependabot and code-scanning digest collection and rendering."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from tools.github_discord_notifications.formatting import (
    GREEN,
    RED,
    YELLOW,
    DiscordEmbed,
    DiscordField,
    join_lines,
    message_payload,
)
from tools.github_discord_notifications.github_api import GitHubApiError, object_value, string_value
from tools.github_discord_notifications.models import JsonObject
from tools.github_discord_notifications.workflow_run import DiscordSender, NotificationResult

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "moderate": 2, "low": 3}
FRESHNESS_DAYS = 8
STALE_PULL_DAYS = 7


class DigestGitHubReader(Protocol):
    """Read-only API surface needed for the security digest."""

    def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject: ...

    def paginate_array(
        self, path: str, *, query: dict[str, str] | None = None
    ) -> Iterable[JsonObject]: ...

    def paginate_keyed_array(
        self,
        path: str,
        *,
        key: str,
        query: dict[str, str] | None = None,
    ) -> Iterable[JsonObject]: ...


@dataclass(frozen=True, slots=True)
class DependabotPull:
    number: str
    title: str
    url: str
    updated_at: str
    mergeability: str
    checks: str
    stale: bool


@dataclass(frozen=True, slots=True)
class DigestData:
    dependabot_alerts: tuple[JsonObject, ...]
    code_alerts: tuple[JsonObject, ...]
    dependabot_pulls: tuple[DependabotPull, ...]
    monitoring_warnings: tuple[str, ...]
    main_sha: str


def run_security_digest(
    *,
    environment: Mapping[str, str],
    github: DigestGitHubReader,
    discord: DiscordSender,
    now: datetime | None = None,
) -> tuple[NotificationResult, ...]:
    """Collect complete data, then send exactly one embed to each digest thread."""

    webhook_url = _required(environment, "DISCORD_WEBHOOK_URL")
    dependabot_thread = _required(environment, "DISCORD_THREAD_DEPENDABOT")
    security_thread = _required(environment, "DISCORD_THREAD_SECURITY")
    role_id = environment.get("DISCORD_ALERT_ROLE_ID", "").strip() or None
    timestamp = now or datetime.now(UTC)
    try:
        data = collect_digest(github=github, now=timestamp)
    except GitHubApiError:
        discord.send(
            webhook_url=webhook_url,
            thread_id=security_thread,
            payload=message_payload(monitor_failure_embed(timestamp), alert_role_id=role_id),
        )
        raise

    dependabot_embed = render_dependabot(data=data, now=timestamp)
    dependabot_mention = _has_high_or_critical(data.dependabot_alerts)
    discord.send(
        webhook_url=webhook_url,
        thread_id=dependabot_thread,
        payload=message_payload(
            dependabot_embed,
            alert_role_id=role_id if dependabot_mention else None,
        ),
    )
    security_embed = render_security(data=data, now=timestamp)
    security_mention = _has_high_or_critical(data.code_alerts) or bool(data.monitoring_warnings)
    discord.send(
        webhook_url=webhook_url,
        thread_id=security_thread,
        payload=message_payload(
            security_embed,
            alert_role_id=role_id if security_mention else None,
        ),
    )
    return (
        NotificationResult("security-digest", "Dependabot", dependabot_embed.url),
        NotificationResult("security-digest", "セキュリティ", security_embed.url),
    )


def collect_digest(*, github: DigestGitHubReader, now: datetime) -> DigestData:
    """Fetch every required page before allowing any count to be rendered."""

    dependabot_alerts = tuple(github.paginate_array("dependabot/alerts", query={"state": "open"}))
    code_alerts = tuple(github.paginate_array("code-scanning/alerts", query={"state": "open"}))
    open_pulls = tuple(github.paginate_array("pulls", query={"state": "open"}))
    dependabot_pulls = tuple(
        _pull_status(github=github, pull=pull, now=now)
        for pull in open_pulls
        if _login(pull) == "dependabot[bot]"
    )
    main = github.get_object("commits/main")
    main_sha = string_value(main.get("sha"), default="")
    if not main_sha:
        raise GitHubApiError("main commit response did not contain sha")
    analyses = tuple(
        github.paginate_array("code-scanning/analyses", query={"ref": "refs/heads/main"})
    )
    warnings = list(_workflow_freshness_warnings(github=github, now=now))
    warnings.extend(_codeql_workflow_warnings(github=github, now=now))
    warnings.extend(_analysis_warnings(analyses=analyses, main_sha=main_sha))
    return DigestData(
        dependabot_alerts=dependabot_alerts,
        code_alerts=code_alerts,
        dependabot_pulls=dependabot_pulls,
        monitoring_warnings=tuple(warnings),
        main_sha=main_sha,
    )


def render_dependabot(*, data: DigestData, now: datetime) -> DiscordEmbed:
    severities = _severity_counts(data.dependabot_alerts)
    stalled = sum(pull.stale for pull in data.dependabot_pulls)
    fields = [
        DiscordField("Open alerts", str(len(data.dependabot_alerts)), True),
        DiscordField("Open PR", str(len(data.dependabot_pulls)), True),
        DiscordField("7日超停滞PR", str(stalled), True),
        DiscordField("Severity", _render_counter(severities)),
    ]
    top_alerts = _top_dependabot_alerts(data.dependabot_alerts)
    fields.append(
        DiscordField("上位alert", join_lines(top_alerts, empty="Open alertはありません。"))
    )
    pull_lines = (
        f"#{pull.number} {pull.title} | {pull.checks} | {pull.mergeability}"
        + (" | 7日超" if pull.stale else "")
        for pull in data.dependabot_pulls[:5]
    )
    fields.append(
        DiscordField("Dependabot PR", join_lines(pull_lines, empty="Open PRはありません。"))
    )
    critical = _has_high_or_critical(data.dependabot_alerts)
    return DiscordEmbed(
        title="🤖 Dependabot daily digest",
        description="Open alertと更新PRの完全取得後の集計です。",
        color=RED if critical else (YELLOW if data.dependabot_alerts else GREEN),
        url="https://github.com/pitekusu/shittim-chest/security/dependabot",
        fields=tuple(fields),
        timestamp=_timestamp(now),
    )


def render_security(*, data: DigestData, now: datetime) -> DiscordEmbed:
    severities = _severity_counts(data.code_alerts)
    tools = Counter(_code_tool(alert) for alert in data.code_alerts)
    fields = [
        DiscordField("Open code scanning alerts", str(len(data.code_alerts)), True),
        DiscordField("main", data.main_sha[:7], True),
        DiscordField("Severity", _render_counter(severities)),
        DiscordField("Tool", _render_counter(tools)),
        DiscordField(
            "上位alert",
            join_lines(_top_code_alerts(data.code_alerts), empty="Open alertはありません。"),
        ),
        DiscordField(
            "監視状態",
            join_lines(data.monitoring_warnings, empty="停止・鮮度警告はありません。"),
        ),
    ]
    critical = _has_high_or_critical(data.code_alerts)
    color = RED if critical else (YELLOW if data.monitoring_warnings else GREEN)
    return DiscordEmbed(
        title="🛡️ Security daily digest",
        description="CodeQL・Grype・定期workflowの状態です。",
        color=color,
        url="https://github.com/pitekusu/shittim-chest/security/code-scanning",
        fields=tuple(fields),
        timestamp=_timestamp(now),
    )


def monitor_failure_embed(now: datetime) -> DiscordEmbed:
    """Render a count-free failure because partial totals are misleading."""

    return DiscordEmbed(
        title="🚨 Security digest monitoring failed",
        description="GitHub APIの必須データを完全に取得できず、不完全な件数は送信していません。",
        color=RED,
        url="https://github.com/pitekusu/shittim-chest/actions/workflows/discord-security-digest.yml",
        fields=(
            DiscordField("必要な処置", "Actions log、API権限、GitHub側障害を確認してください。"),
        ),
        timestamp=_timestamp(now),
    )


def _pull_status(*, github: DigestGitHubReader, pull: JsonObject, now: datetime) -> DependabotPull:
    number = string_value(pull.get("number"), default="")
    if not number.isdecimal():
        raise GitHubApiError("Dependabot pull response did not contain a number")
    detail = github.get_object(f"pulls/{number}")
    head = object_value(detail.get("head"), label="pull_request.head")
    sha = string_value(head.get("sha"), default="")
    if not sha:
        raise GitHubApiError("Dependabot pull response did not contain head sha")
    checks = tuple(github.paginate_keyed_array(f"commits/{sha}/check-runs", key="check_runs"))
    updated_at = string_value(detail.get("updated_at"))
    return DependabotPull(
        number=number,
        title=string_value(detail.get("title")),
        url=string_value(detail.get("html_url")),
        updated_at=updated_at,
        mergeability=_mergeability(detail),
        checks=_check_state(checks),
        stale=_older_than(updated_at, now=now, days=STALE_PULL_DAYS),
    )


def _workflow_freshness_warnings(*, github: DigestGitHubReader, now: datetime) -> tuple[str, ...]:
    warnings: list[str] = []
    for name, filename in (
        ("Dependency Graph", "dependency-graph.yml"),
        ("Release Tool Versions", "tool-versions.yml"),
    ):
        response = github.get_object(
            f"actions/workflows/{filename}/runs",
            query={"branch": "main", "status": "success", "per_page": "1"},
        )
        runs = response.get("workflow_runs")
        if not isinstance(runs, list):
            raise GitHubApiError("workflow runs response did not contain workflow_runs")
        if not runs or not isinstance(runs[0], dict):
            warnings.append(f"{name}: 成功runなし")
            continue
        completed = string_value(runs[0].get("updated_at"))
        if _older_than(completed, now=now, days=FRESHNESS_DAYS):
            warnings.append(f"{name}: {FRESHNESS_DAYS}日以内の成功runなし")
    return tuple(warnings)


def _analysis_warnings(*, analyses: tuple[JsonObject, ...], main_sha: str) -> tuple[str, ...]:
    codeql = [analysis for analysis in analyses if _analysis_tool(analysis) == "codeql"]
    grype = [analysis for analysis in analyses if _analysis_tool(analysis) == "grype"]
    warnings: list[str] = []
    current_codeql = [analysis for analysis in codeql if analysis.get("commit_sha") == main_sha]
    if not current_codeql:
        warnings.append("CodeQL: 現在のmainのanalysisなし")
    elif any(string_value(analysis.get("error"), default="") for analysis in current_codeql):
        warnings.append("CodeQL: 現在のmainのanalysis失敗")
    if not any(analysis.get("commit_sha") == main_sha for analysis in grype):
        warnings.append("Grype: 現在のmainのanalysisなし")
    return tuple(warnings)


def _codeql_workflow_warnings(*, github: DigestGitHubReader, now: datetime) -> tuple[str, ...]:
    workflows = tuple(github.paginate_keyed_array("actions/workflows", key="workflows"))
    codeql = [
        workflow
        for workflow in workflows
        if string_value(workflow.get("name")).casefold() == "codeql"
        or string_value(workflow.get("path"), default="").startswith(
            "dynamic/github-code-scanning/codeql"
        )
    ]
    if not codeql:
        return ("CodeQL workflow: 検出できず",)
    warnings: list[str] = []
    for workflow in codeql:
        workflow_id = string_value(workflow.get("id"), default="")
        if not workflow_id.isdecimal():
            raise GitHubApiError("CodeQL workflow response did not contain an id")
        response = github.get_object(
            f"actions/workflows/{workflow_id}/runs",
            query={"branch": "main", "per_page": "1"},
        )
        runs = response.get("workflow_runs")
        if not isinstance(runs, list):
            raise GitHubApiError("CodeQL workflow runs response lacked workflow_runs")
        if not runs or not isinstance(runs[0], dict):
            warnings.append("CodeQL workflow: main runなし")
            continue
        latest = runs[0]
        conclusion = string_value(latest.get("conclusion"), default="unknown")
        completed = string_value(latest.get("updated_at"))
        if conclusion != "success":
            warnings.append(f"CodeQL workflow: latest main run {conclusion}")
        elif _older_than(completed, now=now, days=FRESHNESS_DAYS):
            warnings.append(f"CodeQL workflow: {FRESHNESS_DAYS}日以内のmain runなし")
    return tuple(warnings)


def _mergeability(pull: JsonObject) -> str:
    mergeable = pull.get("mergeable")
    state = string_value(pull.get("mergeable_state"), default="unknown")
    if mergeable is True:
        return f"mergeable/{state}"
    if mergeable is False:
        return f"blocked/{state}"
    return f"unknown/{state}"


def _check_state(checks: tuple[JsonObject, ...]) -> str:
    if not checks:
        return "checks:unknown"
    if any(string_value(check.get("status"), default="") != "completed" for check in checks):
        return "checks:pending"
    accepted = {"success", "neutral", "skipped"}
    if any(string_value(check.get("conclusion"), default="") not in accepted for check in checks):
        return "checks:failed"
    return "checks:passed"


def _top_dependabot_alerts(alerts: tuple[JsonObject, ...]) -> tuple[str, ...]:
    lines: list[str] = []
    for alert in sorted(alerts, key=lambda value: _severity_rank(_dependabot_severity(value)))[:5]:
        dependency = alert.get("dependency")
        package = dependency.get("package") if isinstance(dependency, dict) else None
        package_name = string_value(package.get("name")) if isinstance(package, dict) else "—"
        vulnerable = string_value(alert.get("vulnerable_manifest_path"), default="—")
        fixed = alert.get("security_vulnerability")
        patched = fixed.get("first_patched_version") if isinstance(fixed, dict) else None
        fixed_version = (
            string_value(patched.get("identifier")) if isinstance(patched, dict) else "修正版なし"
        )
        lines.append(
            f"{_dependabot_severity(alert)} | {package_name} | {vulnerable} | {fixed_version}"
        )
    return tuple(lines)


def _top_code_alerts(alerts: tuple[JsonObject, ...]) -> tuple[str, ...]:
    lines: list[str] = []
    for alert in sorted(alerts, key=lambda value: _severity_rank(_code_severity(value)))[:5]:
        rule = alert.get("rule")
        description = string_value(rule.get("description")) if isinstance(rule, dict) else "—"
        number = string_value(alert.get("number"))
        lines.append(f"{_code_severity(alert)} | {_code_tool(alert)} | #{number} | {description}")
    return tuple(lines)


def _dependabot_severity(alert: JsonObject) -> str:
    advisory = alert.get("security_advisory")
    if isinstance(advisory, dict):
        return string_value(advisory.get("severity"), default="unknown").casefold()
    return "unknown"


def _code_severity(alert: JsonObject) -> str:
    rule = alert.get("rule")
    if not isinstance(rule, dict):
        return "unknown"
    return string_value(
        rule.get("security_severity_level"),
        default=string_value(rule.get("severity"), default="unknown"),
    ).casefold()


def _code_tool(alert: JsonObject) -> str:
    tool = alert.get("tool")
    return (
        string_value(tool.get("name"), default="unknown") if isinstance(tool, dict) else "unknown"
    )


def _analysis_tool(analysis: JsonObject) -> str:
    tool = analysis.get("tool")
    return (
        string_value(tool.get("name"), default="unknown").casefold()
        if isinstance(tool, dict)
        else "unknown"
    )


def _severity_counts(alerts: tuple[JsonObject, ...]) -> Counter[str]:
    return Counter(
        _dependabot_severity(alert) if "security_advisory" in alert else _code_severity(alert)
        for alert in alerts
    )


def _has_high_or_critical(alerts: tuple[JsonObject, ...]) -> bool:
    return any(severity in {"critical", "high"} for severity in _severity_counts(alerts))


def _severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 9)


def _render_counter(counter: Counter[str]) -> str:
    if not counter:
        return "0"
    ordered = sorted(counter.items(), key=lambda item: (_severity_rank(item[0]), item[0]))
    return " / ".join(f"{key}:{value}" for key, value in ordered)


def _login(pull: JsonObject) -> str:
    user = pull.get("user")
    return string_value(user.get("login")) if isinstance(user, dict) else "—"


def _older_than(value: str, *, now: datetime, days: int) -> bool:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return timestamp < now - timedelta(days=days)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not configured")
    return value
