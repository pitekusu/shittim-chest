"""Discord-facing contracts that do not depend on a Discord or AWS SDK."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from uuid import RFC_4122, UUID

from shittim_chest.domain import AttemptId, DebateId

DISCORD_MESSAGE_LIMIT = 2_000
DISCORD_CUSTOM_ID_LIMIT = 100
DISCORD_NONCE_LIMIT = 25

_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}\Z")
_OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{1,20}\Z")


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


def _require_snowflake(value: str, *, label: str) -> None:
    if _SNOWFLAKE_PATTERN.fullmatch(value) is None or not 0 < int(value) < 2**64:
        raise ValueError(f"{label} must be a positive unsigned 64-bit Discord snowflake")


@unique
class DiscordBotSlot(StrEnum):
    """Public-safe identity slots mapped to private runtime Application IDs."""

    MODERATOR = "moderator"
    PARTICIPANT_A = "participant-a"
    PARTICIPANT_B = "participant-b"
    PARTICIPANT_C = "participant-c"


DISCORD_BOT_SLOTS = tuple(DiscordBotSlot)


@unique
class DiscordErrorCode(StrEnum):
    """Stable public error codes produced at the Discord boundary."""

    WRONG_GUILD = "DISCORD_WRONG_GUILD"
    CHANNEL_NOT_ALLOWED = "DISCORD_CHANNEL_NOT_ALLOWED"
    BOTS_NOT_READY = "DISCORD_BOTS_NOT_READY"
    THREAD_CREATE_FAILED = "DISCORD_THREAD_CREATE_FAILED"
    THREAD_LOCKED = "DISCORD_THREAD_LOCKED"
    PERMISSION_DENIED = "DISCORD_PERMISSION_DENIED"


@dataclass(frozen=True, slots=True)
class DiscordIdentityConfig:
    """Bind one generic Bot slot to its runtime Application ID."""

    slot: DiscordBotSlot
    application_id: str

    def __post_init__(self) -> None:
        _require_snowflake(self.application_id, label="Application ID")


@dataclass(frozen=True, slots=True)
class DiscordRuntimeConfig:
    """Fail-closed public-Guild boundary without Bot tokens or persona content."""

    guild_id: str
    allowed_channel_ids: frozenset[str]
    identities: tuple[DiscordIdentityConfig, ...]
    schema_version: str

    def __post_init__(self) -> None:
        _require_snowflake(self.guild_id, label="Guild ID")
        _require_text(self.schema_version, label="runtime config schema version")
        if not self.allowed_channel_ids:
            raise ValueError("allowed channel IDs must not be empty")
        for channel_id in self.allowed_channel_ids:
            _require_snowflake(channel_id, label="channel ID")
        slots = tuple(identity.slot for identity in self.identities)
        if len(slots) != len(DISCORD_BOT_SLOTS) or set(slots) != set(DISCORD_BOT_SLOTS):
            raise ValueError("runtime config must contain each Discord Bot slot exactly once")
        application_ids = tuple(identity.application_id for identity in self.identities)
        if len(set(application_ids)) != len(application_ids):
            raise ValueError("Discord Application IDs must be distinct")

    def allows(self, *, guild_id: str, channel_id: str) -> bool:
        """Return the deterministic Guild/channel allowlist decision."""

        return guild_id == self.guild_id and channel_id in self.allowed_channel_ids

    def application_id_for(self, slot: DiscordBotSlot) -> str:
        """Resolve a generic slot without exposing identity mapping in public source."""

        return next(
            identity.application_id for identity in self.identities if identity.slot is slot
        )


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
    bot_slot: DiscordBotSlot
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
        _require_text(self.operation_id, label="operation ID")
        _require_snowflake(self.thread_id, label="thread ID")
        _require_text(self.content, label="content")
        if len(self.content) > DISCORD_MESSAGE_LIMIT:
            raise ValueError("outbox content must be at most 2000 characters")
        if _SHA256_PATTERN.fullmatch(self.content_hash) is None:
            raise ValueError("content hash must be a lowercase SHA-256 hexadecimal digest")
        if _NONCE_PATTERN.fullmatch(self.nonce) is None:
            raise ValueError("nonce must be 22 unpadded base64url characters")
        if len(self.nonce) > DISCORD_NONCE_LIMIT:
            raise ValueError("nonce exceeds Discord's 25-character limit")
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
            _require_snowflake(self.message_id, label="message ID")
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
        _require_text(self.operation_id, label="operation ID")
        for label, value in (
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
            ("requester ID", self.requester_id),
        ):
            _require_text(value, label=label)
        _require_utc(self.created_at, label="panel operation timestamp")
        if self.thread_id is not None:
            _require_snowflake(self.thread_id, label="thread ID")
        if self.message_id is not None:
            _require_snowflake(self.message_id, label="control panel message ID")
        if self.kind is PanelOperationKind.RETRY:
            if self.source_attempt_id == self.result_attempt_id:
                raise ValueError("retry operation requires a new result attempt")
        elif self.source_attempt_id != self.result_attempt_id:
            raise ValueError("non-retry operation must preserve its attempt ID")


@unique
class PanelAction(StrEnum):
    """User-selectable actions represented in Discord component custom IDs."""

    CANCEL = "cancel"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class PanelCustomId:
    """Validated and reversible control-panel component identity."""

    debate_id: DebateId
    operation_id: str
    action: PanelAction

    def __post_init__(self) -> None:
        if _OPERATION_ID_PATTERN.fullmatch(self.operation_id) is None:
            raise ValueError("panel operation ID must be 1-36 base64url-safe characters")
        if len(self.encode()) > DISCORD_CUSTOM_ID_LIMIT:
            raise ValueError("panel custom ID exceeds Discord's 100-character limit")

    def encode(self) -> str:
        """Encode the stable v1 component identifier."""

        return f"shittim:v1:{self.debate_id}:{self.operation_id}:{self.action.value}"

    @classmethod
    def parse(cls, value: str) -> PanelCustomId:
        """Parse a component identifier and fail closed on another namespace/version."""

        if len(value) > DISCORD_CUSTOM_ID_LIMIT:
            raise ValueError("panel custom ID exceeds Discord's 100-character limit")
        parts = value.split(":")
        if len(parts) != 5 or parts[0:2] != ["shittim", "v1"]:
            raise ValueError("unsupported panel custom ID")
        try:
            action = PanelAction(parts[4])
            debate_id = DebateId.parse(parts[2])
        except ValueError as error:
            raise ValueError("invalid panel custom ID") from error
        return cls(debate_id=debate_id, operation_id=parts[3], action=action)


def nonce_from_uuid7(value: UUID) -> str:
    """Encode one RFC 9562 UUIDv7 as a 22-character unpadded base64url nonce."""

    if value.version != 7 or value.variant != RFC_4122:
        raise ValueError("Discord nonce source must be an RFC 9562 UUIDv7")
    nonce = base64.urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")
    if _NONCE_PATTERN.fullmatch(nonce) is None:
        raise AssertionError("UUIDv7 nonce encoding violated its fixed contract")
    return nonce


def content_sha256(content: str) -> str:
    """Return the UTF-8 content digest used for delivery reconciliation."""

    _require_text(content, label="content")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def split_discord_message(content: str) -> tuple[str, ...]:
    """Split content deterministically, preferring paragraph, line, then word boundaries."""

    normalized = content.strip()
    _require_text(normalized, label="content")
    chunks = _split_with_limit(normalized, DISCORD_MESSAGE_LIMIT)
    if len(chunks) == 1:
        return chunks

    while True:
        total = len(chunks)
        prefix_length = len(f"[{total}/{total}] ")
        chunks_with_room = _split_with_limit(normalized, DISCORD_MESSAGE_LIMIT - prefix_length)
        if len(chunks_with_room) == total:
            return tuple(
                f"[{index}/{total}] {chunk}" for index, chunk in enumerate(chunks_with_room, 1)
            )
        chunks = chunks_with_room


def prepare_outbox_operations(
    *,
    operation_prefix: str,
    debate_id: DebateId,
    attempt_id: AttemptId,
    bot_slot: DiscordBotSlot,
    thread_id: str,
    content: str,
    nonce_sources: tuple[UUID, ...],
    created_at: datetime,
) -> tuple[OutboxOperation, ...]:
    """Build deterministic, content-addressed prepared operations for one logical post."""

    _require_text(operation_prefix, label="operation prefix")
    chunks = split_discord_message(content)
    if len(nonce_sources) != len(chunks):
        raise ValueError("one UUIDv7 nonce source is required for each Discord chunk")
    return tuple(
        OutboxOperation(
            operation_id=f"{operation_prefix}-{sequence:04d}",
            debate_id=debate_id,
            attempt_id=attempt_id,
            bot_slot=bot_slot,
            thread_id=thread_id,
            content=chunk,
            content_hash=content_sha256(chunk),
            nonce=nonce_from_uuid7(nonce_sources[sequence]),
            chunk_sequence=sequence,
            status=OutboxStatus.PREPARED,
            created_at=created_at,
        )
        for sequence, chunk in enumerate(chunks)
    )


def _split_with_limit(content: str, limit: int) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        split_at = _preferred_split(remaining, limit)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    chunks.append(remaining)
    return tuple(chunks)


def _preferred_split(content: str, limit: int) -> int:
    for separator in ("\n\n", "\n", " "):
        position = content.rfind(separator, 0, limit + 1)
        if position > 0:
            return position
    return limit
