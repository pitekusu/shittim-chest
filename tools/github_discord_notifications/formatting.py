# SPDX-License-Identifier: MIT
"""Pure Discord formatting and truncation rules."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from tools.github_discord_notifications.models import (
    ConclusionPresentation,
    DiscordEmbed,
    DiscordField,
    JsonObject,
    JsonValue,
)

GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22
YELLOW = 0xF1C40F
BLUE = 0x3498DB
PURPLE = 0x9B59B6
GRAY = 0x95A5A6

TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
FIELD_COUNT_LIMIT = 25
EMBED_TOTAL_LIMIT = 6000

_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")
_CONTROL_REPLACEMENT = "�"


def sanitize_text(value: object) -> str:
    """Remove display-control spoofing and escape Discord Markdown."""

    text = str(value) if value is not None else "—"
    cleaned = "".join(
        _CONTROL_REPLACEMENT if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in text
    )
    return _MARKDOWN.sub(r"\\\1", cleaned).strip() or "—"


def truncate(value: str, limit: int) -> str:
    """Truncate at a Unicode code-point boundary and mark truncation."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if len(value) <= limit:
        return value
    if limit == 1:
        return "…"
    return value[: limit - 1].rstrip() + "…"


def conclusion_presentation(conclusion: str | None) -> ConclusionPresentation:
    """Map a GitHub workflow conclusion to a stable presentation."""

    presentations = {
        "success": ConclusionPresentation("✅", GREEN, "処置不要です。", False),
        "failure": ConclusionPresentation(
            "❌", RED, "失敗ジョブとGitHub Actionsログを確認してください。", True
        ),
        "cancelled": ConclusionPresentation(
            "⚠️", YELLOW, "キャンセル理由と後続処理への影響を確認してください。", False
        ),
        "timed_out": ConclusionPresentation(
            "⏱️", RED, "タイムアウトしたジョブと処理時間を確認してください。", True
        ),
        "action_required": ConclusionPresentation(
            "🚨", ORANGE, "GitHub上の承認待ちまたは設定不足を確認してください。", True
        ),
    }
    return presentations.get(
        conclusion or "",
        ConclusionPresentation("ℹ️", BLUE, "GitHubで実行状態を確認してください。", False),  # noqa: RUF001
    )


def _field(field: DiscordField) -> DiscordField:
    return DiscordField(
        name=truncate(sanitize_text(field.name), FIELD_NAME_LIMIT),
        value=truncate(sanitize_text(field.value), FIELD_VALUE_LIMIT),
        inline=field.inline,
    )


def normalize_embed(embed: DiscordEmbed) -> DiscordEmbed:
    """Enforce every Discord per-field and aggregate embed limit."""

    title = truncate(sanitize_text(embed.title), TITLE_LIMIT)
    description = truncate(sanitize_text(embed.description), DESCRIPTION_LIMIT)
    footer = truncate(sanitize_text(embed.footer), FOOTER_LIMIT)
    fields = [_field(field) for field in embed.fields[:FIELD_COUNT_LIMIT]]

    def total() -> int:
        return (
            len(title)
            + len(description)
            + len(footer)
            + sum(len(field.name) + len(field.value) for field in fields)
        )

    while total() > EMBED_TOTAL_LIMIT and fields:
        excess = total() - EMBED_TOTAL_LIMIT
        last = fields[-1]
        target = max(1, len(last.value) - excess)
        shortened = truncate(last.value, target)
        if shortened == last.value:
            fields.pop()
        else:
            fields[-1] = DiscordField(last.name, shortened, last.inline)
    if total() > EMBED_TOTAL_LIMIT:
        description = truncate(
            description,
            max(1, len(description) - (total() - EMBED_TOTAL_LIMIT)),
        )
    if total() > EMBED_TOTAL_LIMIT:
        footer = truncate(footer, max(1, len(footer) - (total() - EMBED_TOTAL_LIMIT)))
    if total() > EMBED_TOTAL_LIMIT:
        title = truncate(title, max(1, len(title) - (total() - EMBED_TOTAL_LIMIT)))

    return DiscordEmbed(
        title=title,
        description=description,
        color=embed.color,
        url=embed.url,
        fields=tuple(fields),
        timestamp=embed.timestamp,
        footer=footer,
    )


def embed_payload(embed: DiscordEmbed) -> JsonObject:
    """Convert a validated embed to the Discord wire shape."""

    normalized = normalize_embed(embed)
    fields: list[JsonValue] = [
        {"name": field.name, "value": field.value, "inline": field.inline}
        for field in normalized.fields
    ]
    return {
        "title": normalized.title,
        "description": normalized.description,
        "color": normalized.color,
        "url": normalized.url,
        "fields": fields,
        "timestamp": normalized.timestamp,
        "footer": {"text": normalized.footer},
    }


def message_payload(embed: DiscordEmbed, *, alert_role_id: str | None = None) -> JsonObject:
    """Build a mention-safe Discord webhook message."""

    payload: JsonObject = {
        "username": "Shittim Chest GitHub",
        "embeds": [embed_payload(embed)],
        "allowed_mentions": {"parse": []},
    }
    if alert_role_id is not None:
        validate_snowflake(alert_role_id, label="alert role ID")
        payload["content"] = f"<@&{alert_role_id}>"
        payload["allowed_mentions"] = {"parse": [], "roles": [alert_role_id]}
    return payload


def validate_snowflake(value: str, *, label: str) -> None:
    """Require a non-empty decimal Discord identifier without logging its value."""

    if not value or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{label} must contain decimal digits only")


def join_lines(values: Iterable[object], *, empty: str = "—") -> str:
    """Join sanitized display values while retaining an explicit empty state."""

    rendered = [sanitize_text(value) for value in values]
    return "\n".join(rendered) if rendered else empty
