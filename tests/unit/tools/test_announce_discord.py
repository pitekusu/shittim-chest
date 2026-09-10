"""Safety boundaries for the manual announcement CLI; no AWS or Discord network."""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import boto3
import httpx
import pytest
from tools import announce_discord as notice

from shittim_chest.application.discord import (
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.config.status_publisher import MODERATOR_TOKEN_PARAMETER


@pytest.fixture
def runtime() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id="1",
        allowed_channel_ids=frozenset({"2"}),
        farewell_channel_id="2",
        identities=tuple(
            DiscordIdentityConfig(slot, str(index))
            for index, slot in enumerate(DiscordBotSlot, start=11)
        ),
        schema_version="2",
    )


@pytest.fixture
def api() -> Iterator[tuple[httpx.Client, dict[str, object], list[httpx.Request]]]:
    responses: dict[str, object] = {
        "/oauth2/applications/@me": {"id": "11"},
        "/users/@me": {"id": "99", "username": "test-bot", "bot": True},
        "/channels/2": {"id": "2", "guild_id": "1", "name": "test-channel", "type": 0},
        "/channels/2/messages": [],
    }
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path.removeprefix("/api/v10")
        if request.method == "POST":
            payload = json.loads(request.content)
            posted = {**payload, "id": "9", "channel_id": "2", "author": {"id": "99"}, "embeds": []}
            responses.setdefault("/channels/2/messages/9", posted)
            responses["/channels/2/messages"] = [posted]
            return httpx.Response(200, json=posted)
        value = responses[path]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value)

    client = httpx.Client(
        base_url=notice.DISCORD_API_BASE_URL, transport=httpx.MockTransport(handle)
    )
    try:
        yield client, responses, calls
    finally:
        client.close()


def history_message(**changes: object) -> dict[str, object]:
    return {
        "id": "9",
        "channel_id": "2",
        "author": {"id": "99"},
        "content": "告知",
        "nonce": notice.announcement_nonce("test-notice"),
        "embeds": [],
        **changes,
    }


@pytest.mark.parametrize(
    "text,valid",
    [
        ("更新しました。", True),
        ("あ" * 2_000, True),
        ("あ" * 2_001, False),
        (chr(0x1F600) * 1_000, True),
        (chr(0x1F600) * 1_001, False),
        (" \n", False),
        ("x" * 8_001, False),
    ],
)
def test_message_validation(tmp_path: Path, text: str, valid: bool) -> None:
    source = tmp_path / "notice.txt"
    source.write_text(text, encoding="utf-8")
    if valid:
        assert notice.read_message(source) == text
    else:
        with pytest.raises(notice.AnnouncementError):
            notice.read_message(source)


@pytest.mark.parametrize(
    "actual,category", [("1" * 12, None), ("2" * 12, "unexpected_aws_account")]
)
def test_aws_account_before_any_config_read(
    monkeypatch: pytest.MonkeyPatch, actual: str, category: str | None
) -> None:
    session = Mock(spec=boto3.Session)
    session.client.return_value.get_caller_identity.return_value = {"Account": actual}
    constructor = Mock(return_value=session)
    monkeypatch.setattr(notice.boto3, "Session", constructor)
    if category:
        with pytest.raises(notice.AnnouncementError, match=category):
            notice.create_production_session("1" * 12)
    else:
        assert notice.create_production_session("1" * 12) is session
    constructor.assert_called_once_with(region_name=notice.DEFAULT_AWS_REGION)
    session.client.assert_called_once_with("sts", config=notice.AWS_CLIENT_CONFIG)
    expected_config = {
        "retries": {"total_max_attempts": 1, "mode": "standard"},
        "connect_timeout": 5,
        "read_timeout": 10,
    }
    for attribute, expected in expected_config.items():
        assert getattr(notice.AWS_CLIENT_CONFIG, attribute) == expected


def test_account_cannot_be_inferred_from_current_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor = Mock()
    monkeypatch.setattr(notice.boto3, "Session", constructor)
    with pytest.raises(notice.AnnouncementError, match="expected_aws_account_required"):
        notice.create_production_session(None)
    constructor.assert_not_called()


