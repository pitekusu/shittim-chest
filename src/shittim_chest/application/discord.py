"""Discord-facing contracts that do not depend on a Discord or AWS SDK."""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique
from uuid import RFC_4122, UUID

from shittim_chest.application.models import DebateSnapshot, DeliveryAbandonReason
from shittim_chest.domain import (
    PARTICIPANTS,
    AttemptId,
    DebateId,
    DebatePhase,
    ParticipantSlot,
    select_winner,
)

DISCORD_MESSAGE_LIMIT = 2_000
DISCORD_CUSTOM_ID_LIMIT = 100
DISCORD_NONCE_LIMIT = 25
OUTBOX_CLAIM_SECONDS = 60
MAX_TERMINAL_OUTBOX_CHUNKS = 20
MAX_COMPLETED_RESULT_CHUNKS = 1
MAX_COMPLETED_DECISION_CHUNKS = MAX_TERMINAL_OUTBOX_CHUNKS - MAX_COMPLETED_RESULT_CHUNKS
MAX_INITIAL_OPINION_CHUNKS = 8
MAX_FINAL_PROPOSAL_CHUNKS = 8
MAX_VOTE_CHUNKS = 8
MAX_OUTBOX_DELIVERY_ATTEMPTS = 3
INITIAL_OPINION_DELIVERY_SEQUENCE_START = 0
FINAL_PROPOSAL_DELIVERY_SEQUENCE_START = 100
VOTE_DELIVERY_SEQUENCE_START = 200
COMPLETED_DELIVERY_SEQUENCE_START = 300
FAILED_DELIVERY_SEQUENCE_START = 900
CANCELLED_DELIVERY_SEQUENCE_START = 910
MAX_TERMINAL_NOTICE_CHUNKS = 10
_MODEL_DISPLAY_LINE_LIMIT = 1_800

