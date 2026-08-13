"""SDK-independent contracts for one best-effort idle farewell."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shittim_chest.domain import ParticipantSlot

DISCORD_MESSAGE_LIMIT = 2_000
FAREWELL_GENERATION_LEAD = timedelta(minutes=5)
TOKYO_TIMEZONE = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True, slots=True)
class FarewellTimeContext:
    """Public-safe Tokyo calendar values determined outside the model."""

    local_datetime: str
    period: str
    season: str


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


def prepare_farewell_content(message: str, citation_url: str) -> str:
    """Render one available greeting plus its first provider citation."""

    normalized = " ".join(message.split())
    if not normalized:
        raise ValueError("farewell content must not be empty")
    reference = f"\n参考リンク: {citation_url}"
    available = DISCORD_MESSAGE_LIMIT - len(reference)
    if available < 1:
        raise ValueError("farewell citation leaves no room for content")
    return f"{normalized[:available].rstrip()}{reference}"


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
    "DISCORD_MESSAGE_LIMIT",
    "FAREWELL_GENERATION_LEAD",
    "FarewellTimeContext",
    "farewell_nonce",
    "farewell_time_context",
    "prepare_farewell_content",
)