@pytest.mark.parametrize("version", ["v0004", "v0003"])
def test_runtime_version_and_exact_parameters(runtime: DiscordRuntimeConfig, version: str) -> None:
    runtime_name = "/shittim-chest/production/runtime/v0004"
    token_name = MODERATOR_TOKEN_PARAMETER
    session = Mock(spec=boto3.Session)
    lambda_client, ssm = Mock(), Mock()
    session.client.side_effect = lambda service, **kwargs: {"lambda": lambda_client, "ssm": ssm}[
        service
    ]
    lambda_client.get_function_configuration.return_value = {
        "Environment": {
            "Variables": {
                "SHITTIM_DYNAMODB_TABLE": "test-table",
                "SHITTIM_RUNTIME_CONFIG_PARAMETER": runtime_name,
                "SHITTIM_MODERATOR_TOKEN_PARAMETER": token_name,
            }
        }
    }
    payload = {
        "schema_version": "2",
        "config_version": version,
        "guild_id": runtime.guild_id,
        "allowed_channel_ids": sorted(runtime.allowed_channel_ids),
        "farewell_channel_id": runtime.farewell_channel_id,
        "identities": [
            {"slot": item.slot.value, "application_id": item.application_id}
            for item in runtime.identities
        ],
    }
    values = {runtime_name: json.dumps(payload), token_name: "synthetic-secret"}

    def parameter(*, Name: str, WithDecryption: bool) -> dict[str, object]:
        assert WithDecryption is True
        return {"Parameter": {"Name": Name, "Value": values[Name]}}

    ssm.get_parameter.side_effect = parameter
    if version == "v0004":
        assert notice.load_runtime_config(session) == (runtime, "synthetic-secret")
        assert [call.kwargs["Name"] for call in ssm.get_parameter.call_args_list] == [
            runtime_name,
            token_name,
        ]
    else:
        with pytest.raises(notice.AnnouncementError, match="unexpected_runtime_version"):
            notice.load_runtime_config(session)
        assert ssm.get_parameter.call_count == 1


@pytest.mark.parametrize(
    "path,field,value,category",
    [
        ("/oauth2/applications/@me", "id", "12", "unexpected_application_id"),
        ("/users/@me", "bot", False, "unexpected_bot_identity"),
        ("/users/@me", "bot", 1, "unexpected_bot_identity"),
        ("/channels/2", "guild_id", "3", "invalid_target_channel"),
        ("/channels/2", "id", "3", "invalid_target_channel"),
        ("/channels/2", "type", 11, "invalid_target_channel"),
        ("/channels/2", "type", False, "invalid_target_channel"),
    ],
)
def test_reject_unsafe_identity_and_channel(
    api, runtime: DiscordRuntimeConfig, path: str, field: str, value: object, category: str
) -> None:
    client, responses, calls = api
    responses[path][field] = value
    with pytest.raises(notice.AnnouncementError, match=category):
        notice.resolve_target_channel(client, runtime, None)
    assert all(call.method == "GET" for call in calls)


def test_unique_allowed_channel_and_application_not_user_id(
    api, runtime: DiscordRuntimeConfig
) -> None:
    client, responses, _ = api
    for channel_type in (0, 5):
        responses["/channels/2"]["type"] = channel_type
        target = notice.resolve_target_channel(client, runtime, None)
        assert target.channel_id == "2"
        assert target.bot_id == "99"  # Application ID is 11, not the Bot user ID.
    with pytest.raises(notice.AnnouncementError, match="target_channel_ambiguous"):
        notice.resolve_target_channel(client, runtime, "missing")
    multiple = replace(runtime, allowed_channel_ids=frozenset({"2", "3"}))
    responses["/channels/3"] = {"id": "3", "guild_id": "1", "type": 0, "name": "other"}
    with pytest.raises(notice.AnnouncementError, match="target_channel_ambiguous"):
        notice.resolve_target_channel(client, multiple, None)
    assert notice.resolve_target_channel(client, multiple, "other").channel_id == "3"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, True),
        ({"nonce": None}, True),
        (
            {
                "content": "",
                "nonce": None,
                "embeds": [{"title": notice.LEGACY_EMBED_TITLE, "description": "告知"}],
            },
            True,
        ),
        ({"author": {"id": "77"}}, False),
        (
            {"content": "", "nonce": None, "embeds": [{"title": "other", "description": "告知"}]},
            False,
        ),
    ],
)
def test_duplicate_predicate(changes: dict[str, object], expected: bool) -> None:
    assert (
        notice.is_matching_announcement(
            history_message(**changes),
            bot_id="99",
            content="告知",
            nonce=notice.announcement_nonce("test-notice"),
            legacy_embed_title=notice.LEGACY_EMBED_TITLE,
        )
        is expected
    )


def run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime: DiscordRuntimeConfig,
    client: httpx.Client,
    *,
    send: bool = False,
) -> int:
    message_file = tmp_path / "notice.txt"
    message_file.write_text("告知", encoding="utf-8")
    session = Mock(spec=boto3.Session)
    create_session = Mock(return_value=session)
    monkeypatch.setattr(notice, "create_production_session", create_session)
    load_config = Mock(return_value=(runtime, "synthetic-secret"))
    monkeypatch.setattr(notice, "load_runtime_config", load_config)
    monkeypatch.setattr(notice, "create_discord_client", lambda token: client)
    arguments = [
        "--message-file",
        str(message_file),
        "--announcement-id",
        "test-notice",
        "--expected-account-id",
        "1" * 12,
        "--state-dir",
        str(tmp_path / "state"),
    ]
    if send:
        arguments.append("--send")
    result = notice.main(arguments)
    create_session.assert_called_once_with("1" * 12)
    load_config.assert_called_once_with(session)
    return result