_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{22}\Z")
_OPERATION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,36}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{1,20}\Z")
_TERMINAL_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9_:-]{1,128}\Z")
_DISCORD_MARKDOWN_CHARACTERS = frozenset("\\`*_{}[]()<>#+-.!|>~=")


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
    THREAD_UNAVAILABLE = "DISCORD_THREAD_UNAVAILABLE"
    THREAD_LOCKED = "DISCORD_THREAD_LOCKED"
    PERMISSION_DENIED = "DISCORD_PERMISSION_DENIED"
    OUTBOX_NOT_FOUND = "DISCORD_OUTBOX_NOT_FOUND"
    OUTBOX_CONFLICT = "DISCORD_OUTBOX_CONFLICT"
    RATE_LIMITED = "DISCORD_RATE_LIMITED"
    UNAVAILABLE = "DISCORD_UNAVAILABLE"
    DELIVERY_REJECTED = "DISCORD_DELIVERY_REJECTED"


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
    ABANDONED = "abandoned"


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
    record_schema_version: int = 1
    phase: DebatePhase | None = None
    plan_id: str | None = None
    delivery_sequence: int | None = None
    deadline_at: datetime | None = None
    abandoned_at: datetime | None = None
    abandon_reason: DeliveryAbandonReason | None = None

    def __post_init__(self) -> None:
        _require_text(self.operation_id, label="operation ID")
        _require_snowflake(self.thread_id, label="thread ID")
        _require_text(self.content, label="content")
        if len(self.content) > DISCORD_MESSAGE_LIMIT:
            raise ValueError("outbox content must be at most 2000 characters")
        if _SHA256_PATTERN.fullmatch(self.content_hash) is None:
            raise ValueError("content hash must be a lowercase SHA-256 hexadecimal digest")
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.content_hash:
            raise ValueError("content hash must match the UTF-8 message content")
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
        if self.record_schema_version not in {1, 2}:
            raise ValueError("unsupported outbox record schema")
        if self.record_schema_version == 2:
            if (
                self.phase is None
                or self.plan_id is None
                or self.delivery_sequence is None
                or self.deadline_at is None
            ):
                raise ValueError("outbox v2 requires phase, plan, sequence, and deadline")
            _require_text(self.plan_id, label="delivery plan ID")
            if (
                isinstance(self.delivery_sequence, bool)
                or not isinstance(self.delivery_sequence, int)
                or self.delivery_sequence < 0
            ):
                raise ValueError("delivery sequence must be a non-negative integer")
            if self.delivery_attempt > MAX_OUTBOX_DELIVERY_ATTEMPTS:
                raise ValueError("outbox v2 delivery attempt exceeds its bound")
            _require_utc(self.deadline_at, label="outbox deadline")
            if self.deadline_at != self.created_at + timedelta(minutes=15):
                raise ValueError("outbox deadline must be exactly 15 minutes after creation")
        elif any(
            value is not None
            for value in (self.phase, self.plan_id, self.delivery_sequence, self.deadline_at)
        ):
            raise ValueError("outbox v1 cannot contain v2 delivery fields")
        _require_utc(self.created_at, label="outbox creation timestamp")
        for label, timestamp in (
            ("claim expiry", self.claim_expires_at),
            ("next retry timestamp", self.next_retry_at),
            ("sent timestamp", self.sent_at),
            ("abandoned timestamp", self.abandoned_at),
        ):
            if timestamp is not None:
                _require_utc(timestamp, label=label)
        if (self.claim_owner is None) is not (self.claim_expires_at is None):
            raise ValueError("claim owner and expiry must be set together")
        if self.status is not OutboxStatus.SENT and (
            self.message_id is not None or self.sent_at is not None
        ):
            raise ValueError("only a sent outbox operation may contain delivery result fields")
        if self.status is not OutboxStatus.ABANDONED and (
            self.abandoned_at is not None or self.abandon_reason is not None
        ):
            raise ValueError("only an abandoned operation may contain abandonment fields")
        if self.status is OutboxStatus.PREPARED:
            if self.claim_owner is not None:
                raise ValueError("prepared outbox operation cannot retain a claim")
            if self.delivery_attempt == 0 and self.next_retry_at is not None:
                raise ValueError("unattempted outbox operation cannot have a retry time")
        elif self.status is OutboxStatus.CLAIMED:
            if self.claim_owner is None or self.delivery_attempt < 1:
                raise ValueError("claimed outbox operation requires an attempted owner and expiry")
            if self.next_retry_at is not None:
                raise ValueError("claimed outbox operation cannot retain a retry time")
        elif self.status is OutboxStatus.SENT:
            if self.message_id is None or self.sent_at is None:
                raise ValueError("sent outbox operation requires message ID and sent timestamp")
            if self.delivery_attempt < 1:
                raise ValueError("sent outbox operation requires a positive delivery attempt")
            _require_snowflake(self.message_id, label="message ID")
            if any(
                value is not None
                for value in (
                    self.claim_owner,
                    self.claim_expires_at,
                    self.next_retry_at,
                    self.abandoned_at,
                    self.abandon_reason,
                )
            ):
                raise ValueError("sent operation cannot retain delivery or abandonment state")
        elif self.status is OutboxStatus.ABANDONED:
            if self.record_schema_version != 2:
                raise ValueError("only outbox v2 may be abandoned")
            if self.abandoned_at is None or self.abandon_reason is None:
                raise ValueError("abandoned operation requires a timestamp and reason")
            if self.abandoned_at < self.created_at:
                raise ValueError("outbox abandonment cannot precede creation")
            if any(
                value is not None
                for value in (
                    self.claim_owner,
                    self.claim_expires_at,
                    self.next_retry_at,
                    self.message_id,
                    self.sent_at,
                )
            ):
                raise ValueError("abandoned operation cannot retain delivery state")


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

    @classmethod
    def for_attempt(
        cls,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        action: PanelAction,
    ) -> PanelCustomId:
        """Build a stable panel operation ID bound to one immutable attempt."""

        suffix = "c" if action is PanelAction.CANCEL else "r"
        return cls(
            debate_id=debate_id,
            operation_id=f"{attempt_id.value.hex}{suffix}",
            action=action,
        )

    def expected_attempt_id(self) -> AttemptId:
        """Recover and validate the immutable source attempt encoded by this panel ID."""

        expected_suffix = "c" if self.action is PanelAction.CANCEL else "r"
        if len(self.operation_id) != 33 or self.operation_id[-1] != expected_suffix:
            raise ValueError("panel operation ID is not bound to its action")
        try:
            return AttemptId.parse(self.operation_id[:32])
        except ValueError as error:
            raise ValueError("panel operation ID does not contain a UUIDv7 attempt") from error


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
        prefix_length = len(f"[{total}/{total}]\n")
        chunks_with_room = _split_with_limit(normalized, DISCORD_MESSAGE_LIMIT - prefix_length)
        if len(chunks_with_room) == total:
            return tuple(
                f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks_with_room, 1)
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
    record_schema_version: int = 1,
    phase: DebatePhase | None = None,
    plan_id: str | None = None,
    delivery_sequence_start: int | None = None,
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
            record_schema_version=record_schema_version,
            phase=phase,
            plan_id=plan_id,
            delivery_sequence=(
                None if delivery_sequence_start is None else delivery_sequence_start + sequence
            ),
            deadline_at=(
                None if record_schema_version == 1 else created_at + timedelta(minutes=15)
            ),
        )
        for sequence, chunk in enumerate(chunks)
    )


