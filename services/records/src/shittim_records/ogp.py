"""Public preview value object; never carries answers, votes or private identities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import regex

RECORD_ID = r"[A-Za-z0-9_-]{43}"
IMAGE_VERSION = r"[a-f0-9]{32}"
PREVIEW_PREPARATION_TIMEOUT_SECONDS = 20
TEMPLATE_VERSION = "record-preview-v2"


def preview_text(text: str, limit: int) -> str:
    """Keep graphemes intact, collapse whitespace and omit control characters."""
    text = regex.sub(r"[\p{Cc}\p{Cs}\p{Bidi_Control}]", " ", text)
    parts = regex.findall(r"\X", " ".join(text.split()))
    return "".join(parts if len(parts) <= limit else [*parts[: limit - 1], "…"])


@dataclass(frozen=True, slots=True)
class RecordPreview:
    record_id: str
    question: str
    requester_name: str
    completed_at: datetime
    avatar_key: str | None = None
    profile_version: str | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(RECORD_ID, self.record_id) is None:
            raise ValueError("invalid preview record")
        if self.completed_at.tzinfo is None or not self.question or not self.requester_name:
            raise ValueError("invalid preview metadata")
        if (
            self.avatar_key is not None
            and re.fullmatch(r"requesters/[A-Za-z0-9_/-]+\.webp", self.avatar_key) is None
        ):
            raise ValueError("invalid preview avatar")
        object.__setattr__(self, "question", preview_text(self.question, 160))
        object.__setattr__(self, "requester_name", preview_text(self.requester_name, 80))

    @property
    def date_label(self) -> str:
        value = self.completed_at.astimezone(ZoneInfo("Asia/Tokyo"))
        return f"{value.year}年{value.month}月{value.day}日"

    @property
    def version(self) -> str:
        content = [
            TEMPLATE_VERSION,
            self.record_id,
            self.question,
            self.requester_name,
            self.date_label,
            self.avatar_key,
            self.profile_version,
        ]
        return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()[:32]

    @property
    def image_path(self) -> str:
        return f"/og/records/{self.record_id}/{self.version}.png"

    @property
    def cache_key(self) -> str:
        return f"ogp/records/{self.record_id}/{self.version}.png"
