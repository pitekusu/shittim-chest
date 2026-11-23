from __future__ import annotations

import httpx2
import pytest
from openai import APIStatusError

from shittim_chest.adapters.openai.student_gacha import _log_provider_failure


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (
            {
                "moderation_stage": "output",
                "categories": ["sexual", "private-category", "sexual", {"nested": "private"}],
            },
            "moderation_stage=output moderation_categories=sexual",
        ),
        (
            {
                "moderation_stage": "input",
                "categories": ["violence", "harassment"],
            },
            "moderation_stage=input moderation_categories=harassment,violence",
        ),
        (
            {"moderation_stage": "private-stage", "categories": "private-category"},
            "moderation_stage=unknown moderation_categories=unknown",
        ),
        (None, "moderation_stage=unknown moderation_categories=unknown"),
    ],
)
def test_moderation_block_logs_only_public_diagnostics(
    details: object, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    error = APIStatusError(
        "private-provider-message",
        response=httpx2.Response(
            400, request=httpx2.Request("POST", "https://api.openai.com/v1/images/edits")
        ),
        body={"code": "moderation_blocked", "moderation_details": details},
    )

    _log_provider_failure("image", error)

    assert caplog.messages == [
        "student_gacha_provider_failed stage=image category=api_status "
        f"http_status=400 code=moderation_blocked parameter=other {expected}"
    ]
