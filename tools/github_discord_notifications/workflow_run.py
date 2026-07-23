# SPDX-License-Identifier: MIT
"""Workflow-run notification rendering and orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tools.github_discord_notifications.formatting import (
    GREEN,
    DiscordEmbed,
    DiscordField,
    conclusion_presentation,
    join_lines,
    message_payload,
)
from tools.github_discord_notifications.github_api import GitHubApiError, object_value, string_value
from tools.github_discord_notifications.models import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class WorkflowTarget:
    """Trusted workflow identity and Discord destination."""

    path: str
    thread_environment: str
    logical_thread: str
    notify_success: bool = True


WORKFLOW_TARGETS = {
    "CI": WorkflowTarget(".github/workflows/ci.yml", "DISCORD_THREAD_CI", "CI・定期実行"),
    "Dependency Graph": WorkflowTarget(
        ".github/workflows/dependency-graph.yml",
        "DISCORD_THREAD_SECURITY",
        "セキュリティ",
    ),
    "Release Tool Versions": WorkflowTarget(
        ".github/workflows/tool-versions.yml",
        "DISCORD_THREAD_SECURITY",
        "セキュリティ",
    ),
    "Discord Repository Events": WorkflowTarget(
        ".github/workflows/discord-repository-events.yml",
        "DISCORD_THREAD_SECURITY",
        "セキュリティ",
        notify_success=False,
    ),
    "Discord Security Digest": WorkflowTarget(
        ".github/workflows/discord-security-digest.yml",
        "DISCORD_THREAD_SECURITY",
        "セキュリティ",
        notify_success=False,
    ),
}


class GitHubReader(Protocol):
    """The read-only GitHub API operations used by this command."""

    def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject: ...

    def get_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonValue]: ...


class DiscordSender(Protocol):
    """The bounded Discord transport used by this command."""

    def send(self, *, webhook_url: str, thread_id: str, payload: JsonObject) -> None: ...


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Content-free execution result for the GitHub Step Summary."""

    kind: str
    logical_thread: str
    target_url: str


def resolve_workflow_target(run: JsonObject) -> WorkflowTarget | None:
    """Allow only the exact repository workflow name/path pair."""

    name = string_value(run.get("name"))
    target = WORKFLOW_TARGETS.get(name)
    if target is None or string_value(run.get("path")) != target.path:
        return None
    return target


def run_notification(
    *,
    event: JsonObject,
    environment: Mapping[str, str],
    github: GitHubReader,
    discord: DiscordSender,
) -> tuple[NotificationResult, ...]:
    """Render and send a trusted workflow completion and optional Dependabot merge."""

    run = object_value(event.get("workflow_run"), label="workflow_run")
    target = resolve_workflow_target(run)
    if target is None:
        return ()

    conclusion = string_value(run.get("conclusion"), default="unknown")
    if conclusion == "success" and not target.notify_success:
        return ()

    webhook_url = _required(environment, "DISCORD_WEBHOOK_URL")
    thread_id = _required(environment, target.thread_environment)
    role_id = _required(environment, "DISCORD_ALERT_ROLE_ID")
    presentation = conclusion_presentation(conclusion)
    failed_jobs = _failed_job_names(github, run) if conclusion != "success" else ()
    embed = workflow_embed(run, failed_jobs=failed_jobs)
    discord.send(
        webhook_url=webhook_url,
        thread_id=thread_id,
        payload=message_payload(
            embed,
            alert_role_id=role_id if presentation.mention_role else None,
        ),
    )
    results = [
        NotificationResult(
            kind="workflow-run",
            logical_thread=target.logical_thread,
            target_url=string_value(run.get("html_url")),
        )
    ]

    dependabot_pull = _merged_dependabot_pull(github, run)
    if dependabot_pull is not None:
        dependabot_thread = _required(environment, "DISCORD_THREAD_DEPENDABOT")
        discord.send(
            webhook_url=webhook_url,
            thread_id=dependabot_thread,
            payload=message_payload(dependabot_merge_embed(dependabot_pull, run)),
        )
        results.append(
            NotificationResult(
                kind="dependabot-merge",
                logical_thread="Dependabot",
                target_url=string_value(dependabot_pull.get("html_url")),
            )
        )
    return tuple(results)


