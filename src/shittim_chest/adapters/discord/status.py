"""Moderator-only Discord REST adapter for durable public status messages."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

import httpx

from shittim_chest.adapters.discord.rate_limit_evidence import (
    record_status_rate_limit_response,
)
from shittim_chest.application.scale_to_zero import StatusHistoryCheckpoint
from shittim_chest.application.status_publication import (
    DiscordStatusMessage,
    StatusDeliveryError,
    StatusDeliveryErrorCode,
    StatusHistoryProgress,
    StatusMessageMissing,
    StatusWriteAmbiguous,
    has_exact_status_publication_marker,
)

DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_STATUS_HISTORY_PAGE_SIZE = 100
DISCORD_STATUS_HISTORY_MAX_PAGES = 10
DISCORD_STATUS_USER_AGENT = "DiscordBot (https://github.com/pitekusu/shittim-chest, 1.0.0)"

_SNOWFLAKE = re.compile(r"[0-9]{1,20}\Z")
_PERMISSION_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")

_GUILD_TEXT = 0
_GUILD_PUBLIC_THREAD = 11
_ADMINISTRATOR = 1 << 3
_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11
_READ_MESSAGE_HISTORY = 1 << 16
_SEND_MESSAGES_IN_THREADS = 1 << 38


def create_discord_status_http_client() -> httpx.Client:
    """Create one process-reusable client without any credential in its base headers."""

    return httpx.Client(
        base_url=DISCORD_API_BASE_URL,
        timeout=httpx.Timeout(10.0, connect=3.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        follow_redirects=False,
        event_hooks={"response": [record_status_rate_limit_response]},
    )


class DiscordRestStatusGateway:
    """Create, find, and edit one status message using only the moderator token."""

    __slots__ = (
        "_bot_user_id",
        "_client",
        "_expected_application_id",
        "_expected_guild_id",
        "_token",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        bot_token: str,
        expected_application_id: str,
        expected_guild_id: str,
    ) -> None:
        if not bot_token or bot_token != bot_token.strip():
            raise ValueError("Discord Bot token must not be empty or padded")
        _require_snowflake(expected_application_id, label="expected Application ID")
        _require_snowflake(expected_guild_id, label="expected Guild ID")
        self._client = client
        self._token = bot_token
        self._expected_application_id = expected_application_id
        self._expected_guild_id = expected_guild_id
        self._bot_user_id: str | None = None

    async def current_bot_user_id(self) -> str:
        """Resolve and cache the authenticated Bot user without assuming its Application ID."""

        return await asyncio.to_thread(self._current_bot_user_id)

    def _current_bot_user_id(self) -> str:
        if self._bot_user_id is not None:
            return self._bot_user_id
        application = self._request_json("GET", "/oauth2/applications/@me")
        if not isinstance(application, Mapping):
            raise _permanent_error()
        application_id = application.get("id")
        if not isinstance(application_id, str):
            raise _permanent_error()
        try:
            _require_snowflake(application_id, label="Application ID")
        except ValueError:
            raise _permanent_error() from None
        if application_id != self._expected_application_id:
            raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
        raw = self._request_json("GET", "/users/@me")
        if not isinstance(raw, Mapping) or raw.get("bot") is not True:
            raise _permanent_error()
        bot_user_id = raw.get("id")
        if not isinstance(bot_user_id, str):
            raise _permanent_error()
        try:
            _require_snowflake(bot_user_id, label="Bot user ID")
        except ValueError:
            raise _permanent_error() from None
        self._bot_user_id = bot_user_id
        return bot_user_id

    async def fetch_message(
        self,
        *,
        channel_id: str,
        message_id: str,
    ) -> DiscordStatusMessage:
        return await asyncio.to_thread(self._fetch_message, channel_id, message_id)

    async def find_by_nonce(
        self,
        *,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        after_message_id: str,
        checkpoint: StatusHistoryCheckpoint | None,
    ) -> DiscordStatusMessage | None:
        return await asyncio.to_thread(
            self._find_by_nonce,
            channel_id,
            author_id,
            nonce,
            operation_marker,
            after_message_id,
            checkpoint,
        )

    async def create_message(
        self,
        *,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> DiscordStatusMessage:
        return await asyncio.to_thread(self._create_message, channel_id, content, nonce)

    async def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> DiscordStatusMessage:
        return await asyncio.to_thread(self._edit_message, channel_id, message_id, content)

    def _fetch_message(self, channel_id: str, message_id: str) -> DiscordStatusMessage:
        _require_snowflake(channel_id, label="channel ID")
        _require_snowflake(message_id, label="message ID")
        self._require_status_permissions(channel_id, self._current_bot_user_id())
        raw = self._request_json(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            message_missing=True,
        )
        return _parse_message(raw)

    def _find_by_nonce(
        self,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        after_message_id: str,
        checkpoint: StatusHistoryCheckpoint | None,
    ) -> DiscordStatusMessage | None:
        _require_snowflake(channel_id, label="channel ID")
        _require_snowflake(author_id, label="author ID")
        _require_snowflake(after_message_id, label="history lower-bound ID")
        if not nonce or len(nonce) > 25:
            raise ValueError("Discord status nonce must contain 1-25 characters")
        if not operation_marker or len(operation_marker) > 64:
            raise ValueError("Discord status operation marker must contain 1-64 characters")
        if author_id != self._current_bot_user_id():
            raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
        self._require_status_permissions(channel_id, author_id)
        lower_bound = int(after_message_id)
        if checkpoint is not None:
            _validate_checkpoint(checkpoint, lower_bound=lower_bound)
        persisted_checkpoint = checkpoint
        pages_remaining = DISCORD_STATUS_HISTORY_MAX_PAGES

        if checkpoint is None:
            messages = self._history_page(
                channel_id=channel_id,
                params={
                    "limit": str(DISCORD_STATUS_HISTORY_PAGE_SIZE),
                    "after": after_message_id,
                },
                progress_checkpoint=None,
            )
            pages_remaining -= 1
            match = _history_match(
                messages,
                lower_bound=lower_bound,
                author_id=author_id,
                nonce=nonce,
                operation_marker=operation_marker,
            )
            if match is not None:
                return match
            if not messages:
                return None
            newest_message_id, oldest_message_id = _history_page_bounds(messages)
            if int(oldest_message_id) <= lower_bound:
                raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
            checkpoint = StatusHistoryCheckpoint(
                history_verified_head_message_id=newest_message_id,
                history_cursor_message_id=(
                    oldest_message_id if len(messages) == DISCORD_STATUS_HISTORY_PAGE_SIZE else None
                ),
            )

        checkpoint, match, pages_remaining, stable = self._stabilize_history_head(
            channel_id=channel_id,
            author_id=author_id,
            nonce=nonce,
            operation_marker=operation_marker,
            checkpoint=checkpoint,
            pages_remaining=pages_remaining,
        )
        if match is not None:
            return match
        if not stable:
            raise StatusHistoryProgress(
                StatusDeliveryErrorCode.UNAVAILABLE,
                checkpoint=checkpoint,
            )

        while checkpoint.history_cursor_message_id is not None:
            if pages_remaining == 0:
                raise StatusHistoryProgress(
                    StatusDeliveryErrorCode.UNAVAILABLE,
                    checkpoint=checkpoint,
                )
            cursor = checkpoint.history_cursor_message_id
            messages = self._history_page(
                channel_id=channel_id,
                params={
                    "limit": str(DISCORD_STATUS_HISTORY_PAGE_SIZE),
                    "before": cursor,
                },
                progress_checkpoint=checkpoint,
            )
            pages_remaining -= 1
            match = _history_match(
                messages,
                lower_bound=lower_bound,
                author_id=author_id,
                nonce=nonce,
                operation_marker=operation_marker,
            )
            if match is not None:
                return match
            if not messages:
                checkpoint = replace(checkpoint, history_cursor_message_id=None)
                break
            _, oldest_message_id = _history_page_bounds(messages)
            if any(int(message.message_id) >= int(cursor) for message in messages):
                raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
            if (
                int(oldest_message_id) <= lower_bound
                or len(messages) < DISCORD_STATUS_HISTORY_PAGE_SIZE
            ):
                checkpoint = replace(checkpoint, history_cursor_message_id=None)
                break
            checkpoint = replace(
                checkpoint,
                history_cursor_message_id=oldest_message_id,
            )

        checkpoint, match, _, stable = self._stabilize_history_head(
            channel_id=channel_id,
            author_id=author_id,
            nonce=nonce,
            operation_marker=operation_marker,
            checkpoint=checkpoint,
            pages_remaining=pages_remaining,
        )
        if match is not None:
            return match
        if not stable:
            raise StatusHistoryProgress(
                StatusDeliveryErrorCode.UNAVAILABLE,
                checkpoint=checkpoint,
            )
        if persisted_checkpoint is not None and checkpoint != persisted_checkpoint:
            raise StatusHistoryProgress(
                StatusDeliveryErrorCode.UNAVAILABLE,
                checkpoint=checkpoint,
            )
        return None

    def _stabilize_history_head(
        self,
        *,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        checkpoint: StatusHistoryCheckpoint,
        pages_remaining: int,
    ) -> tuple[StatusHistoryCheckpoint, DiscordStatusMessage | None, int, bool]:
        """Catch up every message above the verified head before concluding a scan."""

        while pages_remaining > 0:
            verified_head = int(checkpoint.history_verified_head_message_id)
            gap_cursor = checkpoint.history_gap_cursor_message_id
            if gap_cursor is None:
                messages = self._history_page(
                    channel_id=channel_id,
                    params={
                        "limit": str(DISCORD_STATUS_HISTORY_PAGE_SIZE),
                        "after": checkpoint.history_verified_head_message_id,
                    },
                    progress_checkpoint=checkpoint,
                )
                pages_remaining -= 1
                match = _history_match(
                    messages,
                    lower_bound=verified_head,
                    author_id=author_id,
                    nonce=nonce,
                    operation_marker=operation_marker,
                )
                if match is not None:
                    return checkpoint, match, pages_remaining, True
                if not messages:
                    return checkpoint, None, pages_remaining, True
                newest_message_id, oldest_message_id = _history_page_bounds(messages)
                if int(oldest_message_id) <= verified_head:
                    raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
                if len(messages) < DISCORD_STATUS_HISTORY_PAGE_SIZE:
                    checkpoint = replace(
                        checkpoint,
                        history_verified_head_message_id=newest_message_id,
                    )
                    continue
                checkpoint = replace(
                    checkpoint,
                    history_gap_cursor_message_id=oldest_message_id,
                    history_gap_upper_message_id=newest_message_id,
                )
                continue

            gap_upper = checkpoint.history_gap_upper_message_id
            if gap_upper is None:
                raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
            messages = self._history_page(
                channel_id=channel_id,
                params={
                    "limit": str(DISCORD_STATUS_HISTORY_PAGE_SIZE),
                    "before": gap_cursor,
                },
                progress_checkpoint=checkpoint,
            )
            pages_remaining -= 1
            if any(int(message.message_id) >= int(gap_cursor) for message in messages):
                raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
            match = _history_match(
                tuple(
                    message
                    for message in messages
                    if verified_head < int(message.message_id) <= int(gap_upper)
                ),
                lower_bound=verified_head,
                author_id=author_id,
                nonce=nonce,
                operation_marker=operation_marker,
            )
            if match is not None:
                return checkpoint, match, pages_remaining, True
            in_gap = tuple(
                message for message in messages if int(message.message_id) > verified_head
            )
            gap_complete = (
                len(messages) < DISCORD_STATUS_HISTORY_PAGE_SIZE
                or not in_gap
                or min(int(message.message_id) for message in messages) <= verified_head
            )
            if gap_complete:
                checkpoint = StatusHistoryCheckpoint(
                    history_cursor_message_id=checkpoint.history_cursor_message_id,
                    history_verified_head_message_id=gap_upper,
                )
                continue
            _, oldest_message_id = _history_page_bounds(in_gap)
            checkpoint = replace(
                checkpoint,
                history_gap_cursor_message_id=oldest_message_id,
            )
        return checkpoint, None, pages_remaining, False

    def _history_page(
        self,
        *,
        channel_id: str,
        params: Mapping[str, str],
        progress_checkpoint: StatusHistoryCheckpoint | None,
    ) -> tuple[DiscordStatusMessage, ...]:
        try:
            raw = self._request_json(
                "GET",
                f"/channels/{channel_id}/messages",
                params=params,
            )
        except StatusDeliveryError as error:
            if error.retryable and progress_checkpoint is not None:
                raise StatusHistoryProgress(
                    error.code,
                    checkpoint=progress_checkpoint,
                    retry_after_seconds=error.retry_after_seconds,
                ) from None
            raise
        if not isinstance(raw, list):
            raise _permanent_error()
        return tuple(_parse_message(value) for value in raw)

    def _require_status_permissions(self, channel_id: str, bot_user_id: str) -> bool:
        """Recompute current Bot permissions from the guild and channel sources of truth."""

        raw_channel = self._request_json("GET", f"/channels/{channel_id}")
        channel_type, permission_overwrites, archived = self._permission_target(
            raw_channel,
            expected_channel_id=channel_id,
        )
        required_permissions = _VIEW_CHANNEL | _READ_MESSAGE_HISTORY
        if channel_type == _GUILD_TEXT:
            required_permissions |= _SEND_MESSAGES
        else:
            required_permissions |= _SEND_MESSAGES_IN_THREADS

        raw_member = self._request_json(
            "GET",
            f"/guilds/{self._expected_guild_id}/members/{bot_user_id}",
        )
        member_role_ids = _parse_member_role_ids(
            raw_member,
            expected_user_id=bot_user_id,
            expected_guild_id=self._expected_guild_id,
        )
        raw_roles = self._request_json(
            "GET",
            f"/guilds/{self._expected_guild_id}/roles",
        )
        role_permissions = _parse_role_permissions(
            raw_roles,
            expected_guild_id=self._expected_guild_id,
            member_role_ids=member_role_ids,
        )
        effective = _compute_effective_permissions(
            expected_guild_id=self._expected_guild_id,
            bot_user_id=bot_user_id,
            member_role_ids=member_role_ids,
            role_permissions=role_permissions,
            permission_overwrites=permission_overwrites,
        )
        if not effective & _ADMINISTRATOR and (
            effective & required_permissions != required_permissions
        ):
            raise _permanent_error(StatusDeliveryErrorCode.REJECTED)
        return channel_type == _GUILD_PUBLIC_THREAD and archived

    def _permission_target(
        self,
        raw_channel: object,
        *,
        expected_channel_id: str,
    ) -> tuple[int, tuple[tuple[int, str, int, int], ...], bool]:
        channel_type, parent_id, overwrites, archived, locked = _parse_permission_channel(
            raw_channel,
            expected_channel_id=expected_channel_id,
            expected_guild_id=self._expected_guild_id,
        )
        if channel_type == _GUILD_TEXT:
            return channel_type, overwrites, False
        if channel_type != _GUILD_PUBLIC_THREAD or parent_id is None or locked:
            raise _permanent_error(StatusDeliveryErrorCode.REJECTED)
        raw_parent = self._request_json("GET", f"/channels/{parent_id}")
        parent_type, _, parent_overwrites, _, parent_locked = _parse_permission_channel(
            raw_parent,
            expected_channel_id=parent_id,
            expected_guild_id=self._expected_guild_id,
        )
        if parent_type != _GUILD_TEXT or parent_locked:
            raise _permanent_error(StatusDeliveryErrorCode.REJECTED)
        return channel_type, parent_overwrites, archived

    def _create_message(
        self,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> DiscordStatusMessage:
        _require_snowflake(channel_id, label="channel ID")
        if not content or len(content) > 2_000:
            raise ValueError("Discord status content must contain 1-2000 characters")
        if not nonce or len(nonce) > 25:
            raise ValueError("Discord status nonce must contain 1-25 characters")
        self._require_status_permissions(channel_id, self._current_bot_user_id())
        raw = self._request_json(
            "POST",
            f"/channels/{channel_id}/messages",
            json={
                "content": content,
                "nonce": nonce,
                "enforce_nonce": True,
                "allowed_mentions": {"parse": []},
            },
            write_may_succeed=True,
        )
        try:
            return _parse_message(raw)
        except StatusDeliveryError:
            raise StatusWriteAmbiguous() from None

    def _edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> DiscordStatusMessage:
        _require_snowflake(channel_id, label="channel ID")
        _require_snowflake(message_id, label="message ID")
        if not content or len(content) > 2_000:
            raise ValueError("Discord status content must contain 1-2000 characters")
        bot_user_id = self._current_bot_user_id()
        if self._require_status_permissions(channel_id, bot_user_id):
            self._unarchive_public_thread(channel_id)
            if self._require_status_permissions(channel_id, bot_user_id):
                raise StatusWriteAmbiguous(StatusDeliveryErrorCode.CONFLICT)
        raw = self._request_json(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            json={
                "content": content,
                "allowed_mentions": {"parse": []},
            },
            message_missing=True,
            write_may_succeed=True,
        )
        try:
            return _parse_message(raw)
        except StatusDeliveryError:
            raise StatusWriteAmbiguous() from None

    def _unarchive_public_thread(self, channel_id: str) -> None:
        """Activate one unlocked public thread before editing its existing message."""

        raw = self._request_json(
            "PATCH",
            f"/channels/{channel_id}",
            json={"archived": False},
            write_may_succeed=True,
        )
        try:
            channel_type, _, _, archived, locked = _parse_permission_channel(
                raw,
                expected_channel_id=channel_id,
                expected_guild_id=self._expected_guild_id,
            )
        except StatusDeliveryError:
            raise StatusWriteAmbiguous(StatusDeliveryErrorCode.CONFLICT) from None
        if channel_type != _GUILD_PUBLIC_THREAD or archived or locked:
            raise StatusWriteAmbiguous(StatusDeliveryErrorCode.CONFLICT)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
        message_missing: bool = False,
        write_may_succeed: bool = False,
    ) -> object:
        try:
            response = self._client.request(
                method,
                path,
                headers={
                    "Authorization": f"Bot {self._token}",
                    "User-Agent": DISCORD_STATUS_USER_AGENT,
                },
                json=json,
                params=params,
            )
        except httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout:
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        except httpx.TimeoutException, httpx.TransportError:
            if write_may_succeed:
                raise StatusWriteAmbiguous() from None
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        if response.status_code == 429:
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.RATE_LIMITED,
                retryable=True,
                retry_after_seconds=_retry_after(response),
            ) from None
        if response.status_code in {408, 409} or response.status_code >= 500:
            if write_may_succeed:
                raise StatusWriteAmbiguous() from None
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        if response.status_code == 404 and message_missing and _error_code(response) == 10008:
            raise StatusMessageMissing from None
        if not 200 <= response.status_code < 300:
            raise _permanent_error() from None
        try:
            return response.json()
        except ValueError:
            if write_may_succeed:
                raise StatusWriteAmbiguous() from None
            raise _permanent_error() from None


def _parse_permission_channel(
    value: object,
    *,
    expected_channel_id: str,
    expected_guild_id: str,
) -> tuple[int, str | None, tuple[tuple[int, str, int, int], ...], bool, bool]:
    if not isinstance(value, Mapping):
        raise _permanent_error()
    channel_id = _snowflake_field(value.get("id"))
    guild_id = _snowflake_field(value.get("guild_id"))
    channel_type = value.get("type")
    if (
        channel_id != expected_channel_id
        or guild_id != expected_guild_id
        or isinstance(channel_type, bool)
        or not isinstance(channel_type, int)
    ):
        raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
    parent = value.get("parent_id")
    parent_id = None if parent is None else _snowflake_field(parent)
    if channel_type == _GUILD_TEXT:
        return (
            channel_type,
            parent_id,
            _parse_permission_overwrites(cast("Mapping[object, object]", value)),
            False,
            False,
        )
    if channel_type != _GUILD_PUBLIC_THREAD or parent_id is None:
        raise _permanent_error(StatusDeliveryErrorCode.REJECTED)
    metadata = value.get("thread_metadata")
    if not isinstance(metadata, Mapping):
        raise _permanent_error()
    archived = metadata.get("archived")
    locked = metadata.get("locked")
    if not isinstance(archived, bool) or not isinstance(locked, bool):
        raise _permanent_error()
    return channel_type, parent_id, (), archived, locked


def _parse_permission_overwrites(
    channel: Mapping[object, object],
) -> tuple[tuple[int, str, int, int], ...]:
    raw = channel.get("permission_overwrites")
    if not isinstance(raw, list):
        raise _permanent_error()
    parsed: list[tuple[int, str, int, int]] = []
    identities: set[tuple[int, str]] = set()
    for value in raw:
        if not isinstance(value, Mapping):
            raise _permanent_error()
        overwrite_type = value.get("type")
        if (
            isinstance(overwrite_type, bool)
            or not isinstance(overwrite_type, int)
            or overwrite_type not in {0, 1}
        ):
            raise _permanent_error()
        overwrite_id = _snowflake_field(value.get("id"))
        identity = (overwrite_type, overwrite_id)
        if identity in identities:
            raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
        identities.add(identity)
        parsed.append(
            (
                overwrite_type,
                overwrite_id,
                _permission_field(value.get("allow")),
                _permission_field(value.get("deny")),
            )
        )
    return tuple(parsed)


def _parse_member_role_ids(
    value: object,
    *,
    expected_user_id: str,
    expected_guild_id: str,
) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        raise _permanent_error()
    user = value.get("user")
    roles = value.get("roles")
    if not isinstance(user, Mapping) or not isinstance(roles, list):
        raise _permanent_error()
    if _snowflake_field(user.get("id")) != expected_user_id:
        raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
    parsed = tuple(_snowflake_field(role_id) for role_id in roles)
    if len(set(parsed)) != len(parsed) or expected_guild_id in parsed:
        raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
    return parsed


def _parse_role_permissions(
    value: object,
    *,
    expected_guild_id: str,
    member_role_ids: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(value, list):
        raise _permanent_error()
    parsed: dict[str, int] = {}
    for role in value:
        if not isinstance(role, Mapping):
            raise _permanent_error()
        role_id = _snowflake_field(role.get("id"))
        if role_id in parsed:
            raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
        parsed[role_id] = _permission_field(role.get("permissions"))
    if expected_guild_id not in parsed or any(role_id not in parsed for role_id in member_role_ids):
        raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
    return parsed


def _compute_effective_permissions(
    *,
    expected_guild_id: str,
    bot_user_id: str,
    member_role_ids: tuple[str, ...],
    role_permissions: Mapping[str, int],
    permission_overwrites: tuple[tuple[int, str, int, int], ...],
) -> int:
    permissions = role_permissions[expected_guild_id]
    for role_id in member_role_ids:
        permissions |= role_permissions[role_id]
    if permissions & _ADMINISTRATOR:
        return permissions

    overwrites = {
        (overwrite_type, overwrite_id): (allow, deny)
        for overwrite_type, overwrite_id, allow, deny in permission_overwrites
    }
    everyone = overwrites.get((0, expected_guild_id))
    if everyone is not None:
        allow, deny = everyone
        permissions = permissions & ~deny | allow

    role_allow = 0
    role_deny = 0
    for role_id in member_role_ids:
        overwrite = overwrites.get((0, role_id))
        if overwrite is not None:
            allow, deny = overwrite
            role_allow |= allow
            role_deny |= deny
    permissions = permissions & ~role_deny | role_allow

    member = overwrites.get((1, bot_user_id))
    if member is not None:
        allow, deny = member
        permissions = permissions & ~deny | allow
    return permissions


def _permission_field(value: object) -> int:
    if not isinstance(value, str) or _PERMISSION_INTEGER.fullmatch(value) is None:
        raise _permanent_error()
    return int(value)


def _snowflake_field(value: object) -> str:
    if not isinstance(value, str):
        raise _permanent_error()
    try:
        _require_snowflake(value, label="provider field")
    except ValueError:
        raise _permanent_error() from None
    return value


def _parse_message(value: object) -> DiscordStatusMessage:
    if not isinstance(value, Mapping):
        raise _permanent_error()
    author = value.get("author")
    if not isinstance(author, Mapping):
        raise _permanent_error()
    message_id = value.get("id")
    channel_id = value.get("channel_id")
    author_id = author.get("id")
    content = value.get("content")
    nonce = value.get("nonce")
    if not isinstance(message_id, str):
        raise _permanent_error()
    if not isinstance(channel_id, str):
        raise _permanent_error()
    if not isinstance(author_id, str):
        raise _permanent_error()
    if not isinstance(content, str):
        raise _permanent_error()
    if nonce is not None and not isinstance(nonce, str | int):
        raise _permanent_error()
    try:
        return DiscordStatusMessage(
            message_id=message_id,
            channel_id=channel_id,
            author_id=author_id,
            content=content,
            nonce=None if nonce is None else str(nonce),
        )
    except ValueError:
        raise _permanent_error() from None


def _history_match(
    messages: tuple[DiscordStatusMessage, ...],
    *,
    lower_bound: int,
    author_id: str,
    nonce: str,
    operation_marker: str,
) -> DiscordStatusMessage | None:
    matched = tuple(
        message
        for message in messages
        if int(message.message_id) > lower_bound
        and message.author_id == author_id
        and (
            message.nonce == nonce
            or (
                message.nonce is None
                and has_exact_status_publication_marker(message.content, operation_marker)
            )
        )
    )
    if not matched:
        return None
    return min(matched, key=lambda item: int(item.message_id))


def _history_page_bounds(
    messages: tuple[DiscordStatusMessage, ...],
) -> tuple[str, str]:
    if not messages:
        raise ValueError("Discord history page must not be empty")
    identifiers = tuple(message.message_id for message in messages)
    if len(set(identifiers)) != len(identifiers):
        raise _permanent_error(StatusDeliveryErrorCode.CONFLICT)
    return max(identifiers, key=int), min(identifiers, key=int)


def _validate_checkpoint(
    checkpoint: StatusHistoryCheckpoint,
    *,
    lower_bound: int,
) -> None:
    if int(checkpoint.history_verified_head_message_id) < lower_bound:
        raise ValueError("Discord history verified head cannot precede its lower bound")
    cursor = checkpoint.history_cursor_message_id
    if cursor is not None and int(cursor) <= lower_bound:
        raise ValueError("Discord history cursor must follow its lower bound")


def _retry_after(response: httpx.Response) -> float | None:
    candidates: list[object] = [response.headers.get("Retry-After")]
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        candidates.append(body.get("retry_after"))
    parsed_candidates: list[float] = []
    for candidate in candidates:
        if isinstance(candidate, bool) or not isinstance(candidate, str | int | float):
            continue
        try:
            parsed = float(candidate)
        except TypeError, ValueError:
            continue
        if math.isfinite(parsed) and parsed > 0:
            parsed_candidates.append(parsed)
    return max(parsed_candidates, default=None)


def _error_code(response: httpx.Response) -> int | None:
    try:
        value = response.json()
    except ValueError:
        return None
    if not isinstance(value, Mapping):
        return None
    code = value.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _permanent_error(
    code: StatusDeliveryErrorCode = StatusDeliveryErrorCode.REJECTED,
) -> StatusDeliveryError:
    return StatusDeliveryError(code, retryable=False)


def _require_snowflake(value: str, *, label: str) -> None:
    if (
        _SNOWFLAKE.fullmatch(value) is None
        or not 0 < int(value) < 2**64
        or str(int(value)) != value
    ):
        raise ValueError(f"Discord status {label} must be a valid snowflake")


__all__ = (
    "DISCORD_API_BASE_URL",
    "DiscordRestStatusGateway",
    "create_discord_status_http_client",
)