def prepare_terminal_outbox_operations(
    *,
    snapshot: DebateSnapshot,
    target_phase: DebatePhase,
    created_at: datetime,
    error_code: str | None = None,
    participant_display_names: Mapping[ParticipantSlot, str] | None = None,
) -> tuple[OutboxOperation, ...]:
    """Build deterministic required delivery for one terminal outcome."""

    _require_utc(created_at, label="terminal delivery creation timestamp")
    if snapshot.state.phase.is_terminal:
        raise ValueError("terminal delivery must be staged from an active attempt")
    if snapshot.thread_id is None:
        raise ValueError("terminal delivery requires a bound Discord thread")
    content = _terminal_content(snapshot, target_phase=target_phase, error_code=error_code)
    chunks = split_discord_message(content)
    if target_phase is DebatePhase.COMPLETED:
        decision = snapshot.final_decision
        if decision is None:  # pragma: no cover - content validation above is authoritative
            raise ValueError("completed delivery requires a final decision")
        if participant_display_names is None:
            raise ValueError("completed delivery requires participant display names")
        display_names = dict(participant_display_names)
        if set(display_names) != set(PARTICIPANTS):
            raise ValueError(
                "completed delivery requires each participant display name exactly once"
            )
        voting_result = select_winner(snapshot.votes)
        if decision.winner is not voting_result.winner:
            raise ValueError("completed delivery winner conflicts with the durable ballot")
        result_content = _completed_result_content(
            snapshot,
            participant_display_names=display_names,
        )
        result_chunks = split_discord_message(result_content)
        if len(result_chunks) != MAX_COMPLETED_RESULT_CHUNKS:
            raise ValueError("completed result announcement must fit in one Discord message")
        if len(chunks) > MAX_COMPLETED_DECISION_CHUNKS:
            raise ValueError("completed decision exceeds the bounded chunk count")
        plan_id = "terminal-completed"
        winner_bot_slot = DiscordBotSlot(decision.winner.value)
        result_operations = prepare_outbox_operations(
            operation_prefix="terminal-completed-result",
            debate_id=snapshot.state.debate_id,
            attempt_id=snapshot.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id=snapshot.thread_id,
            content=result_content,
            nonce_sources=(
                _derived_uuid7(
                    snapshot.state.attempt_id,
                    phase=target_phase,
                    bot_slot=DiscordBotSlot.MODERATOR,
                    sequence=0,
                ),
            ),
            created_at=created_at,
            record_schema_version=2,
            phase=target_phase,
            plan_id=plan_id,
            delivery_sequence_start=COMPLETED_DELIVERY_SEQUENCE_START,
        )
        decision_operations = prepare_outbox_operations(
            operation_prefix="terminal-completed-decision",
            debate_id=snapshot.state.debate_id,
            attempt_id=snapshot.state.attempt_id,
            bot_slot=winner_bot_slot,
            thread_id=snapshot.thread_id,
            content=content,
            nonce_sources=tuple(
                _derived_uuid7(
                    snapshot.state.attempt_id,
                    phase=target_phase,
                    bot_slot=winner_bot_slot,
                    sequence=sequence,
                )
                for sequence in range(len(chunks))
            ),
            created_at=created_at,
            record_schema_version=2,
            phase=target_phase,
            plan_id=plan_id,
            delivery_sequence_start=(
                COMPLETED_DELIVERY_SEQUENCE_START + MAX_COMPLETED_RESULT_CHUNKS
            ),
        )
        return (*result_operations, *decision_operations)
    if len(chunks) > MAX_TERMINAL_OUTBOX_CHUNKS:
        raise ValueError("terminal delivery exceeds the bounded chunk count")
    if (
        target_phase in {DebatePhase.FAILED, DebatePhase.CANCELLED}
        and len(chunks) > MAX_TERMINAL_NOTICE_CHUNKS
    ):
        raise ValueError("terminal notice exceeds its reserved delivery sequence range")
    operation_prefix = f"terminal-{target_phase.value}"
    plan_id = operation_prefix
    delivery_sequence_start = {
        DebatePhase.COMPLETED: COMPLETED_DELIVERY_SEQUENCE_START,
        DebatePhase.FAILED: FAILED_DELIVERY_SEQUENCE_START,
        DebatePhase.CANCELLED: CANCELLED_DELIVERY_SEQUENCE_START,
    }.get(target_phase)
    if delivery_sequence_start is None:
        raise ValueError("terminal delivery target must be completed, failed, or cancelled")
    bot_slot = DiscordBotSlot.MODERATOR
    nonce_sources = tuple(
        _derived_uuid7(
            snapshot.state.attempt_id,
            phase=target_phase,
            bot_slot=bot_slot,
            sequence=sequence,
        )
        for sequence in range(len(chunks))
    )
    return prepare_outbox_operations(
        operation_prefix=operation_prefix,
        debate_id=snapshot.state.debate_id,
        attempt_id=snapshot.state.attempt_id,
        bot_slot=bot_slot,
        thread_id=snapshot.thread_id,
        content=content,
        nonce_sources=nonce_sources,
        created_at=created_at,
        record_schema_version=2,
        phase=target_phase,
        plan_id=plan_id,
        delivery_sequence_start=delivery_sequence_start,
    )