def workflow_embed(run: JsonObject, *, failed_jobs: tuple[str, ...]) -> DiscordEmbed:
    """Build one bounded workflow completion embed."""

    conclusion = string_value(run.get("conclusion"), default="unknown")
    presentation = conclusion_presentation(conclusion)
    actor = object_value(run.get("actor"), label="workflow_run.actor")
    started = string_value(run.get("run_started_at"), default=string_value(run.get("created_at")))
    completed = string_value(run.get("updated_at"))
    duration = _duration(started, completed)
    run_number = string_value(run.get("run_number"))
    run_attempt = string_value(run.get("run_attempt"), default="1")
    fields = [
        DiscordField("結果", f"{presentation.icon} {conclusion}", True),
        DiscordField("イベント", string_value(run.get("event")), True),
        DiscordField("ブランチ", string_value(run.get("head_branch")), True),
        DiscordField("Commit", string_value(run.get("head_sha"))[:7], True),
        DiscordField("実行者", string_value(actor.get("login")), True),
        DiscordField("実行", f"#{run_number} / attempt {run_attempt}", True),
        DiscordField("開始日時", started, True),
        DiscordField("実行時間", duration, True),
    ]
    if failed_jobs:
        fields.append(DiscordField("失敗・中断ジョブ", join_lines(failed_jobs)))
    fields.append(DiscordField("必要な処置", presentation.action))
    workflow_name = string_value(run.get("name"))
    return DiscordEmbed(
        title=f"{presentation.icon} {workflow_name}: {conclusion}",
        description="GitHub Actionsの実行が完了しました。",
        color=presentation.color,
        url=string_value(run.get("html_url")),
        fields=tuple(fields),
        timestamp=completed,
    )


def dependabot_merge_embed(pull: JsonObject, run: JsonObject) -> DiscordEmbed:
    """Build a trusted notification for a Dependabot PR merged into main."""

    number = string_value(pull.get("number"))
    title = string_value(pull.get("title"))
    merged_at = string_value(pull.get("merged_at"), default=string_value(run.get("updated_at")))
    return DiscordEmbed(
        title=f"✅ Dependabot PR #{number} merged",
        description=title,
        color=GREEN,
        url=string_value(pull.get("html_url")),
        fields=(
            DiscordField("base", _nested_name(pull, "base"), True),
            DiscordField("head", _nested_name(pull, "head"), True),
            DiscordField("必要な処置", "mainのCI結果を確認してください。"),
        ),
        timestamp=merged_at,
    )


def _failed_job_names(github: GitHubReader, run: JsonObject) -> tuple[str, ...]:
    run_id = string_value(run.get("id"), default="")
    attempt = string_value(run.get("run_attempt"), default="1")
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise GitHubApiError("workflow_run id and attempt must be decimal integers")
    response = github.get_object(
        f"actions/runs/{run_id}/attempts/{attempt}/jobs",
        query={"per_page": "100"},
    )
    jobs = response.get("jobs")
    if not isinstance(jobs, list):
        raise GitHubApiError("GitHub workflow jobs response did not contain an array")
    failed: list[str] = []
    for item in jobs:
        if not isinstance(item, dict):
            continue
        job = item
        if string_value(job.get("conclusion"), default="") in {
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        }:
            failed.append(string_value(job.get("name")))
        if len(failed) == 8:
            break
    return tuple(failed)


def _merged_dependabot_pull(github: GitHubReader, run: JsonObject) -> JsonObject | None:
    if not (
        string_value(run.get("name")) == "CI"
        and string_value(run.get("event")) == "push"
        and string_value(run.get("head_branch")) == "main"
    ):
        return None
    sha = string_value(run.get("head_sha"), default="")
    if not sha:
        raise GitHubApiError("CI push workflow did not provide head_sha")
    pulls = github.get_array(f"commits/{sha}/pulls")
    for item in pulls:
        if not isinstance(item, dict):
            continue
        pull = item
        user = pull.get("user")
        if (
            isinstance(user, dict)
            and string_value(user.get("login")) == "dependabot[bot]"
            and pull.get("merged_at") is not None
        ):
            return pull
    return None


def _duration(start: str, end: str) -> str:
    try:
        started = datetime.fromisoformat(start.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(end.replace("Z", "+00:00"))
        seconds = max(0, int((completed - started).total_seconds()))
    except ValueError:
        return "不明"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _nested_name(pull: JsonObject, key: str) -> str:
    value = pull.get(key)
    if not isinstance(value, dict):
        return "—"
    return string_value(value.get("ref"))


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not configured")
    return value


def utc_now() -> str:
    """Return a Discord-compatible UTC timestamp for failure summaries."""

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
