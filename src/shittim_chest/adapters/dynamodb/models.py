"""SDK-independent records used by the DynamoDB outbox and panel operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique

from shittim_chest.domain import AttemptId, DebateId

_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


@unique
class OutboxStatus(StrEnum):
    """Persisted delivery states for one Discord message chunk."""

    PREPARED = "prepared"
    CLAIMED = "claimed"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class OutboxOperation:
    """One content-addressed Discord delivery operation."""

    operation_id: str
    debate_id: DebateId
    attempt_id: AttemptId
    bot_id: str
    thread_id: str
    content: str
    content_hash: str
    nonce: str
    chunk_sequence: int
    status: OutboxStatus
    created_at: datetime
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    delivery_attempt: int = 0
    next_retry_at: datetime | None = None
    message_id: str | None = None
    sent_at: datetime | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("operation ID", self.operation_id),
            ("Bot ID", self.bot_id),
            ("thread ID", self.thread_id),
            ("content", self.content),
        ):
            _require_text(value, label=label)
        if len(self.content) > 2_000:
            raise ValueError("outbox content must be at most 2000 characters")
        if _SHA256_PATTERN.fullmatch(self.content_hash) is None:
            raise ValueError("content hash must be a lowercase SHA-256 hexadecimal digest")
        if _NONCE_PATTERN.fullmatch(self.nonce) is None:
            raise ValueError("nonce must be 22 unpadded base64url characters")
        if (
            isinstance(self.chunk_sequence, bool)
            or not isinstance(self.chunk_sequence, int)
            or self.chunk_sequence < 0
        ):
            raise ValueError("chunk sequence must be a non-negative integer")
        if (
            isinstance(self.delivery_attempt, bool)
            or not isinstance(self.delivery_attempt, int)
            or self.delivery_attempt < 0
        ):
            raise ValueError("delivery attempt must be a non-negative integer")
        _require_utc(self.created_at, label="outbox creation timestamp")
        for label, timestamp in (
            ("claim expiry", self.claim_expires_at),
            ("next retry timestamp", self.next_retry_at),
            ("sent timestamp", self.sent_at),
        ):
            if timestamp is not None:
                _require_utc(timestamp, label=label)
        if (self.claim_owner is None) is not (self.claim_expires_at is None):
            raise ValueError("claim owner and expiry must be set together")
        if self.status is OutboxStatus.CLAIMED and self.claim_owner is None:
            raise ValueError("claimed outbox operation requires an owner and expiry")
        if self.status is OutboxStatus.SENT:
            if self.message_id is None or self.sent_at is None:
                raise ValueError("sent outbox operation requires message ID and sent timestamp")
        elif self.message_id is not None or self.sent_at is not None:
            raise ValueError("only a sent outbox operation may contain delivery result fields")


@unique
class PanelOperationKind(StrEnum):
    """Idempotent Discord control-panel operations."""

    ACCEPT = "accept"
    CANCEL = "cancel"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class PanelOperation:
    """Persisted binding between one Discord operation and its result."""

    operation_id: str
    kind: PanelOperationKind
    debate_id: DebateId
    source_attempt_id: AttemptId
    result_attempt_id: AttemptId
    guild_id: str
    channel_id: str
    requester_id: str
    created_at: datetime
    thread_id: str | None = None
    message_id: str | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("operation ID", self.operation_id),
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
            ("requester ID", self.requester_id),
        ):
            _require_text(value, label=label)
        _require_utc(self.created_at, label="panel operation timestamp")
        if self.thread_id is not None:
            _require_text(self.thread_id, label="thread ID")
        if self.message_id is not None:
            _require_text(self.message_id, label="message ID")
        if self.kind is PanelOperationKind.RETRY:
            if self.source_attempt_id == self.result_attempt_id:
                raise ValueError("retry operation requires a new result attempt")
        elif self.source_attempt_id != self.result_attempt_id:
            raise ValueError("non-retry operation must preserve its attempt ID")
