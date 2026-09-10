# SPDX-License-Identifier: MIT
"""Manually announce as the production moderator Bot; never POST without --send.

Verify AWS -> load runtime -> verify target -> find duplicates -> reserve a local
attempt -> POST once -> GET and verify. Never log provider exception text.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.adapters.discord.status import DISCORD_API_BASE_URL
from shittim_chest.application.discord import (
    DISCORD_MESSAGE_LIMIT,
    DiscordBotSlot,
    DiscordRuntimeConfig,
)
from shittim_chest.config.models import (
    DEFAULT_AWS_REGION,
    StartupConfigurationError,
    parse_discord_runtime_config,
)
from shittim_chest.config.status_publisher import load_status_publisher_settings

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient

STATUS_FUNCTION = "shittim-chest-production-discord-status-publisher"
EXPECTED_ACCOUNT_ENV = "SHITTIM_EXPECTED_AWS_ACCOUNT_ID"
RECENT_MESSAGE_LIMIT = 100
MAX_MESSAGE_BYTES = 8_000
LEGACY_EMBED_TITLE = "メモリアルロビーの画像生成モデルを更新しました"
AWS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class AnnouncementError(RuntimeError):
    """Only fixed categories (never provider error text) cross the CLI boundary."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


class Arguments(argparse.Namespace):
    message_file: Path
    announcement_id: str
    expected_account_id: str | None
    channel_name: str | None
    legacy_embed_title: str
    state_dir: Path
    send: bool


@dataclass(frozen=True, slots=True, repr=False)
class AnnouncementTarget:
    """Validated routing/identity shared by search, POST, verification and output."""

    guild_id: str
    channel_id: str
    channel_name: str
    bot_id: str
    bot_name: str


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-file", type=Path, required=True)
    parser.add_argument(
        "--announcement-id", required=True, help="Reuse this ID for the same notice"
    )
    parser.add_argument(
        "--expected-account-id",
        default=os.environ.get(EXPECTED_ACCOUNT_ENV),
        help=f"Independent production account ID, or {EXPECTED_ACCOUNT_ENV}",
    )
    parser.add_argument("--channel-name", help="Select exactly one runtime-allowed channel by name")
    parser.add_argument(
        "--legacy-embed-title",
        default=LEGACY_EMBED_TITLE,
        help="Duplicate detection only; never added to new posts",
    )
    parser.add_argument("--send", action="store_true", help="Actually send; default is read-only")
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/shittim-chest/announcements"
    )
    return parser.parse_args(argv, namespace=Arguments())


def read_message(path: Path) -> str:
    with path.open("rb") as source:
        raw = source.read(MAX_MESSAGE_BYTES + 1)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise AnnouncementError("message_too_large")
    try:
        message = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise AnnouncementError("message_encoding_invalid") from None
    if not message or len(message.encode("utf-16-le")) // 2 > DISCORD_MESSAGE_LIMIT:
        raise AnnouncementError("message_length_invalid")
    return message