def prepare_initial_opinion_outbox_operations(
    *,
    snapshot: DebateSnapshot,
    created_at: datetime,
) -> tuple[OutboxOperation, ...]:
    """Build the ordered participant-owned delivery for all three initial opinions."""

    _require_utc(created_at, label="initial opinion delivery creation timestamp")
    if snapshot.state.phase is not DebatePhase.COLLECTING_INITIAL_OPINIONS:
        raise ValueError("initial opinions can only be delivered from their generation phase")
    if snapshot.thread_id is None:
        raise ValueError("initial opinion delivery requires a bound Discord thread")
    opinions = {opinion.participant: opinion for opinion in snapshot.initial_opinions}
    if len(opinions) != len(PARTICIPANTS) or set(opinions) != set(PARTICIPANTS):
        raise ValueError("initial opinion delivery requires each participant exactly once")

    plan_id = "initial-opinions"
    operations: list[OutboxOperation] = []
    for participant_index, participant in enumerate(PARTICIPANTS):
        opinion = opinions[participant]
        bot_slot = DiscordBotSlot(participant.value)
        content = "\n".join(
            (
                "**初回意見**",
                "**要点**",
                _quoted_model_text(opinion.summary),
                "**提案**",
                _quoted_model_text(opinion.proposal),
            )
        )
        chunks = split_discord_message(content)
        if len(chunks) > MAX_INITIAL_OPINION_CHUNKS:
            raise ValueError("one initial opinion exceeds its reserved delivery sequence range")
        sequence_start = (
            INITIAL_OPINION_DELIVERY_SEQUENCE_START + participant_index * MAX_INITIAL_OPINION_CHUNKS
        )
        operations.extend(
            prepare_outbox_operations(
                operation_prefix=f"initial-opinion-{participant.value}",
                debate_id=snapshot.state.debate_id,
                attempt_id=snapshot.state.attempt_id,
                bot_slot=bot_slot,
                thread_id=snapshot.thread_id,
                content=content,
                nonce_sources=tuple(
                    _derived_uuid7(
                        snapshot.state.attempt_id,
                        phase=DebatePhase.COLLECTING_INITIAL_OPINIONS,
                        bot_slot=bot_slot,
                        sequence=sequence,
                    )
                    for sequence in range(len(chunks))
                ),
                created_at=created_at,
                record_schema_version=2,
                phase=DebatePhase.DISCUSSING,
                plan_id=plan_id,
                delivery_sequence_start=sequence_start,
            )
        )
    return tuple(operations)


