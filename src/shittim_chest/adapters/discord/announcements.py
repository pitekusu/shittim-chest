# SPDX-License-Identifier: MIT
"""Validated Discord announcements shared by the manual CLI and scheduled notices.

Callers must durably reserve each send attempt before calling post_announcement.
HTTP failures are ambiguous: retry discovery/verification, never blindly POST again.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx

from shittim_chest.adapters.discord.status import DISCORD_API_BASE_URL
from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig

RECENT_MESSAGE_LIMIT = 100


class AnnouncementError(RuntimeError):
    """Only fixed categories (never provider error text) cross the CLI boundary."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


@dataclass(frozen=True, slots=True, repr=False)
class AnnouncementTarget:
    """Validated routing/identity shared by search, POST, verification and output."""

    guild_id: str
    channel_id: str
    channel_name: str
    bot_id: str
    bot_name: str


def announcement_nonce(announcement_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", announcement_id) is None:
        raise AnnouncementError("announcement_id_invalid")
    # Keep the saved tool's nonce/marker derivation compatible across refactors.
    return hashlib.sha256(announcement_id.encode()).hexdigest()[:24]


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
    channel_name: str | None = None,
    *,
    channel_id: str | None = None,
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
        if (channel_name is None or channel["name"] == channel_name) and (
            channel_id is None or allowed_id == channel_id
        ):
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
    legacy_embed_title: str | None = None,
) -> bool:
    if require_object(message.get("author")).get("id") != bot_id:
        return False
    existing_content = message.get("content")
    content_matches = isinstance(existing_content, str) and existing_content.strip() == content
    embeds = message.get("embeds", [])
    if not isinstance(embeds, list):
        raise AnnouncementError("invalid_discord_response")
    legacy_matches = legacy_embed_title is not None and any(
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
    legacy_embed_title: str | None = None,
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


def announcement_fingerprint(target: AnnouncementTarget, content: str) -> str:
    """Bind a receipt to the intended author, channel and exact content without PII."""
    return hashlib.sha256(f"{target.bot_id}\n{target.channel_id}\n{content}".encode()).hexdigest()


def post_announcement(
    client: httpx.Client,
    target: AnnouncementTarget,
    *,
    content: str,
    nonce: str,
) -> str:
    """POST once; the caller must already hold an exclusive durable attempt."""
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
