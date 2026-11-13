"""Authorize Discord student recruitment and prepare its interaction responses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.ingress import GUILD_TEXT_CHANNEL

if TYPE_CHECKING:
    from shittim_chest.application.student_gacha_state import GachaOffer


class GachaDeliveryFailure(RuntimeError):
    """Represent whether delivery can safely be retried or needs reconciliation."""

    def __init__(self, *, retryable: bool, ambiguous: bool = False) -> None:
        super().__init__("student_gacha_discord_delivery_failed")
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True, repr=False, slots=True)
class DiscordStudentGachaOperation:
    """An authenticated Arona component or modal with its original message binding."""

    interaction_id: str
    application_id: str
    guild_id: str
    channel_id: str
    channel_type: int | None
    requester_id: str
    offer_id: str
    message_id: str
    message_author_id: str
    received_at: datetime
    teacher_name: str | None = field(default=None, repr=False)
    is_modal: bool = False


class GachaInteractionOutcome(StrEnum):
    OPEN_MODAL = "open_modal"
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    WRONG_OWNER = "wrong_owner"
    INVALID_TEACHER_NAME = "invalid_teacher_name"


@dataclass(frozen=True, slots=True)
class GachaInteractionResult:
    outcome: GachaInteractionOutcome
    warned_users: tuple[str, str] | None = field(default=None, repr=False)


class _GachaInteractionStore(Protocol):
    async def get_offer(self, offer_id: str) -> GachaOffer | None: ...

    async def claim_warning(
        self, *, offer_id: str, guild_id: str, channel_id: str, message_id: str, at: datetime
    ) -> bool: ...

    async def submit(
        self,
        *,
        offer_id: str,
        guild_id: str,
        channel_id: str,
        requester_id: str,
        message_id: str,
        interaction_id: str,
        teacher_name: str,
        at: datetime,
    ) -> bool: ...


class StudentGachaInteractions:
    def __init__(
        self,
        *,
        store: _GachaInteractionStore,
        config: DiscordRuntimeConfig,
    ) -> None:
        self._store = store
        self._config = config
        self._arona_application_id = config.application_id_for(DiscordBotSlot.PARTICIPANT_A)

    async def accept(self, operation: DiscordStudentGachaOperation) -> GachaInteractionResult:
        if (
            operation.application_id != self._arona_application_id
            or operation.message_author_id != self._arona_application_id
            or operation.channel_type != GUILD_TEXT_CHANNEL
            or not self._config.allows(guild_id=operation.guild_id, channel_id=operation.channel_id)
        ):
            return GachaInteractionResult(GachaInteractionOutcome.UNAVAILABLE)
        if operation.is_modal:
            return GachaInteractionResult(await self._submit(operation))

        offer = await self._store.get_offer(operation.offer_id)
        if (
            offer is None
            or not offer.live
            or offer.guild_id != operation.guild_id
            or offer.channel_id != operation.channel_id
            or offer.message_id != operation.message_id
        ):
            return GachaInteractionResult(GachaInteractionOutcome.UNAVAILABLE)
        if offer.requester_id == operation.requester_id:
            return GachaInteractionResult(GachaInteractionOutcome.OPEN_MODAL)
        if (
            _is_discord_user_id(offer.requester_id)
            and _is_discord_user_id(operation.requester_id)
            and await self._store.claim_warning(
                offer_id=operation.offer_id,
                guild_id=operation.guild_id,
                channel_id=operation.channel_id,
                message_id=operation.message_id,
                at=operation.received_at,
            )
        ):
            return GachaInteractionResult(
                GachaInteractionOutcome.WRONG_OWNER,
                warned_users=(offer.requester_id, operation.requester_id),
            )
        return GachaInteractionResult(GachaInteractionOutcome.WRONG_OWNER)

    async def _submit(self, operation: DiscordStudentGachaOperation) -> GachaInteractionOutcome:
        if operation.teacher_name is None:
            return GachaInteractionOutcome.INVALID_TEACHER_NAME
        accepted = await self._store.submit(
            offer_id=operation.offer_id,
            guild_id=operation.guild_id,
            channel_id=operation.channel_id,
            requester_id=operation.requester_id,
            message_id=operation.message_id,
            interaction_id=operation.interaction_id,
            teacher_name=operation.teacher_name,
            at=operation.received_at,
        )
        return GachaInteractionOutcome.ACCEPTED if accepted else GachaInteractionOutcome.UNAVAILABLE


def _is_discord_user_id(value: str) -> bool:
    return re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None and int(value) < 2**64
