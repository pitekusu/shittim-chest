"""Publish student-gacha messages as Arona in the source text channel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TypedDict

import discord

from shittim_chest.adapters.discord.errors import (
    DiscordAdapterError,
    DiscordDeliveryConflict,
    DiscordIdentityUnavailable,
    DiscordPermissionDenied,
    DiscordUnavailable,
)
from shittim_chest.adapters.discord_http import (
    STUDENT_GACHA_APPROVAL,
    STUDENT_GACHA_BUTTON_PREFIX,
    STUDENT_GACHA_OFFER_BUTTON,
    STUDENT_GACHA_OFFER_ID,
)
from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.student_gacha_discord import GachaDeliveryFailure

_LOADING_TEXT = "⏳ 生徒を募集しています!"
_LOADING_FILENAME = re.compile(r"show-[A-Za-z0-9_-]{22}\.gif\Z")
_HISTORY_LIMIT = 500
_UNKNOWN_MESSAGE = 10008
_DELIVERY_TIMEOUT_SECONDS = 35
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


class _MessageOptions(TypedDict, total=False):
    view: discord.ui.View
    reference: discord.MessageReference


@dataclass(frozen=True, slots=True)
class _Publication:
    content: str
    filename: str | None = None
    content_type: str | None = None
    attachment: bytes | None = field(default=None, repr=False)
    custom_id: str | None = None
    reply_to_anchor: bool = False


class DiscordStudentGacha:
    """Use stable Discord identities to reconcile an interrupted publication."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        config: DiscordRuntimeConfig,
    ) -> None:
        self._client = clients[DiscordBotSlot.PARTICIPANT_A]
        self._config = config
        self._author_id = int(config.application_id_for(DiscordBotSlot.PARTICIPANT_A))

    async def send_progress(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        remaining: int,
    ) -> str:
        if not 1 <= remaining <= 4:
            raise ValueError("student gacha progress is outside its bounds")
        return await self._send(
            guild_id,
            channel_id,
            publication_id,
            after_message_id,
            _Publication(
                f"✨ あと{remaining}回で生徒ガチャにチャレンジできます!",
                reply_to_anchor=True,
            ),
        )

    async def send_offer(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        offer_id: str,
    ) -> str:
        if STUDENT_GACHA_OFFER_ID.fullmatch(offer_id) is None:
            raise ValueError("student gacha offer ID is invalid")
        return await self._send(
            guild_id,
            channel_id,
            publication_id,
            after_message_id,
            _Publication(
                STUDENT_GACHA_APPROVAL,
                custom_id=f"{STUDENT_GACHA_BUTTON_PREFIX}{offer_id}:{_nonce(publication_id)}",
            ),
        )

    async def send_loading(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str:
        delivery_id = f"{publication_id}:loading"
        return await self._send(
            guild_id,
            channel_id,
            delivery_id,
            after_message_id,
            _loading_publication(delivery_id, content),
        )

    async def find_loading(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str | None:
        """Reconcile an unresolved loading message without publishing one."""

        delivery_id = f"{publication_id}:loading"
        publication = _loading_publication(delivery_id, content)
        async with _delivery_boundary():
            channel = await self._channel(guild_id, channel_id, send_messages=False)
            anchor = None if after_message_id is None else discord.Object(id=int(after_message_id))
            message = await self._find(channel, anchor, publication, _nonce(delivery_id), None)
            return None if message is None else str(message.id)

    async def send_result(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        content: bytes,
    ) -> str:
        delivery_id = f"{publication_id}:result"
        digest_prefix = hashlib.sha256(content).hexdigest()[:16]
        return await self._send(
            guild_id,
            channel_id,
            delivery_id,
            after_message_id,
            _Publication(
                "✨ 生徒募集の結果です!",
                f"student-{_nonce(delivery_id)}-{digest_prefix}.png",
                "image/png",
                content,
            ),
        )

    async def send_failure(
        self,
        *,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        retryable: bool,
    ) -> str:
        content = (
            "生徒募集を完了できませんでした。もう一度「１回募集」からお試しください。"
            if retryable
            else "生徒募集の結果を確認できませんでした。しばらくお待ちください。"
        )
        return await self._send(
            guild_id,
            channel_id,
            f"{publication_id}:failure",
            after_message_id,
            _Publication(content),
        )

    async def disable_offer(self, *, guild_id: str, channel_id: str, message_id: str) -> None:
        async with _delivery_boundary():
            channel = await self._channel(guild_id, channel_id)
            try:
                message = await channel.fetch_message(int(message_id))
                self._require_message_target(message, channel)
                custom_id = _offer_button_id(message)
                if message.content != STUDENT_GACHA_APPROVAL or custom_id is None:
                    raise DiscordDeliveryConflict
                await message.edit(
                    view=_offer_view(custom_id, disabled=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.NotFound as error:
                if error.code != _UNKNOWN_MESSAGE:
                    raise DiscordUnavailable from None

    async def delete_loading(self, *, guild_id: str, channel_id: str, message_id: str) -> None:
        async with _delivery_boundary():
            channel = await self._channel(guild_id, channel_id)
            try:
                message = await channel.fetch_message(int(message_id))
                self._require_message_target(message, channel)
                if (
                    message.content != _LOADING_TEXT
                    or len(message.attachments) != 1
                    or _LOADING_FILENAME.fullmatch(message.attachments[0].filename) is None
                ):
                    raise DiscordDeliveryConflict
                await message.delete()
            except discord.NotFound as error:
                if error.code != _UNKNOWN_MESSAGE:
                    raise DiscordUnavailable from None

    async def _send(
        self,
        guild_id: str,
        channel_id: str,
        publication_id: str,
        after_message_id: str | None,
        publication: _Publication,
    ) -> str:
        nonce = _nonce(publication_id)
        if publication.attachment is not None and (
            not publication.attachment or len(publication.attachment) > _MAX_ATTACHMENT_BYTES
        ):
            raise GachaDeliveryFailure(retryable=False)
        # Discord may omit nonce in history. A nonsecret embed footer identifies
        # independent text-only publications, even after Discord's nonce window.
        marker = (
            nonce
            if (
                publication.custom_id is None
                and publication.filename is None
                and (not publication.reply_to_anchor or after_message_id is None)
            )
            else None
        )
        async with _delivery_boundary():
            channel = await self._channel(
                guild_id,
                channel_id,
                attach_files=publication.attachment is not None,
                embed_links=marker is not None,
            )
            anchor = None if after_message_id is None else discord.Object(id=int(after_message_id))
            existing = await self._find(channel, anchor, publication, nonce, marker)
            if existing is not None:
                return str(existing.id)

            file = (
                None
                if publication.attachment is None
                else discord.File(io.BytesIO(publication.attachment), filename=publication.filename)
            )
            options: _MessageOptions = {}
            if publication.custom_id is not None:
                options["view"] = _offer_view(publication.custom_id)
            if anchor is not None and publication.reply_to_anchor:
                options["reference"] = discord.MessageReference(
                    message_id=anchor.id,
                    channel_id=channel.id,
                    guild_id=channel.guild.id,
                    fail_if_not_exists=True,
                )
            try:
                message = await channel.send(
                    publication.content,
                    nonce=nonce,
                    allowed_mentions=discord.AllowedMentions.none(),
                    embeds=[] if marker is None else [_marker_embed(marker)],
                    files=[] if file is None else [file],
                    **options,
                )
            finally:
                if file is not None:
                    file.close()
            self._validate_message(message, channel, publication, nonce, marker, anchor)
            return str(message.id)

    async def _find(
        self,
        channel: discord.TextChannel,
        anchor: discord.Object | None,
        publication: _Publication,
        nonce: str,
        marker: str | None,
    ) -> discord.Message | None:
        checked = 0
        async for message in channel.history(
            limit=_HISTORY_LIMIT + 1,
            after=anchor,
            oldest_first=anchor is not None,
        ):
            checked += 1
            if message.author.id != self._author_id:
                continue
            is_match = (
                str(message.nonce) == nonce
                or (marker is not None and _has_marker(message, marker))
                or (
                    publication.reply_to_anchor
                    and anchor is not None
                    and message.reference is not None
                    and message.reference.message_id == anchor.id
                    and message.content == publication.content
                )
                or (
                    publication.custom_id is not None
                    and _offer_button_id(message) == publication.custom_id
                )
                or (
                    publication.filename is not None
                    and any(item.filename == publication.filename for item in message.attachments)
                )
            )
            if is_match:
                self._validate_message(message, channel, publication, nonce, marker, anchor)
                return message
        if checked > _HISTORY_LIMIT:
            raise DiscordDeliveryConflict
        return None

    async def _channel(
        self,
        guild_id: str,
        channel_id: str,
        *,
        attach_files: bool = False,
        embed_links: bool = False,
        send_messages: bool = True,
    ) -> discord.TextChannel:
        if not self._config.allows(guild_id=guild_id, channel_id=channel_id):
            raise DiscordPermissionDenied
        user = self._client.user
        if not self._client.is_ready() or user is None or user.id != self._author_id:
            raise DiscordIdentityUnavailable
        channel = self._client.get_channel(int(channel_id))
        if channel is None:
            channel = await self._client.fetch_channel(int(channel_id))
        if (
            not isinstance(channel, discord.TextChannel)
            or channel.type != discord.ChannelType.text
            or str(channel.id) != channel_id
            or str(channel.guild.id) != guild_id
        ):
            raise DiscordPermissionDenied
        member = channel.guild.me
        if member is None or member.id != self._author_id:
            raise DiscordPermissionDenied
        permissions = channel.permissions_for(member)
        if not (
            permissions.view_channel
            and (not send_messages or permissions.send_messages)
            and permissions.read_message_history
            and (not embed_links or permissions.embed_links)
            and (not attach_files or permissions.attach_files)
        ):
            raise DiscordPermissionDenied
        return channel

    def _require_message_target(
        self, message: discord.Message, channel: discord.TextChannel
    ) -> None:
        if message.channel.id != channel.id or message.author.id != self._author_id:
            raise DiscordDeliveryConflict

    def _validate_message(
        self,
        message: discord.Message,
        channel: discord.TextChannel,
        publication: _Publication,
        nonce: str,
        marker: str | None,
        anchor: discord.Object | None,
    ) -> None:
        self._require_message_target(message, channel)
        if (
            message.content != publication.content
            or (message.nonce is not None and str(message.nonce) != nonce)
            or _offer_button_id(message) != publication.custom_id
            or (marker is not None and not _has_marker(message, marker))
            or (
                publication.reply_to_anchor
                and anchor is not None
                and (
                    message.reference is None
                    or message.reference.message_id != anchor.id
                    or message.reference.channel_id != channel.id
                )
            )
        ):
            raise DiscordDeliveryConflict
        if publication.filename is None:
            if message.attachments:
                raise DiscordDeliveryConflict
            return
        if len(message.attachments) != 1:
            raise DiscordDeliveryConflict
        attachment = message.attachments[0]
        if (
            attachment.filename != publication.filename
            or attachment.size <= 0
            or (
                attachment.content_type is not None
                and attachment.content_type != publication.content_type
            )
        ):
            raise DiscordDeliveryConflict


@asynccontextmanager
async def _delivery_boundary() -> AsyncGenerator[None]:
    try:
        async with asyncio.timeout(_DELIVERY_TIMEOUT_SECONDS):
            yield
    except DiscordDeliveryConflict:
        raise GachaDeliveryFailure(retryable=False, ambiguous=True) from None
    except DiscordAdapterError as error:
        raise GachaDeliveryFailure(retryable=error.retryable) from None
    except discord.RateLimited:
        raise GachaDeliveryFailure(retryable=True) from None
    except discord.HTTPException as error:
        ambiguous = error.status >= 500
        raise GachaDeliveryFailure(
            retryable=ambiguous or error.status == 429,
            ambiguous=ambiguous,
        ) from None
    except OSError, TimeoutError:
        raise GachaDeliveryFailure(retryable=True, ambiguous=True) from None


def _nonce(publication_id: str) -> str:
    if not publication_id:
        raise ValueError("student gacha publication ID is empty")
    digest = hashlib.sha256(f"student-gacha:{publication_id}".encode()).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def _loading_publication(delivery_id: str, content: bytes) -> _Publication:
    if not content or len(content) > _MAX_ATTACHMENT_BYTES:
        raise GachaDeliveryFailure(retryable=False)
    return _Publication(_LOADING_TEXT, f"show-{_nonce(delivery_id)}.gif", "image/gif", content)


def _marker_embed(marker: str) -> discord.Embed:
    return discord.Embed().set_footer(text=marker)


def _has_marker(message: discord.Message, marker: str) -> bool:
    return len(message.embeds) == 1 and message.embeds[0].footer.text == marker


def _offer_view(custom_id: str, *, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="１回募集",
            style=discord.ButtonStyle.primary,
            custom_id=custom_id,
            disabled=disabled,
        )
    )
    return view


def _offer_button_id(message: discord.Message) -> str | None:
    if len(message.components) != 1:
        return None
    row = message.components[0]
    if not isinstance(row, discord.ActionRow) or len(row.children) != 1:
        return None
    button = row.children[0]
    if not isinstance(button, discord.Button) or button.custom_id is None:
        return None
    if not button.custom_id.startswith(STUDENT_GACHA_BUTTON_PREFIX):
        return None
    binding = button.custom_id.removeprefix(STUDENT_GACHA_BUTTON_PREFIX)
    return button.custom_id if STUDENT_GACHA_OFFER_BUTTON.fullmatch(binding) else None
