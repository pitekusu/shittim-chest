# SPDX-License-Identifier: MIT
"""Trusted rendering for pull-request metadata and main-branch pushes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from tools.github_discord_notifications.formatting import (
    GREEN,
    PURPLE,
    YELLOW,
    DiscordEmbed,
    DiscordField,
    message_payload,
)
from tools.github_discord_notifications.github_api import GitHubApiError, object_value, string_value
from tools.github_discord_notifications.models import JsonObject, JsonValue
from tools.github_discord_notifications.workflow_run import DiscordSender, NotificationResult


class PullRequestReader(Protocol):
    """Read-only GitHub operation used to classify a push."""

    def get_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonValue]: ...


@dataclass(frozen=True, slots=True)
class PushClassification:
    """Result of associating one main commit with merged pull requests."""

    merge_derived: bool
    api_available: bool


def notify_pull_request(
    *,
    event: JsonObject,
    environment: Mapping[str, str],
    discord: DiscordSender,
) -> tuple[NotificationResult, ...]:
    """Post one metadata-only pull-request lifecycle notification."""

    pull = object_value(event.get("pull_request"), label="pull_request")
    user = object_value(pull.get("user"), label="pull_request.user")
    if string_value(user.get("login")) == "dependabot[bot]":
        return ()
    action = string_value(event.get("action"), default="unknown")
    if action not in {"opened", "reopened", "ready_for_review", "closed"}:
        return ()
    webhook_url = _required(environment, "DISCORD_WEBHOOK_URL")
    thread_id = _required(environment, "DISCORD_THREAD_PR")
    embed = pull_request_embed(action=action, pull=pull)
    discord.send(
        webhook_url=webhook_url,
        thread_id=thread_id,
        payload=message_payload(embed),
    )
    return (
        NotificationResult(
            kind="pull-request",
            logical_thread="PR・マージ",
            target_url=string_value(pull.get("html_url")),
        ),
    )


def notify_push(
    *,
    event: JsonObject,
    environment: Mapping[str, str],
    github: PullRequestReader,
    discord: DiscordSender,
) -> tuple[NotificationResult, ...]:
    """Suppress merge pushes and post direct or unclassified main pushes."""

    if string_value(event.get("ref"), default="") != "refs/heads/main":
        return ()
    after = string_value(event.get("after"), default="")
    if not after:
        raise GitHubApiError("push event did not provide an after commit")
    classification = classify_push(github=github, commit_sha=after)
    if classification.merge_derived:
        return ()
    webhook_url = _required(environment, "DISCORD_WEBHOOK_URL")
    thread_id = _required(environment, "DISCORD_THREAD_PR")
    embed = push_embed(event=event, api_available=classification.api_available)
    discord.send(
        webhook_url=webhook_url,
        thread_id=thread_id,
        payload=message_payload(embed),
    )
    return (
        NotificationResult(
            kind="push" if classification.api_available else "push-origin-unknown",
            logical_thread="PR・マージ",
            target_url=embed.url,
        ),
    )


def classify_push(*, github: PullRequestReader, commit_sha: str) -> PushClassification:
    """Classify a commit using GitHub's associated-pulls API."""

    try:
        pulls = github.get_array(f"commits/{commit_sha}/pulls")
    except GitHubApiError:
        return PushClassification(merge_derived=False, api_available=False)
    for value in pulls:
        if not isinstance(value, dict):
            continue
        if value.get("merged_at") is not None:
            return PushClassification(merge_derived=True, api_available=True)
    return PushClassification(merge_derived=False, api_available=True)


def pull_request_embed(*, action: str, pull: JsonObject) -> DiscordEmbed:
    """Build one pull-request lifecycle embed from GitHub metadata."""

    merged = pull.get("merged_at") is not None or pull.get("merged") is True
    action_label = "merged" if action == "closed" and merged else action
    presentation = {
        "opened": ("🔀", PURPLE, "レビューとCIを確認してください。"),
        "reopened": ("🔄", YELLOW, "再開理由とCIを確認してください。"),
        "ready_for_review": ("👀", PURPLE, "レビュー可能です。"),
        "merged": ("✅", GREEN, "マージ後のmain CIを確認してください。"),
        "closed": ("⚪", YELLOW, "未マージcloseです。必要なら理由を確認してください。"),
    }
    icon, color, required_action = presentation[action_label]
    user = object_value(pull.get("user"), label="pull_request.user")
    return DiscordEmbed(
        title=f"{icon} PR #{string_value(pull.get('number'))}: {action_label}",
        description=string_value(pull.get("title")),
        color=color,
        url=string_value(pull.get("html_url")),
        fields=(
            DiscordField("作成者", string_value(user.get("login")), True),
            DiscordField("base", _nested_ref(pull, "base"), True),
            DiscordField("head", _nested_ref(pull, "head"), True),
            DiscordField("draft", string_value(pull.get("draft")), True),
            DiscordField("必要な処置", required_action),
        ),
        timestamp=string_value(
            pull.get("merged_at") or pull.get("closed_at") or pull.get("updated_at")
        ),
    )


def push_embed(*, event: JsonObject, api_available: bool) -> DiscordEmbed:
    """Build one direct or unclassified main push embed."""

    repository = object_value(event.get("repository"), label="repository")
    sender = object_value(event.get("sender"), label="sender")
    commit = event.get("head_commit")
    commit_object = commit if isinstance(commit, dict) else {}
    after = string_value(event.get("after"))
    repository_url = string_value(repository.get("html_url"))
    commit_url = f"{repository_url}/commit/{after}" if repository_url != "—" else repository_url
    if api_available:
        icon = "⬆️"
        color = PURPLE
        description = "mainへの直接pushを検知しました。"
        required_action = "Rulesetと変更内容を確認してください。"
    else:
        icon = "⚠️"
        color = YELLOW
        description = "main pushのPR由来をGitHub APIで判定できませんでした。"
        required_action = "GitHubでcommitと関連PRを確認してください。"
    return DiscordEmbed(
        title=f"{icon} main push",
        description=description,
        color=color,
        url=commit_url,
        fields=(
            DiscordField("Commit", after[:7], True),
            DiscordField("実行者", string_value(sender.get("login")), True),
            DiscordField("変更", string_value(event.get("size")), True),
            DiscordField("メッセージ", string_value(commit_object.get("message"))),
            DiscordField("必要な処置", required_action),
        ),
        timestamp=string_value(commit_object.get("timestamp")),
    )


def _nested_ref(pull: JsonObject, key: str) -> str:
    value = pull.get(key)
    return string_value(value.get("ref")) if isinstance(value, dict) else "—"


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not configured")
    return value
