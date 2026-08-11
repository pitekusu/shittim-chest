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


def test_content_is_one_line_bounded_and_discord_sanitized() -> None:
    raw = (
        "東京の夏らしい晴れ空でしたね!今日の宇宙ニュースにもわくわくしました。"
        "みんなと話せて最高です、それでは楽しい夜を。また元気に会いましょう!"
    )

    content = prepare_farewell_content(raw)

    assert "\n" not in content
    assert "https://" not in content
    assert "AI生成" not in content
    assert "\\!" in content


@pytest.mark.parametrize(
    "value",
    [
        "短すぎます。",
        "a" * 161,
        "a" * 59 + "\n続き",
        "a" * 60 + " https://example.test",
        "a" * 60 + " weather.example.com/tokyo",
        "a" * 60 + " この出力はAI生成です",
        "a" * 60 + "\u200d",
    ],
)
def test_content_rejects_unsafe_or_out_of_contract_values(value: str) -> None:
    with pytest.raises(ValueError):
        prepare_farewell_content(value)


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
