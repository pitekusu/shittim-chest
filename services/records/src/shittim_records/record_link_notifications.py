"""Post one durable Web record link after an Archive projection succeeds."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from shittim_chest.application import DebateSnapshot
from shittim_chest.application.status_publication import (
    DiscordStatusGateway,
    DiscordStatusMessage,
)

RECORD_LINK_NOTIFICATION_PK = "RECORD_LINK_NOTIFICATION"
RECORD_LINK_NOTIFICATION_SCHEMA_VERSION = 1

_OPAQUE_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_PUBLIC_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+\Z"
)


class RecordLinkNotificationState(StrEnum):
    """Persisted delivery state for one content-free record-link receipt."""

    PENDING = "pending"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class RecordLinkNotificationReceipt:
    """Validated content-free delivery receipt."""

    record_id: str
    source_fingerprint: str
    state: RecordLinkNotificationState
    attempted: bool


class RecordLinkNotificationStore(Protocol):
    """Persist only delivery state; Discord identifiers stay in the source aggregate."""

    def load(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
    ) -> RecordLinkNotificationReceipt | None: ...

    def mark_sent(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: datetime,
    ) -> None: ...

    def mark_attempted(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: datetime,
    ) -> None: ...


class RecordLinkGatewayFactory(Protocol):
    """Create a moderator gateway after validating the source Discord boundary."""

    async def create(self, snapshot: DebateSnapshot) -> DiscordStatusGateway: ...


class RecordLinkNotificationService:
    """Converge one post-projection link message in the original chat channel."""

    def __init__(
        self,
        *,
        store: RecordLinkNotificationStore,
        gateway_factory: RecordLinkGatewayFactory,
        public_hostname: str,
    ) -> None:
        if _PUBLIC_HOSTNAME.fullmatch(public_hostname) is None:
            raise ValueError("Records public hostname is invalid")
        self._store = store
        self._gateway_factory = gateway_factory
        self._public_hostname = public_hostname

    def publish(
        self,
        *,
        snapshot: DebateSnapshot,
        record_id: str,
        source_fingerprint: str,
        now: datetime,
    ) -> None:
        """Synchronously publish from the synchronous Lambda stream handler."""

        asyncio.run(
            self._publish(
                snapshot=snapshot,
                record_id=record_id,
                source_fingerprint=source_fingerprint,
                now=now,
            )
        )

    async def _publish(
        self,
        *,
        snapshot: DebateSnapshot,
        record_id: str,
        source_fingerprint: str,
        now: datetime,
    ) -> None:
        receipt = self._store.load(
            record_id=record_id,
            source_fingerprint=source_fingerprint,
        )
        if receipt is None or receipt.state is RecordLinkNotificationState.SENT:
            return
        if snapshot.starter_message_id is None:
            raise ValueError("completed debate has no starter message")
        _require_record_id(record_id)
        if len(source_fingerprint) != 64:
            raise ValueError("source fingerprint is invalid")

        gateway = await self._gateway_factory.create(snapshot)
        expected_author = await gateway.current_bot_user_id()
        nonce = _notification_nonce(record_id)
        content = record_link_message(self._public_hostname, record_id)
        message = None
        if receipt.attempted:
            message = await gateway.find_by_nonce(
                channel_id=snapshot.channel_id,
                author_id=expected_author,
                nonce=nonce,
                operation_marker=record_id,
                after_message_id=snapshot.starter_message_id,
                checkpoint=None,
            )
        if message is None:
            self._store.mark_attempted(
                record_id=record_id,
                source_fingerprint=source_fingerprint,
                at=now,
            )
            message = await gateway.create_message(
                channel_id=snapshot.channel_id,
                content=content,
                nonce=nonce,
            )
        _validate_message(
            message,
            channel_id=snapshot.channel_id,
            author_id=expected_author,
            content=content,
            nonce=nonce,
        )
        self._store.mark_sent(
            record_id=record_id,
            source_fingerprint=source_fingerprint,
            at=now,
        )


def pending_record_link_notification(
    *,
    record_id: str,
    source_fingerprint: str,
    created_at: datetime,
) -> dict[str, object]:
    """Build the content-free item written atomically with the Archive rows."""

    _require_record_id(record_id)
    if len(source_fingerprint) != 64:
        raise ValueError("source fingerprint is invalid")
    return {
        "PK": RECORD_LINK_NOTIFICATION_PK,
        "SK": record_id,
        "record_type": "record_link_notification",
        "schema_version": RECORD_LINK_NOTIFICATION_SCHEMA_VERSION,
        "source_fingerprint": source_fingerprint,
        "state": RecordLinkNotificationState.PENDING.value,
        "attempted": False,
        "created_at": _timestamp(created_at),
    }


def record_link_message(public_hostname: str, record_id: str) -> str:
    """Render fixed prose without question text, mentions, or private identifiers."""

    if _PUBLIC_HOSTNAME.fullmatch(public_hostname) is None:
        raise ValueError("Records public hostname is invalid")
    _require_record_id(record_id)
    url = f"https://{public_hostname}/records/{record_id}"
    return f"議論結果はこちらからも確認できます。\n[Webで議論結果を見る]({url})"


def _notification_nonce(record_id: str) -> str:
    digest = hashlib.sha256(f"record-link:{record_id}".encode()).digest()
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"record-{encoded[:18]}"


def _validate_message(
    message: DiscordStatusMessage,
    *,
    channel_id: str,
    author_id: str,
    content: str,
    nonce: str,
) -> None:
    if (
        message.channel_id != channel_id
        or message.author_id != author_id
        or message.content != content
        or (message.nonce is not None and message.nonce != nonce)
    ):
        raise ValueError("Discord record-link response is inconsistent")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record-link timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_record_id(record_id: str) -> None:
    if _OPAQUE_RECORD_ID.fullmatch(record_id) is None:
        raise ValueError("record ID is invalid")


__all__ = (
    "RECORD_LINK_NOTIFICATION_PK",
    "RECORD_LINK_NOTIFICATION_SCHEMA_VERSION",
    "RecordLinkGatewayFactory",
    "RecordLinkNotificationReceipt",
    "RecordLinkNotificationService",
    "RecordLinkNotificationState",
    "RecordLinkNotificationStore",
    "pending_record_link_notification",
    "record_link_message",
)