@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_still_checks_history_without_posts(
    api, runtime, monkeypatch, tmp_path, capsys, existing: bool
) -> None:
    client, responses, calls = api
    if existing:
        responses["/channels/2/messages"] = [history_message(nonce=None)]
    assert run_cli(monkeypatch, tmp_path, runtime, client) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == ("already_posted" if existing else "ready")
    assert calls[-1].url.path.endswith("/messages")
    assert all(call.method == "GET" for call in calls)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "changes",
    [
        None,
        {"author": {"id": "77"}},
        {"author": None},
        {"content": "changed"},
        {"embeds": [{"title": "unexpected"}]},
        {"embeds": None},
        {"channel_id": "3"},
        {"id": "8"},
    ],
)
def test_post_payload_and_get_verification(
    api, runtime, monkeypatch, tmp_path, capsys, changes
) -> None:
    client, responses, calls = api
    if changes is not None:
        responses["/channels/2/messages/9"] = history_message(**changes)
    assert run_cli(monkeypatch, tmp_path, runtime, client, send=True) == (
        0 if changes is None else 1
    )
    output = capsys.readouterr()
    if changes is None:
        assert json.loads(output.out)["state"] == "posted_and_verified"
    else:
        assert json.loads(output.err)["category"] == "post_verification_failed"
    payload = json.loads(next(call.content for call in calls if call.method == "POST"))
    assert payload == {
        "content": "告知",
        "allowed_mentions": {"parse": []},
        "nonce": notice.announcement_nonce("test-notice"),
        "enforce_nonce": True,
    }
    assert calls[-1].method == "GET"
    assert calls[-1].url.path.endswith("/messages/9")
    assert len(list((tmp_path / "state").iterdir())) == 1


def test_attempt_survives_timeout_and_is_per_announcement(tmp_path: Path) -> None:
    posts = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        raise httpx.ReadTimeout("secret provider details")

    target = notice.AnnouncementTarget("1", "2", "test", "99", "test-bot")
    path = tmp_path / "state" / f"{notice.announcement_nonce('test-notice')}.attempt"
    fingerprint = notice.check_send_attempt(path, target, "告知")
    with httpx.Client(
        base_url=notice.DISCORD_API_BASE_URL, transport=httpx.MockTransport(timeout)
    ) as client:
        for attempt in range(2):
            error_type = httpx.ReadTimeout if attempt == 0 else notice.AnnouncementError
            with pytest.raises(error_type):
                notice.send_announcement(
                    client,
                    target,
                    content="告知",
                    nonce="test",
                    attempt_path=path,
                    fingerprint=fingerprint,
                )
    assert posts == 1
    assert path.read_text() == fingerprint
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(notice.AnnouncementError, match="reused"):
        notice.check_send_attempt(path, target, "changed")
    other = path.with_name(f"{notice.announcement_nonce('another')}.attempt")
    assert notice.check_send_attempt(other, target, "another")
    assert not other.exists()


def test_nonce_collision_is_not_silently_accepted() -> None:
    with pytest.raises(notice.AnnouncementError, match="announcement_id_conflict"):
        notice.is_matching_announcement(
            history_message(content="different"),
            bot_id="99",
            content="告知",
            nonce=notice.announcement_nonce("test-notice"),
            legacy_embed_title=notice.LEGACY_EMBED_TITLE,
        )


@pytest.mark.parametrize(
    "failure,category",
    [
        (httpx.Response(403, text="synthetic-secret"), "discord_http_403"),
        (httpx.Response(302, headers={"Location": "https://example.invalid"}), "discord_http_302"),
        (httpx.Response(200, text="synthetic-secret"), "invalid_discord_response"),
        (httpx.ReadTimeout("synthetic-secret"), "discord_transport_failed"),
    ],
)
def test_safe_error_output_and_logging_restore(
    api, runtime, monkeypatch, tmp_path, capsys, caplog, failure: object, category: str
) -> None:
    client, responses, _ = api
    responses["/channels/2/messages"] = failure
    previous = logging.root.manager.disable
    with caplog.at_level(logging.DEBUG):
        assert run_cli(monkeypatch, tmp_path, runtime, client) == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {"state": "stopped", "category": category}
    assert "synthetic-secret" not in output.out + output.err + caplog.text
    assert logging.root.manager.disable == previous


def test_discord_http_settings() -> None:
    with notice.create_discord_client("synthetic-secret") as client:
        assert str(client.base_url).rstrip("/") == notice.DISCORD_API_BASE_URL
        assert client.follow_redirects is False
        assert client.timeout.read == 20
        assert client.timeout.connect == 20
