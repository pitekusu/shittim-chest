"""SDK-free inputs accepted from Discord's signed HTTP interaction boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from shittim_chest.application.discord import PanelAction, PanelCustomId
from shittim_chest.application.scale_to_zero import IngressKind
from shittim_chest.domain import AttemptId, DebateId

SHITTIM_COMMAND_NAME = "shittim"


def _require_text(value: str, *, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_snowflake(value: str, *, label: str) -> None:
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
        raise ValueError(f"{label} must be a positive unsigned 64-bit Discord snowflake")


def _require_utc(value: datetime, *, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class DiscordHttpPing:
    """One authenticated PING that requires no durable application operation."""

    received_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.received_at, label="PING receipt timestamp")


@dataclass(frozen=True, repr=False, slots=True)
class DiscordHttpOperation:
    """Strict, token-free command or component input ready for policy validation."""

    interaction_id: str
    operation_id: str
    kind: IngressKind
    application_id: str
    guild_id: str
    channel_id: str
    channel_type: int | None
    parent_channel_id: str | None
    requester_id: str
    requester_username: str = field(repr=False)
    requester_display_name: str = field(repr=False)
    can_manage_messages: bool
    received_at: datetime
    debate_id: DebateId | None = None
    expected_attempt_id: AttemptId | None = None
    command_name: str | None = None
    question: str | None = field(default=None, repr=False)
    custom_id: str | None = None
    source_message_id: str | None = None
    source_thread_id: str | None = None

    def __post_init__(self) -> None:
        _require_snowflake(self.interaction_id, label="interaction ID")
        _require_text(self.operation_id, label="operation ID")
        for label, value in (
            ("application ID", self.application_id),
            ("Guild ID", self.guild_id),
            ("channel ID", self.channel_id),
            ("requester ID", self.requester_id),
        ):
            _require_snowflake(value, label=label)
        for label, value in (
            ("requester username", self.requester_username),
            ("requester display name", self.requester_display_name),
        ):
            _require_text(value, label=label)
        _require_utc(self.received_at, label="interaction receipt timestamp")
        if self.channel_type is not None and (
            isinstance(self.channel_type, bool)
            or not isinstance(self.channel_type, int)
            or not 0 <= self.channel_type <= 255
        ):
            raise ValueError("channel type must be an unsigned byte when present")
        for label, value in (
            ("parent channel ID", self.parent_channel_id),
            ("source message ID", self.source_message_id),
            ("source thread ID", self.source_thread_id),
        ):
            if value is not None:
                _require_snowflake(value, label=label)
        if not isinstance(self.can_manage_messages, bool):
            raise TypeError("manage-messages decision must be a boolean")
        if self.source_thread_id is not None and self.source_thread_id != self.channel_id:
            raise ValueError("source thread must match the interaction channel")

        if self.kind is IngressKind.NEW_DEBATE:
            if self.operation_id != self.interaction_id:
                raise ValueError("new debate operation must use its interaction ID")
            if self.command_name != SHITTIM_COMMAND_NAME:
                raise ValueError("unsupported application command")
            if self.question is None or not 1 <= len(self.question) <= 1000:
                raise ValueError("new debate requires a 1-1000 character question")
            if not self.question.strip():
                raise ValueError("new debate question must not be blank")
            if any(
                value is not None
                for value in (
                    self.debate_id,
                    self.expected_attempt_id,
                    self.custom_id,
                    self.source_message_id,
                    self.source_thread_id,
                )
            ):
                raise ValueError("new debate cannot contain component context")
            return

        if self.kind not in {IngressKind.RETRY, IngressKind.CANCEL}:
            raise ValueError("unsupported HTTP ingress operation")
        if self.command_name is not None or self.question is not None:
            raise ValueError("component operation cannot contain command input")
        if self.custom_id is None:
            raise ValueError("component operation requires a custom ID")
        _require_text(self.custom_id, label="component custom ID")
        if self.source_message_id is None or self.source_thread_id is None:
            raise ValueError("component operation requires its source message and thread")
        if self.debate_id is None or self.expected_attempt_id is None:
            raise ValueError(
                "component operation requires its immutable debate and attempt binding"
            )
        panel_id = PanelCustomId.parse(self.custom_id)
        expected_action = (
            PanelAction.CANCEL if self.kind is IngressKind.CANCEL else PanelAction.RETRY
        )
        if (
            panel_id.debate_id != self.debate_id
            or panel_id.operation_id != self.operation_id
            or panel_id.action is not expected_action
            or panel_id.expected_attempt_id() != self.expected_attempt_id
        ):
            raise ValueError("component operation does not match its immutable panel binding")


type DiscordHttpInput = DiscordHttpPing | DiscordHttpOperation


__all__ = (
    "SHITTIM_COMMAND_NAME",
    "DiscordHttpInput",
    "DiscordHttpOperation",
    "DiscordHttpPing",
)
