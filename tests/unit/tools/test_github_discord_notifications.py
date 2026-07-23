"""Tests for trusted GitHub Actions to Discord workflow notifications."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from tools.github_discord_notifications.formatting import (
    EMBED_TOTAL_LIMIT,
    RED,
    DiscordEmbed,
    DiscordField,
    conclusion_presentation,
    embed_payload,
    message_payload,
    sanitize_text,
)
from tools.github_discord_notifications.github_api import GitHubApiError, GitHubClient, _next_link
from tools.github_discord_notifications.models import CurlResult, JsonObject, JsonValue
from tools.github_discord_notifications.webhook import (
    MAX_ATTEMPTS,
    DiscordWebhookError,
    DiscordWebhookSender,
)
from tools.github_discord_notifications.workflow_run import (
    dependabot_merge_embed,
    resolve_workflow_target,
    run_notification,
    workflow_embed,
)


def test_notification_package_parses_with_github_runner_python_baseline() -> None:
    package = Path("tools/github_discord_notifications")
    for source in sorted(package.glob("*.py")):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source), feature_version=(3, 12))


def workflow_run(
    *,
    name: str = "CI",
    path: str = ".github/workflows/ci.yml",
    conclusion: str = "success",
    event: str = "pull_request",
    branch: str = "feature",
) -> JsonObject:
    return {
        "id": 321,
        "name": name,
        "path": path,
        "conclusion": conclusion,
        "event": event,
        "head_branch": branch,
        "head_sha": "abcdef0123456789",
        "actor": {"login": "contributor"},
        "run_number": 9,
        "run_attempt": 2,
        "run_started_at": "2026-07-23T00:00:00Z",
        "created_at": "2026-07-23T00:00:00Z",
        "updated_at": "2026-07-23T00:02:05Z",
        "html_url": "https://github.com/example/project/actions/runs/321",
    }


def event_payload(**kwargs: str) -> JsonObject:
    return {"workflow_run": workflow_run(**kwargs)}


def environment() -> dict[str, str]:
    return {
        "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/example/token-placeholder",
        "DISCORD_ALERT_ROLE_ID": "123",
        "DISCORD_THREAD_CI": "201",
        "DISCORD_THREAD_DEPENDABOT": "202",
        "DISCORD_THREAD_SECURITY": "203",
    }


@dataclass
class FakeGitHub:
    jobs: list[JsonObject] = field(default_factory=list)
    pulls: list[JsonObject] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject:
        self.requests.append(path)
        return {"jobs": cast(list[JsonValue], self.jobs)}

    def get_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonValue]:
        self.requests.append(path)
        return cast(list[JsonValue], self.pulls)


@dataclass
class FakeDiscord:
    messages: list[tuple[str, JsonObject]] = field(default_factory=list)

    def send(self, *, webhook_url: str, thread_id: str, payload: JsonObject) -> None:
        assert "token-placeholder" in webhook_url
        self.messages.append((thread_id, payload))


@pytest.mark.parametrize(
    ("conclusion", "color", "mention"),
    [
        ("success", 0x2ECC71, False),
        ("failure", RED, True),
        ("cancelled", 0xF1C40F, False),
        ("timed_out", RED, True),
        ("action_required", 0xE67E22, True),
        ("neutral", 0x3498DB, False),
    ],
)
def test_conclusion_presentation(conclusion: str, color: int, mention: bool) -> None:
    presentation = conclusion_presentation(conclusion)
    assert presentation.color == color
    assert presentation.mention_role is mention


def test_embed_sanitizes_mentions_markdown_controls_and_limits() -> None:
    hostile = "@everyone **hidden**\u202elink"
    embed = DiscordEmbed(
        title=hostile * 100,
        description=hostile * 500,
        color=RED,
        url="https://github.com/example/project",
        fields=tuple(DiscordField(f"field {index}", hostile * 100) for index in range(30)),
        timestamp="2026-07-23T00:00:00Z",
    )
    payload = embed_payload(embed)
    fields = cast(list[JsonValue], payload["fields"])
    footer = cast(JsonObject, payload["footer"])
    aggregate = (
        len(cast(str, payload["title"]))
        + len(cast(str, payload["description"]))
        + len(cast(str, footer["text"]))
        + sum(
            len(cast(str, cast(JsonObject, item)["name"]))
            + len(cast(str, cast(JsonObject, item)["value"]))
            for item in fields
        )
    )
    assert len(fields) <= 25
    assert aggregate <= EMBED_TOTAL_LIMIT
    assert "\\*\\*hidden\\*\\*" in cast(str, payload["description"])
    assert "\u202e" not in cast(str, payload["description"])
    assert "�" in cast(str, payload["description"])


def test_message_mentions_only_the_explicit_role() -> None:
    embed = workflow_embed(workflow_run(conclusion="failure"), failed_jobs=("tests",))
    without_role = message_payload(embed)
    with_role = message_payload(embed, alert_role_id="123")
    assert without_role["allowed_mentions"] == {"parse": []}
    assert "content" not in without_role
    assert with_role["content"] == "<@&123>"
    assert with_role["allowed_mentions"] == {"parse": [], "roles": ["123"]}
    with pytest.raises(ValueError, match="decimal digits"):
        message_payload(embed, alert_role_id="not-a-number")


def test_sanitize_text_preserves_no_raw_discord_markdown() -> None:
    assert sanitize_text("[label](url) @here") == r"\[label\]\(url\) @here"


def test_sanitize_text_preserves_newlines_and_readable_dependency_versions() -> None:
    value = "first\nsecond\r\nhigh | brace-expansion | — | 5.0.7"
    assert sanitize_text(value) == "first\nsecond\nhigh | brace-expansion | — | 5.0.7"


def test_workflow_start_time_is_rendered_in_japan_standard_time() -> None:
    embed = workflow_embed(workflow_run(), failed_jobs=())
    started = next(field.value for field in embed.fields if field.name == "開始日時")
    assert started == "2026-07-23 09:00:00 JST"


def test_workflow_name_and_path_must_both_match() -> None:
    assert resolve_workflow_target(workflow_run()) is not None
    assert resolve_workflow_target(workflow_run(path="dynamic/dependabot/update-graph")) is None
    assert resolve_workflow_target(workflow_run(name="Unknown")) is None


def test_notification_workflow_success_is_suppressed_but_failure_is_reported() -> None:
    success = event_payload(
        name="Discord Security Digest",
        path=".github/workflows/discord-security-digest.yml",
    )
    assert (
        run_notification(
            event=success,
            environment={},
            github=FakeGitHub(),
            discord=FakeDiscord(),
        )
        == ()
    )
    discord = FakeDiscord()
    run_notification(
        event=event_payload(
            name="Discord Repository Events",
            path=".github/workflows/discord-repository-events.yml",
            conclusion="failure",
        ),
        environment=environment(),
        github=FakeGitHub(jobs=[{"name": "notify", "conclusion": "failure"}]),
        discord=discord,
    )
    assert [thread for thread, _ in discord.messages] == ["203"]


def test_successful_workflow_sends_no_alert_mention() -> None:
    discord = FakeDiscord()
    results = run_notification(
        event=event_payload(),
        environment=environment(),
        github=FakeGitHub(),
        discord=discord,
    )
    assert [result.kind for result in results] == ["workflow-run"]
    assert [thread for thread, _ in discord.messages] == ["201"]
    assert discord.messages[0][1]["allowed_mentions"] == {"parse": []}


def test_failure_lists_at_most_eight_failed_jobs_and_mentions_role() -> None:
    github = FakeGitHub(
        jobs=[{"name": f"failed-{index}", "conclusion": "failure"} for index in range(10)]
        + [{"name": "success", "conclusion": "success"}]
    )
    discord = FakeDiscord()
    run_notification(
        event=event_payload(conclusion="failure"),
        environment=environment(),
        github=github,
        discord=discord,
    )
    embed = cast(JsonObject, cast(list[JsonValue], discord.messages[0][1]["embeds"])[0])
    fields = cast(list[JsonValue], embed["fields"])
    failed = next(
        cast(str, cast(JsonObject, field)["value"])
        for field in fields
        if cast(JsonObject, field)["name"] == "失敗・中断ジョブ"
    )
    assert failed.count("failed") == 8
    assert discord.messages[0][1]["allowed_mentions"] == {"parse": [], "roles": ["123"]}
    assert github.requests == ["actions/runs/321/attempts/2/jobs"]


def test_failure_without_configured_role_disables_all_mentions() -> None:
    configured = environment()
    configured.pop("DISCORD_ALERT_ROLE_ID")
    discord = FakeDiscord()
    run_notification(
        event=event_payload(conclusion="failure"),
        environment=configured,
        github=FakeGitHub(jobs=[{"name": "failed", "conclusion": "failure"}]),
        discord=discord,
    )
    assert discord.messages[0][1]["allowed_mentions"] == {"parse": []}
    assert "content" not in discord.messages[0][1]


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
def test_non_success_conclusions_query_jobs(conclusion: str) -> None:
    github = FakeGitHub(jobs=[{"name": "job", "conclusion": conclusion}])
    discord = FakeDiscord()
    run_notification(
        event=event_payload(conclusion=conclusion),
        environment=environment(),
        github=github,
        discord=discord,
    )
    assert github.requests == ["actions/runs/321/attempts/2/jobs"]


def test_main_ci_dependabot_merge_sends_a_second_message() -> None:
    github = FakeGitHub(
        pulls=[
            {
                "number": 7,
                "title": "deps: update package",
                "html_url": "https://github.com/example/project/pull/7",
                "merged_at": "2026-07-23T00:01:00Z",
                "user": {"login": "dependabot[bot]"},
                "base": {"ref": "main"},
                "head": {"ref": "dependabot/update"},
            }
        ]
    )
    discord = FakeDiscord()
    results = run_notification(
        event=event_payload(event="push", branch="main"),
        environment=environment(),
        github=github,
        discord=discord,
    )
    assert [result.kind for result in results] == ["workflow-run", "dependabot-merge"]
    assert [thread for thread, _ in discord.messages] == ["201", "202"]
    assert github.requests == ["commits/abcdef0123456789/pulls"]


def test_dependabot_merge_renderer_accepts_nullable_nested_fields() -> None:
    embed = dependabot_merge_embed(
        {"number": 1, "title": None, "html_url": None, "merged_at": None},
        workflow_run(),
    )
    assert embed.fields[0].value == "—"
    assert embed.fields[1].value == "—"


def test_missing_configuration_fails_closed() -> None:
    values = environment()
    del values["DISCORD_WEBHOOK_URL"]
    with pytest.raises(ValueError, match="DISCORD_WEBHOOK_URL"):
        run_notification(
            event=event_payload(),
            environment=values,
            github=FakeGitHub(),
            discord=FakeDiscord(),
        )


def test_job_api_shape_failure_is_sanitized() -> None:
    class InvalidGitHub(FakeGitHub):
        def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject:
            return {"jobs": None}

    with pytest.raises(GitHubApiError, match="jobs response"):
        run_notification(
            event=event_payload(conclusion="failure"),
            environment=environment(),
            github=InvalidGitHub(),
            discord=FakeDiscord(),
        )


def test_next_link_accepts_only_the_next_relation() -> None:
    header = (
        '<https://api.github.com/items?page=1>; rel="prev", '
        '<https://api.github.com/items?page=3>; rel="next"'
    )
    assert _next_link(header) == "https://api.github.com/items?page=3"
    assert _next_link(None) is None


def test_keyed_pagination_reads_every_link_page(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GitHubClient(token="token-placeholder", repository="example/project")  # noqa: S106
    pages: list[tuple[JsonValue, str | None]] = [
        (
            {"check_runs": [{"id": 1}]},
            "https://api.github.com/repos/example/project/check-runs?page=2",
        ),
        ({"check_runs": [{"id": 2}]}, None),
    ]
    queries: list[dict[str, str] | None] = []

    def get(path: str, *, query: dict[str, str] | None) -> tuple[JsonValue, str | None]:
        queries.append(query)
        return pages.pop(0)

    monkeypatch.setattr(client, "_get", get)
    assert [item["id"] for item in client.paginate_keyed_array("check-runs", key="check_runs")] == [
        1,
        2,
    ]
    assert queries[0] == {"per_page": "100"}
    assert queries[1] == {"page": "2"}


def test_keyed_pagination_rejects_a_missing_array(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GitHubClient(token="token-placeholder", repository="example/project")  # noqa: S106

    def get(path: str, *, query: dict[str, str] | None) -> tuple[JsonValue, str | None]:
        return {"check_runs": None}, None

    monkeypatch.setattr(client, "_get", get)
    with pytest.raises(GitHubApiError, match="lacked array field"):
        tuple(client.paginate_keyed_array("check-runs", key="check_runs"))


@pytest.mark.parametrize("status", [429, 500, 503, 599])
def test_webhook_retries_transient_http_statuses_then_succeeds(status: int) -> None:
    attempts: list[int] = []
    delays: list[float] = []

    def runner(webhook_url: str, thread_id: str, body: bytes) -> CurlResult:
        attempts.append(len(body))
        if len(attempts) < MAX_ATTEMPTS:
            return CurlResult(0, status, (("Retry-After", "0"),))
        return CurlResult(0, 200)

    DiscordWebhookSender(runner=runner, sleeper=delays.append).send(
        webhook_url="https://discord.com/api/webhooks/example/token-placeholder",
        thread_id="123",
        payload={"content": "test"},
    )
    assert len(attempts) == MAX_ATTEMPTS
    assert delays == [0.0, 0.0, 0.0]


def test_webhook_does_not_retry_a_client_error() -> None:
    attempts = 0

    def runner(webhook_url: str, thread_id: str, body: bytes) -> CurlResult:
        nonlocal attempts
        attempts += 1
        return CurlResult(0, 400)

    with pytest.raises(DiscordWebhookError, match="HTTP 400"):
        DiscordWebhookSender(runner=runner).send(
            webhook_url="https://discord.com/api/webhooks/example/token-placeholder",
            thread_id="123",
            payload={"content": "test"},
        )
    assert attempts == 1


def test_webhook_retry_is_finite_for_transport_failure() -> None:
    attempts = 0

    def runner(webhook_url: str, thread_id: str, body: bytes) -> CurlResult:
        nonlocal attempts
        attempts += 1
        return CurlResult(28, None)

    with pytest.raises(DiscordWebhookError, match="transport failed"):
        DiscordWebhookSender(runner=runner, sleeper=lambda _: None).send(
            webhook_url="https://discord.com/api/webhooks/example/token-placeholder",
            thread_id="123",
            payload={"content": "test"},
        )
    assert attempts == MAX_ATTEMPTS


def test_webhook_rejects_non_discord_url_without_echoing_it() -> None:
    webhook = "https://example.com/api/webhooks/value"
    with pytest.raises(DiscordWebhookError, match="invalid") as captured:
        DiscordWebhookSender().send(
            webhook_url=webhook,
            thread_id="123",
            payload={"content": "test"},
        )
    assert webhook not in str(captured.value)
