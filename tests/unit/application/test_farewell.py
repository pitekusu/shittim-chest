"""Pure contract tests for the idle farewell."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shittim_chest.application.farewell import (
    farewell_nonce,
    farewell_time_context,
    prepare_farewell_content,
)
from shittim_chest.domain import ParticipantSlot


@pytest.mark.parametrize(
    ("value", "period", "season"),
    [
        (datetime(2026, 3, 1, 20, 0, tzinfo=UTC), "朝", "春"),
        (datetime(2026, 6, 1, 3, 0, tzinfo=UTC), "昼", "夏"),
        (datetime(2026, 9, 1, 9, 0, tzinfo=UTC), "夜", "秋"),
        (datetime(2026, 12, 1, 17, 0, tzinfo=UTC), "深夜", "冬"),
    ],
)
def test_time_context_uses_tokyo_period_and_season(
    value: datetime,
    period: str,
    season: str,
) -> None:
    context = farewell_time_context(value)

    assert context.period == period
    assert context.season == season
    assert context.local_datetime.endswith("+09:00")


def test_content_collapses_whitespace_and_appends_the_citation() -> None:
    content = prepare_farewell_content(
        "短い挨拶です。\r\n URLや **Markdown** も本文では拒否しません。",
        "https://example.test/source",
    )

    assert content == (
        "短い挨拶です。 URLや **Markdown** も本文では拒否しません。"
        "\n参考リンク: https://example.test/source"
    )


@pytest.mark.parametrize(
    "value",
    [
        "短い挨拶です。",
        "a" * 161,
        "本文にも https://example.test が入っています。",
        "この出力はAI生成です。",
    ],
)
def test_content_accepts_values_previously_rejected_for_display_quality(value: str) -> None:
    content = prepare_farewell_content(value, "https://example.test/source")

    assert content.endswith("\n参考リンク: https://example.test/source")


def test_content_is_truncated_only_to_the_discord_message_limit() -> None:
    content = prepare_farewell_content("a" * 2_000, "https://example.test/source")

    assert len(content) == 2_000


def test_empty_content_is_rejected() -> None:
    with pytest.raises(ValueError):
        prepare_farewell_content(" \r\n ", "https://example.test/source")


def test_nonce_is_deterministic_short_and_identity_specific() -> None:
    at = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    first = farewell_nonce(
        generation=3,
        stop_eligible_at=at,
        participant=ParticipantSlot.PARTICIPANT_A,
    )

    assert len(first) == 22
    assert first == farewell_nonce(
        generation=3,
        stop_eligible_at=at,
        participant=ParticipantSlot.PARTICIPANT_A,
    )
    assert first != farewell_nonce(
        generation=3,
        stop_eligible_at=at,
        participant=ParticipantSlot.PARTICIPANT_B,
    )
