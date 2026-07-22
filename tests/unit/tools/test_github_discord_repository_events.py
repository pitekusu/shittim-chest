"""Tests for metadata-only repository event notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from tools.github_discord_notifications.github_api import GitHubApiError
from tools.github_discord_notifications.models import JsonObject, JsonValue
from tools.github_discord_notifications.repository_events import (
    classify_push,
    notify_pull_request,
    notify_push,
    pull_request_embed,
)


def pull_request(*, login: str = "contributor", merged: bool = False) -> JsonObject:
    return {
        "number": 71,
        "title": "Improve **workflow** @everyone\u202etest",
        "html_url": "https://github.com/example/project/pull/71",
        "user": {"login": login},
        "base": {"ref": "main"},
        "head": {"ref": "feature"},
        "draft": False,
        "merged": merged,
        "merged_at": "2026-07-23T01:00:00Z" if merged else None,
        "closed_at": "2026-07-23T01:00:00Z" if merged else None,
        "updated_at": "2026-07-23T00:00:00Z",
    }


def pr_event(*, action: str = "opened", login: str = "contributor") -> JsonObject:
    return {"action": action, "pull_request": pull_request(login=login, merged=action == "closed")}


def push_event() -> JsonObject:
    return {
        "ref": "refs/heads/main",
        "after": "abcdef0123456789",
        "size": 1,
        "repository": {"html_url": "https://github.com/example/project"},
        "sender": {"login": "maintainer"},
        "head_commit": {
            "message": "Direct **change** @everyone\u202etest",
            "timestamp": "2026-07-23T00:00:00Z",
        },
    }


def environment() -> dict[str, str]:
    return {
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/example/token-placeholder",
        "DISCORD_THREAD_PR": "201",
    }


@dataclass
class FakeDiscord:
    messages: list[tuple[str, JsonObject]] = field(default_factory=list)

    def send(self, *, webhook_url: str, thread_id: str, payload: JsonObject) -> None:
        assert webhook_url.endswith("token-placeholder")
        self.messages.append((thread_id, payload))


@dataclass
class FakeGitHub:
    pulls: list[JsonValue] = field(default_factory=list)
    fail: bool = False
    requests: list[str] = field(default_factory=list)

    def get_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonValue]:
        self.requests.append(path)
        if self.fail:
            raise GitHubApiError("unavailable")
        return self.pulls


def test_pull_request_lifecycle_posts_sanitized_metadata() -> None:
    discord = FakeDiscord()
    results = notify_pull_request(
        event=pr_event(),
        environment=environment(),
        discord=discord,
    )
    assert [result.kind for result in results] == ["pull-request"]
    assert discord.messages[0][0] == "201"
    payload = discord.messages[0][1]
    embed = cast(JsonObject, cast(list[JsonValue], payload["embeds"])[0])
    assert "\\*\\*workflow\\*\\*" in cast(str, embed["description"])
    assert "\u202e" not in cast(str, embed["description"])
    assert payload["allowed_mentions"] == {"parse": []}


def test_dependabot_pull_request_is_skipped_without_configuration() -> None:
    assert (
        notify_pull_request(
            event=pr_event(login="dependabot[bot]"),
            environment={},
            discord=FakeDiscord(),
        )
        == ()
    )


def test_unsupported_pull_request_action_is_skipped() -> None:
    assert (
        notify_pull_request(
            event=pr_event(action="synchronize"),
            environment={},
            discord=FakeDiscord(),
        )
        == ()
    )


def test_merged_and_unmerged_close_have_different_labels() -> None:
    merged = pull_request_embed(action="closed", pull=pull_request(merged=True))
    unmerged = pull_request_embed(action="closed", pull=pull_request(merged=False))
    assert "merged" in merged.title
    assert "closed" in unmerged.title


def test_merge_derived_push_is_suppressed() -> None:
    github = FakeGitHub(pulls=[{"merged_at": "2026-07-23T00:00:00Z"}])
    discord = FakeDiscord()
    assert (
        notify_push(
            event=push_event(),
            environment={},
            github=github,
            discord=discord,
        )
        == ()
    )
    assert github.requests == ["commits/abcdef0123456789/pulls"]
    assert discord.messages == []


def test_direct_push_is_posted_without_mentions() -> None:
    github = FakeGitHub()
    discord = FakeDiscord()
    results = notify_push(
        event=push_event(),
        environment=environment(),
        github=github,
        discord=discord,
    )
    assert [result.kind for result in results] == ["push"]
    payload = discord.messages[0][1]
    assert payload["allowed_mentions"] == {"parse": []}
    embed = cast(JsonObject, cast(list[JsonValue], payload["embeds"])[0])
    fields = cast(list[JsonValue], embed["fields"])
    message = next(
        cast(str, cast(JsonObject, field)["value"])
        for field in fields
        if cast(JsonObject, field)["name"] == "メッセージ"
    )
    assert "\\*\\*change\\*\\*" in message
    assert "\u202e" not in message


def test_push_api_failure_posts_yellow_origin_unknown() -> None:
    github = FakeGitHub(fail=True)
    discord = FakeDiscord()
    results = notify_push(
        event=push_event(),
        environment=environment(),
        github=github,
        discord=discord,
    )
    assert [result.kind for result in results] == ["push-origin-unknown"]
    embed = cast(JsonObject, cast(list[JsonValue], discord.messages[0][1]["embeds"])[0])
    assert embed["color"] == 0xF1C40F
    assert "判定できません" in cast(str, embed["description"])


def test_non_main_push_is_skipped_without_api_or_configuration() -> None:
    event = push_event()
    event["ref"] = "refs/heads/feature"
    assert (
        notify_push(event=event, environment={}, github=FakeGitHub(), discord=FakeDiscord()) == ()
    )


def test_classify_push_ignores_unknown_array_members() -> None:
    result = classify_push(github=FakeGitHub(pulls=[None, "invalid", {}]), commit_sha="abc")
    assert result.api_available is True
    assert result.merge_derived is False
