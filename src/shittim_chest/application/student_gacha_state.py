"""Durable recruitment offers and work shared by the Discord and Core adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shittim_chest.application.student_gacha_models import (
        GachaPresentation,
        StoredGachaImage,
        Student,
    )
    from shittim_chest.domain import ParticipantSlot


class GachaJobKind(StrEnum):
    PROGRESS = "progress"
    OFFER = "offer"
    DRAW = "draw"


@dataclass(frozen=True, slots=True)
class GachaOffer:
    offer_id: str
    guild_id: str
    channel_id: str
    message_id: str | None
    requester_id: str = field(repr=False)
    live: bool = True


@dataclass(frozen=True, slots=True)
class GachaJob:
    job_id: str
    kind: GachaJobKind
    guild_id: str
    channel_id: str
    after_message_id: str
    created_at: datetime
    next_available_at: datetime
    remaining: int | None = None
    offer_id: str | None = None
    teacher_name: str | None = field(default=None, repr=False)
    student: Student | None = None
    performer: ParticipantSlot | None = None
    presentation: GachaPresentation | None = field(default=None, repr=False)
    paid_started: bool = False
    stored: StoredGachaImage | None = field(default=None, repr=False)
    loading_message_id: str | None = field(default=None, repr=False)
    result_message_id: str | None = field(default=None, repr=False)
    result_after_message_id: str | None = field(default=None, repr=False)
    offer_message_id: str | None = field(default=None, repr=False)
    previous_offer_channel_id: str | None = field(default=None, repr=False)
    previous_offer_message_id: str | None = field(default=None, repr=False)
    offer_superseded: bool = False
    failure_code: str | None = None
    attempts: int = 0
    version: int = 0
    claim_owner: str | None = field(default=None, repr=False)
    claim_expires_at: datetime | None = None