def prepare_final_proposal_outbox_operations(
    *,
    snapshot: DebateSnapshot,
    created_at: datetime,
) -> tuple[OutboxOperation, ...]:
    """Build the ordered participant-owned delivery for all three final proposals."""

    _require_utc(created_at, label="final proposal delivery creation timestamp")
    if snapshot.state.phase is not DebatePhase.COLLECTING_FINAL_PROPOSALS:
        raise ValueError("final proposals can only be delivered from their generation phase")
    if snapshot.thread_id is None:
        raise ValueError("final proposal delivery requires a bound Discord thread")
    proposals = {proposal.participant: proposal for proposal in snapshot.final_proposals}
    if len(proposals) != len(PARTICIPANTS) or set(proposals) != set(PARTICIPANTS):
        raise ValueError("final proposal delivery requires each participant exactly once")

    plan_id = "final-proposals"
    operations: list[OutboxOperation] = []
    for participant_index, participant in enumerate(PARTICIPANTS):
        proposal = proposals[participant]
        bot_slot = DiscordBotSlot(participant.value)
        content = "\n".join(
            (
                "**最終案**",
                "**タイトル**",
                _quoted_model_text(proposal.title),
                "**提案**",
                _quoted_model_text(proposal.proposal),
            )
        )
        chunks = split_discord_message(content)
        if len(chunks) > MAX_FINAL_PROPOSAL_CHUNKS:
            raise ValueError("one final proposal exceeds its reserved delivery sequence range")
        sequence_start = (
            FINAL_PROPOSAL_DELIVERY_SEQUENCE_START + participant_index * MAX_FINAL_PROPOSAL_CHUNKS
        )
        operations.extend(
            prepare_outbox_operations(
                operation_prefix=f"final-proposal-{participant.value}",
                debate_id=snapshot.state.debate_id,
                attempt_id=snapshot.state.attempt_id,
                bot_slot=bot_slot,
                thread_id=snapshot.thread_id,
                content=content,
                nonce_sources=tuple(
                    _derived_uuid7(
                        snapshot.state.attempt_id,
                        phase=DebatePhase.COLLECTING_FINAL_PROPOSALS,
                        bot_slot=bot_slot,
                        sequence=sequence,
                    )
                    for sequence in range(len(chunks))
                ),
                created_at=created_at,
                record_schema_version=2,
                phase=DebatePhase.SELECTING_WINNER,
                plan_id=plan_id,
                delivery_sequence_start=sequence_start,
            )
        )
    return tuple(operations)