def announcement_nonce(announcement_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", announcement_id) is None:
        raise AnnouncementError("announcement_id_invalid")
    # Keep the saved tool's nonce/marker derivation compatible across refactors.
    return hashlib.sha256(announcement_id.encode()).hexdigest()[:24]


def create_production_session(expected_account_id: str | None) -> boto3.Session:
    if expected_account_id is None or re.fullmatch(r"[0-9]{12}", expected_account_id) is None:
        raise AnnouncementError("expected_aws_account_required")
    session = boto3.Session(region_name=DEFAULT_AWS_REGION)
    identity = session.client("sts", config=AWS_CLIENT_CONFIG).get_caller_identity()
    if identity.get("Account") != expected_account_id:
        raise AnnouncementError("unexpected_aws_account")
    return session


def read_parameter(client: SSMClient, name: str) -> str:
    parameter = client.get_parameter(Name=name, WithDecryption=True).get("Parameter", {})
    value = parameter.get("Value")
    if parameter.get("Name") != name or not isinstance(value, str) or not value:
        raise AnnouncementError("invalid_ssm_parameter")
    return value


def load_runtime_config(session: boto3.Session) -> tuple[DiscordRuntimeConfig, str]:
    function = session.client("lambda", config=AWS_CLIENT_CONFIG).get_function_configuration(
        FunctionName=STATUS_FUNCTION,
    )
    try:
        settings = load_status_publisher_settings(
            function.get("Environment", {}).get("Variables", {})
        )
        ssm = session.client("ssm", config=AWS_CLIENT_CONFIG)
        runtime, version = parse_discord_runtime_config(
            read_parameter(ssm, settings.runtime_config_parameter)
        )
    except StartupConfigurationError:
        raise AnnouncementError("invalid_runtime_config") from None
    # Follow the deployed version, not an obsolete hard-coded vNNNN snapshot.
    if settings.runtime_config_parameter.rsplit("/", 1)[-1] != version:
        raise AnnouncementError("unexpected_runtime_version")
    token = read_parameter(ssm, settings.moderator_token_parameter)
    if token != token.strip() or "\r" in token or "\n" in token:
        raise AnnouncementError("invalid_moderator_token")
    return runtime, token


def create_discord_client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=DISCORD_API_BASE_URL,
        timeout=20,
        follow_redirects=False,
        headers={"Authorization": f"Bot {token}"},
    )


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    payload: Mapping[str, object] | None = None,
) -> object:
    response = client.request(method, path, params=params, json=payload)
    if not 200 <= response.status_code < 300:
        raise AnnouncementError(f"discord_http_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise AnnouncementError("invalid_discord_response") from None


def require_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise AnnouncementError("invalid_discord_response")
    return cast(dict[str, object], value)


def require_snowflake(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{1,20}", value) is None
        or not 0 < int(value) < 2**64
    ):
        raise AnnouncementError("invalid_discord_response")
    return value


def resolve_target_channel(
    client: httpx.Client,
    runtime: DiscordRuntimeConfig,
    channel_name: str | None,
) -> AnnouncementTarget:
    application = require_object(request_json(client, "GET", "/oauth2/applications/@me"))
    if application.get("id") != runtime.application_id_for(DiscordBotSlot.MODERATOR):
        raise AnnouncementError("unexpected_application_id")
    bot = require_object(request_json(client, "GET", "/users/@me"))
    if bot.get("bot") is not True or not isinstance(bot.get("username"), str):
        raise AnnouncementError("unexpected_bot_identity")
    bot_id = require_snowflake(bot.get("id"))
    # Like the existing status gateway, do not assume Bot user ID == Application ID.
    candidates: list[dict[str, object]] = []
    for allowed_id in sorted(runtime.allowed_channel_ids):
        channel = require_object(request_json(client, "GET", f"/channels/{allowed_id}"))
        if (
            channel.get("id") != allowed_id
            or channel.get("guild_id") != runtime.guild_id
            or type(channel.get("type")) is not int
            or channel["type"] not in (0, 5)
            or not isinstance(channel.get("name"), str)
        ):
            raise AnnouncementError("invalid_target_channel")
        if channel_name is None or channel["name"] == channel_name:
            candidates.append(channel)
    if len(candidates) != 1:
        raise AnnouncementError("target_channel_ambiguous")
    channel = candidates[0]
    return AnnouncementTarget(
        runtime.guild_id,
        require_snowflake(channel["id"]),
        cast(str, channel["name"]),
        bot_id,
        cast(str, bot["username"]),
    )


def is_matching_announcement(
    message: Mapping[str, object],
    *,
    bot_id: str,
    content: str,
    nonce: str,
    legacy_embed_title: str,
) -> bool:
    if require_object(message.get("author")).get("id") != bot_id:
        return False
    existing_content = message.get("content")
    content_matches = isinstance(existing_content, str) and existing_content.strip() == content
    embeds = message.get("embeds", [])
    if not isinstance(embeds, list):
        raise AnnouncementError("invalid_discord_response")
    legacy_matches = any(
        isinstance(embed, dict)
        and embed.get("title") == legacy_embed_title
        and embed.get("description") == content
        for embed in embeds
    )
    if message.get("nonce") == nonce and not (content_matches or legacy_matches):
        raise AnnouncementError("announcement_id_conflict")
    return content_matches or legacy_matches


def find_existing_announcement(
    client: httpx.Client,
    target: AnnouncementTarget,
    *,
    content: str,
    nonce: str,
    legacy_embed_title: str,
) -> str | None:
    history = request_json(
        client,
        "GET",
        f"/channels/{target.channel_id}/messages",
        params={"limit": str(RECENT_MESSAGE_LIMIT)},
    )
    if not isinstance(history, list):
        raise AnnouncementError("invalid_discord_response")
    match: str | None = None
    for value in history:
        message = require_object(value)
        if message.get("channel_id") != target.channel_id:
            raise AnnouncementError("invalid_discord_response")
        if is_matching_announcement(
            message,
            bot_id=target.bot_id,
            content=content,
            nonce=nonce,
            legacy_embed_title=legacy_embed_title,
        ):
            match = require_snowflake(message.get("id"))
    return match


def check_send_attempt(path: Path, target: AnnouncementTarget, content: str) -> str:
    fingerprint = hashlib.sha256(
        f"{target.bot_id}\n{target.channel_id}\n{content}".encode()
    ).hexdigest()
    if path.exists() and path.read_text(encoding="ascii") != fingerprint:
        raise AnnouncementError("announcement_id_reused_for_different_content_or_target")
    return fingerprint


def send_announcement(
    client: httpx.Client,
    target: AnnouncementTarget,
    *,
    content: str,
    nonce: str,
    attempt_path: Path,
    fingerprint: str,
) -> str:
    # History/Discord nonce are bounded. Retain this exclusive claim even on HTTP failure:
    # after a timeout the server may already have accepted the message. Never auto-retry.
    attempt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(attempt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AnnouncementError("previous_send_attempt_exists_check_discord_before_retry") from None
    with os.fdopen(descriptor, "w", encoding="ascii") as marker:
        marker.write(fingerprint)
        marker.flush()
        os.fsync(marker.fileno())
    posted = require_object(
        request_json(
            client,
            "POST",
            f"/channels/{target.channel_id}/messages",
            payload={
                "content": content,
                "allowed_mentions": {"parse": []},
                "nonce": nonce,
                "enforce_nonce": True,
            },
        )
    )
    return require_snowflake(posted.get("id"))


def verify_posted_announcement(
    client: httpx.Client,
    target: AnnouncementTarget,
    message_id: str,
    content: str,
) -> None:
    message = require_object(
        request_json(client, "GET", f"/channels/{target.channel_id}/messages/{message_id}")
    )
    author = message.get("author")
    if (
        message.get("id") != message_id
        or message.get("channel_id") != target.channel_id
        or not isinstance(author, dict)
        or author.get("id") != target.bot_id
        or message.get("content") != content
        or message.get("embeds") != []
    ):
        raise AnnouncementError("post_verification_failed")


def emit_result(state: str, target: AnnouncementTarget, message_id: str | None = None) -> None:
    result = {"state": state, "bot": target.bot_name, "channel": target.channel_name}
    if message_id is not None:
        result["url"] = (
            f"https://discord.com/channels/{target.guild_id}/{target.channel_id}/{message_id}"
        )
    print(json.dumps(result, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    # botocore DEBUG includes decrypted SSM response bodies. Preserve suppression for
    # this short-lived CLI, but restore the caller's setting when main is used in tests.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        content = read_message(args.message_file)
        nonce = announcement_nonce(args.announcement_id)
        session = create_production_session(args.expected_account_id)
        runtime, token = load_runtime_config(session)
        with create_discord_client(token) as discord:
            target = resolve_target_channel(discord, runtime, args.channel_name)
            attempt_path = args.state_dir / f"{nonce}.attempt"
            fingerprint = check_send_attempt(attempt_path, target, content)
            existing = find_existing_announcement(
                discord,
                target,
                content=content,
                nonce=nonce,
                legacy_embed_title=args.legacy_embed_title,
            )
            if existing is not None:
                emit_result("already_posted", target, existing)
                return 0
            if not args.send:
                emit_result("ready", target)
                return 0
            posted = send_announcement(
                discord,
                target,
                content=content,
                nonce=nonce,
                attempt_path=attempt_path,
                fingerprint=fingerprint,
            )
            verify_posted_announcement(discord, target, posted, content)
            emit_result("posted_and_verified", target, posted)
            return 0
    except AnnouncementError as error:
        category = error.category
    except BotoCoreError, ClientError:
        category = "aws_request_failed"
    except httpx.HTTPError:
        category = "discord_transport_failed"
    except OSError:
        category = "local_io_failed"
    except Exception:
        category = "announcement_failed"
    finally:
        logging.disable(previous_logging_disable)
    print(json.dumps({"state": "stopped", "category": category}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
