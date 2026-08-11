"""SDK-independent contracts for one best-effort idle farewell."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shittim_chest.application.discord import sanitize_discord_model_text
from shittim_chest.domain import ParticipantSlot

FAREWELL_MIN_CHARACTERS = 60
FAREWELL_MAX_CHARACTERS = 160
FAREWELL_GENERATION_LEAD = timedelta(minutes=2)
TOKYO_TIMEZONE = ZoneInfo("Asia/Tokyo")

_URL_PATTERN = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_DISCLAIMER_FRAGMENTS = (
    "AI生成",
    "正確性や専門的判断",
    "専門的判断を保証",
)


@dataclass(frozen=True, slots=True)
class FarewellTimeContext:
    """Public-safe Tokyo calendar values determined outside the model."""

    local_datetime: str
    period: str
    season: str


@dataclass(frozen=True, slots=True)
class FarewellCandidate:
    """One process-memory-only farewell bound to an IDLE generation."""

    generation: int
    stop_eligible_at: datetime
    participant: ParticipantSlot
    content: str = field(repr=False)
    nonce: str

    def __post_init__(self) -> None:
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("farewell generation must be positive")
        _require_utc(self.stop_eligible_at)
        if re.fullmatch(r"[A-Za-z0-9_-]{22}", self.nonce) is None:
            raise ValueError("farewell nonce must be 22 base64url characters")
        if not self.content.strip():
            raise ValueError("farewell content must not be empty")


def farewell_time_context(now: datetime) -> FarewellTimeContext:
    """Derive the Japanese time period and meteorological season in Tokyo."""

    _require_utc(now)
    local = now.astimezone(TOKYO_TIMEZONE)
    if 5 <= local.hour < 11:
        period = "朝"
    elif 11 <= local.hour < 17:
        period = "昼"
    elif local.hour >= 17:
        period = "夜"
    else:
        period = "深夜"
    if local.month in {3, 4, 5}:
        season = "春"
    elif local.month in {6, 7, 8}:
        season = "夏"
    elif local.month in {9, 10, 11}:
        season = "秋"
    else:
        season = "冬"
    return FarewellTimeContext(
        local_datetime=local.isoformat(timespec="minutes"),
        period=period,
        season=season,
    )


def prepare_farewell_content(value: str) -> str:
    """Validate the short one-line contract and escape Discord formatting."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\n" in normalized:
        raise ValueError("farewell content must be one line")
    if not FAREWELL_MIN_CHARACTERS <= len(normalized) <= FAREWELL_MAX_CHARACTERS:
        raise ValueError("farewell content is outside the allowed length")
    if _URL_PATTERN.search(normalized) is not None:
        raise ValueError("farewell content must not expose source URLs")
    if any(fragment in normalized for fragment in _DISCLAIMER_FRAGMENTS):
        raise ValueError("farewell content must not contain a fixed disclaimer")
    return sanitize_discord_model_text(normalized)


def farewell_nonce(
    *,
    generation: int,
    stop_eligible_at: datetime,
    participant: ParticipantSlot,
) -> str:
    """Derive a replay-stable short nonce from the exact idle operation."""

    if isinstance(generation, bool) or generation < 1:
        raise ValueError("farewell generation must be positive")
    _require_utc(stop_eligible_at)
    identity = (
        f"farewell-v1|{generation}|{stop_eligible_at.isoformat()}|{participant.value}"
    ).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(identity).digest()[:16]).decode().rstrip("=")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("farewell timestamp must be timezone-aware UTC")


__all__ = (
    "FAREWELL_GENERATION_LEAD",
    "FAREWELL_MAX_CHARACTERS",
    "FAREWELL_MIN_CHARACTERS",
    "FarewellCandidate",
    "FarewellTimeContext",
    "farewell_nonce",
    "farewell_time_context",
    "prepare_farewell_content",
)