def prepare_vote_outbox_operations(
    *,
    snapshot: DebateSnapshot,
    participant_display_names: Mapping[ParticipantSlot, str],
    created_at: datetime,
) -> tuple[OutboxOperation, ...]:
    """Build the ordered participant-owned delivery for one complete ballot."""

    _require_utc(created_at, label="vote delivery creation timestamp")
    if snapshot.state.phase is not DebatePhase.SELECTING_WINNER:
        raise ValueError("votes can only be delivered from their generation phase")
    if snapshot.thread_id is None:
        raise ValueError("vote delivery requires a bound Discord thread")
    votes = {vote.voter: vote for vote in snapshot.votes}
    if len(votes) != len(PARTICIPANTS) or set(votes) != set(PARTICIPANTS):
        raise ValueError("vote delivery requires each participant exactly once")
    display_names = dict(participant_display_names)
    if set(display_names) != set(PARTICIPANTS):
        raise ValueError("vote delivery requires each participant display name exactly once")

    plan_id = "votes"
    operations: list[OutboxOperation] = []
    for participant_index, participant in enumerate(PARTICIPANTS):
        vote = votes[participant]
        bot_slot = DiscordBotSlot(participant.value)
        content = "\n".join(
            (
                "**投票**",
                "**投票先**",
                _quoted_model_text(display_names[vote.candidate]),
                "**理由**",
                _quoted_model_text(vote.reason),
            )
        )
        chunks = split_discord_message(content)
        if len(chunks) > MAX_VOTE_CHUNKS:
            raise ValueError("one vote exceeds its reserved delivery sequence range")
        sequence_start = VOTE_DELIVERY_SEQUENCE_START + participant_index * MAX_VOTE_CHUNKS
        operations.extend(
            prepare_outbox_operations(
                operation_prefix=f"vote-{participant.value}",
                debate_id=snapshot.state.debate_id,
                attempt_id=snapshot.state.attempt_id,
                bot_slot=bot_slot,
                thread_id=snapshot.thread_id,
                content=content,
                nonce_sources=tuple(
                    _derived_uuid7(
                        snapshot.state.attempt_id,
                        phase=DebatePhase.SELECTING_WINNER,
                        bot_slot=bot_slot,
                        sequence=sequence,
                    )
                    for sequence in range(len(chunks))
                ),
                created_at=created_at,
                record_schema_version=2,
                phase=DebatePhase.GENERATING_DECISION,
                plan_id=plan_id,
                delivery_sequence_start=sequence_start,
            )
        )
    return tuple(operations)


def _terminal_content(
    snapshot: DebateSnapshot,
    *,
    target_phase: DebatePhase,
    error_code: str | None,
) -> str:
    if target_phase is DebatePhase.COMPLETED:
        decision = snapshot.final_decision
        if decision is None or error_code is not None:
            raise ValueError("completed delivery requires a decision without an error")
        sections = []
        if decision.victory_message is not None:
            sections.extend(
                (
                    "**勝利の言葉**",
                    _quoted_model_text(decision.victory_message),
                    "",
                )
            )
        sections.extend(("**最終決定**", _quoted_model_text(decision.decision)))
        if decision.actions:
            sections.extend(
                (
                    "**実行案**",
                    *(_quoted_model_text(action, bullet=True) for action in decision.actions),
                )
            )
        if decision.caveats:
            sections.extend(
                (
                    "**注意点**",
                    *(_quoted_model_text(caveat, bullet=True) for caveat in decision.caveats),
                )
            )
        return "\n".join(sections)
    if target_phase is DebatePhase.FAILED:
        if error_code is None or _TERMINAL_ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("failed delivery requires an error code")
        return "\n".join(
            (
                "**討論を完了できませんでした**",
                f"エラーコード: `{error_code}`",
                "再試行は操作パネルから行えます。",
            )
        )
    if target_phase is DebatePhase.CANCELLED:
        if error_code is not None:
            raise ValueError("cancelled delivery cannot contain an error code")
        return "**討論を中止しました**"
    raise ValueError("terminal delivery target must be completed, failed, or cancelled")


