from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from openai import APITimeoutError
from openai.types.responses.response_function_web_search import (
    ActionSearch,
    ResponseFunctionWebSearch,
)
from PIL import Image

from shittim_chest.adapters.openai.config import ParticipantProfile, ParticipantProfiles
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
from shittim_chest.adapters.openai.student_gacha import (
    IMAGE_MODEL,
    OpenAIStudentGacha,
    _AppearanceBrief,
    _CardBrief,
)
from shittim_chest.application.student_gacha_models import GachaGenerationFailure, Student
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

_STUDENT = Student("アコ\uff08ドレス\uff09", "ゲヘナ")
_TEACHER = "サンプル先生"
_PRIVATE_PERSONA = "private-persona-for-test"
_PERFORMER = ParticipantSlot.PARTICIPANT_C


def _adapter(client: Mock) -> OpenAIStudentGacha:
    return OpenAIStudentGacha(
        client,
        OpenAIRequestLimiter(1),
        ParticipantProfiles(
            {
                participant: ParticipantProfile(participant.value, _PRIVATE_PERSONA)
                for participant in PARTICIPANTS
            }
        ),
    )


def _card(appearance: str | None) -> _CardBrief:
    return _CardBrief(
        appearance=appearance,
        club="戦略研究会",
        role="SPECIAL・サポーター",
        weapon="HG",
        greeting=("お待たせしました、{teacher}。", "警備はお任せを。", "段取りは完璧です。"),
    )


@pytest.mark.asyncio
async def test_known_appearance_uses_one_text_request_without_search() -> None:
    client = Mock()
    client.responses.parse = AsyncMock(
        return_value=SimpleNamespace(status="completed", output_parsed=_card("青いドレス"))
    )

    result = await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert result.appearance == "青いドレス"
    assert result.club == "戦略研究会"
    client.responses.parse.assert_awaited_once()
    payload = client.responses.parse.call_args.kwargs
    assert "tools" not in payload
    assert not payload["store"]
    assert _PRIVATE_PERSONA in payload["instructions"]


@pytest.mark.asyncio
async def test_uncertain_appearance_uses_only_the_student_identity_for_web_search() -> None:
    search = ResponseFunctionWebSearch(
        id="test-search",
        type="web_search_call",
        status="completed",
        action=ActionSearch(
            type="search",
            sources=[{"type": "url", "url": "https://example.com/official"}],
        ),
    )
    client = Mock()
    client.responses.parse = AsyncMock(
        side_effect=[
            SimpleNamespace(status="completed", output_parsed=_card(None)),
            SimpleNamespace(
                status="completed",
                output_parsed=_AppearanceBrief(appearance="サファイア色のドレス"),
                output=[search],
            ),
        ]
    )

    result = await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert result.appearance == "サファイア色のドレス"
    payload = client.responses.parse.call_args.kwargs
    assert json.loads(payload["input"]) == {"name": _STUDENT.name, "school": _STUDENT.school}
    assert payload["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert payload["max_tool_calls"] == 2
    assert _PRIVATE_PERSONA not in repr(payload)
    assert _TEACHER not in repr(payload)


@pytest.mark.asyncio
async def test_render_uses_approved_image_model_and_inserts_name_only_into_png() -> None:
    picture = io.BytesIO()
    Image.new("RGB", (1792, 832), (190, 183, 214)).save(picture, "PNG")
    image_response = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(picture.getvalue()).decode("ascii"))]
    )
    client = Mock()
    client.responses.parse = AsyncMock(
        return_value=SimpleNamespace(status="completed", output_parsed=_card("青いドレス"))
    )
    edit = AsyncMock(return_value=image_response)
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(student=_STUDENT, performer=_PERFORMER)

    result = await adapter.render(
        presentation, teacher_name=_TEACHER, references=(b"layout", b"portrait")
    )
    payload = edit.call_args.kwargs
    assert payload["model"] == IMAGE_MODEL
    assert payload["image"] == [
        ("format.jpg", b"layout", "image/jpeg"),
        ("performer.webp", b"portrait", "image/webp"),
    ]
    assert "live-action photography" in payload["prompt"]
    assert _TEACHER not in repr(payload)
    client.with_options.assert_called_once_with(max_retries=0)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (1792, 828)
        # The artwork survives intact at the corner where the prototype had a label.
        assert image.getpixel((1720, 802)) == (190, 183, 214)

    other = await adapter.render(
        replace(presentation, performer=ParticipantSlot.PARTICIPANT_A),
        teacher_name="別の先生",
        references=(b"layout", b"portrait"),
    )
    assert "crisp anime illustration" in edit.call_args.kwargs["prompt"]
    assert other != result


@pytest.mark.asyncio
async def test_image_timeout_is_ambiguous_and_is_not_retried_by_adapter() -> None:
    client = Mock()
    client.responses.parse = AsyncMock(
        return_value=SimpleNamespace(status="completed", output_parsed=_card("青いドレス"))
    )
    edit = AsyncMock(
        side_effect=APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com"))
    )
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(student=_STUDENT, performer=_PERFORMER)

    with pytest.raises(GachaGenerationFailure, match="gacha_image_response_unknown") as failure:
        await adapter.render(
            presentation, teacher_name=_TEACHER, references=(b"layout", b"portrait")
        )

    assert failure.value.ambiguous
    edit.assert_awaited_once()