def _completed_result_content(
    snapshot: DebateSnapshot,
    *,
    participant_display_names: Mapping[ParticipantSlot, str],
) -> str:
    """Render the moderator-owned ballot result before the winner speaks."""

    voting_result = select_winner(snapshot.votes)
    vote_counts = {
        participant: sum(vote.candidate is participant for vote in voting_result.votes)
        for participant in PARTICIPANTS
    }
    highest_count = max(vote_counts.values())
    leaders = tuple(
        participant for participant in PARTICIPANTS if vote_counts[participant] == highest_count
    )
    display_names = {
        participant: sanitize_discord_model_text(participant_display_names[participant])
        for participant in PARTICIPANTS
    }
    sections = [
        "**投票結果**",
        *(
            f"- {display_names[participant]}: {vote_counts[participant]}票"
            for participant in PARTICIPANTS
        ),
        "**勝者**",
        f"> {display_names[voting_result.winner]} ({vote_counts[voting_result.winner]}票)",
    ]
    if len(leaders) > 1:
        sections.append("同票のため、規定の評価基準で勝者を決定しました。")
    return "\n".join(sections)


def _derived_uuid7(
    attempt_id: AttemptId,
    *,
    phase: DebatePhase,
    bot_slot: DiscordBotSlot,
    sequence: int,
) -> UUID:
    """Derive a replay-stable UUIDv7 nonce while retaining attempt time bits."""

    if sequence < 0:
        raise ValueError("terminal delivery sequence must be non-negative")
    digest = hashlib.sha256(
        attempt_id.value.bytes
        + phase.value.encode("ascii")
        + bot_slot.value.encode("ascii")
        + sequence.to_bytes(4, "big")
    ).digest()
    raw = bytearray(attempt_id.value.bytes)
    raw[8:] = digest[:8]
    raw[8] = (raw[8] & 0x3F) | 0x80
    derived = UUID(bytes=bytes(raw))
    if derived.version != 7 or derived.variant != RFC_4122:  # pragma: no cover - construction
        raise AssertionError("derived terminal nonce is not an RFC 9562 UUIDv7")
    return derived


def sanitize_discord_model_text(value: str) -> str:
    """Normalize and escape untrusted model text for display-only Discord content."""

    normalized = _normalize_discord_model_text(value)
    return "".join(_escaped_discord_token(character) for character in normalized)


def _normalize_discord_model_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = normalized.replace("\t", " ")
    if not normalized.strip():
        raise ValueError("model display text must not be empty")
    for character in normalized:
        codepoint = ord(character)
        if character != "\n" and (
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
            or 0xFDD0 <= codepoint <= 0xFDEF
            or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}
        ):
            raise ValueError("model display text contains a forbidden Unicode code point")
    return normalized


def _escaped_discord_token(character: str) -> str:
    return f"\\{character}" if character in _DISCORD_MARKDOWN_CHARACTERS else character


def _quoted_model_text(value: str, *, bullet: bool = False) -> str:
    normalized = _normalize_discord_model_text(value)
    prefix = "> - " if bullet else "> "
    rendered: list[str] = []
    for line in normalized.split("\n"):
        tokens = tuple(_escaped_discord_token(character) for character in line)
        if not tokens:
            rendered.append(">")
            continue
        segment: list[str] = []
        segment_length = 0
        for token in tokens:
            if segment and segment_length + len(token) > _MODEL_DISPLAY_LINE_LIMIT:
                rendered.append(prefix + "".join(segment))
                segment = []
                segment_length = 0
            segment.append(token)
            segment_length += len(token)
        rendered.append(prefix + "".join(segment))
    return "\n".join(rendered)


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
