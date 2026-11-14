from __future__ import annotations

import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest
from openai import APIStatusError, APITimeoutError
from openai.types.responses import Response
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
from shittim_chest.application.student_gacha_models import (
    TEACHER_PLACEHOLDER,
    GachaGenerationFailure,
    Student,
)
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

_STUDENT = Student("アコ\uff08ドレス\uff09", "ゲヘナ", rarity=4)
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
        greeting=(
            "お待たせしました。",
            f"警備はお任せを、{TEACHER_PLACEHOLDER}。",
            "段取りは完璧です。",
        ),
    )


def _response(
    brief: _AppearanceBrief, *, search: ResponseFunctionWebSearch | None = None
) -> SimpleNamespace:
    message = SimpleNamespace(
        type="message",
        status="completed",
        phase="final_answer",
        content=[SimpleNamespace(type="output_text", text=brief.model_dump_json())],
    )
    return SimpleNamespace(
        status="completed",
        output=[message] if search is None else [search, message],
    )


@pytest.mark.asyncio
async def test_known_appearance_uses_one_text_request_without_search() -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))

    result = await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert result.appearance == "青いドレス"
    assert result.club == "戦略研究会"
    assert result.greeting == (
        "お待たせしました。",
        f"警備はお任せを、{TEACHER_PLACEHOLDER}。",
        "段取りは完璧です。",
    )
    client.responses.create.assert_awaited_once()
    payload = client.responses.create.call_args.kwargs
    assert payload["tools"] == []
    assert payload["tool_choice"] == "none"
    assert not payload["store"]
    assert _PRIVATE_PERSONA not in payload["instructions"]
    assert json.loads(payload["input"])["performer_reference"]["preferences"] == _PRIVATE_PERSONA
    assert json.loads(payload["input"])["student"] == {
        "name": _STUDENT.name,
        "school": _STUDENT.school,
    }


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
    client.responses.create = AsyncMock(
        side_effect=[
            _response(_card(None)),
            _response(_AppearanceBrief(appearance="サファイア色のドレス"), search=search),
        ]
    )

    result = await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert result.appearance == "サファイア色のドレス"
    payload = client.responses.create.call_args.kwargs
    assert json.loads(payload["input"]) == {"name": _STUDENT.name, "school": _STUDENT.school}
    assert payload["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert payload["max_tool_calls"] == 2
    assert _PRIVATE_PERSONA not in repr(payload)
    assert _TEACHER not in repr(payload)


@pytest.mark.asyncio
async def test_exact_verified_outfit_survives_missing_model_appearance_without_web_search() -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card(None)))
    student = Student("キキョウ\uff08水着\uff09", "百鬼夜行")

    result = await _adapter(client).prepare(student=student, performer=_PERFORMER)

    client.responses.create.assert_awaited_once()
    assert result.student == student
    assert "淡い水色の羽織" in result.appearance
    assert "不透明な紺のスポーツ水着" in result.appearance
    assert "ヘイロー" not in result.appearance
    assert "グレーのボブ" not in result.appearance
    payload = client.responses.create.call_args.kwargs
    assert json.loads(payload["input"])["verified_costume"] == result.appearance


@pytest.mark.asyncio
async def test_unconfirmed_appearance_does_not_proceed_to_image(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Mock()
    client.responses.create = AsyncMock(
        side_effect=[_response(_card(None)), _response(_AppearanceBrief(appearance="ドレス"))],
    )

    with pytest.raises(GachaGenerationFailure, match="gacha_appearance_unconfirmed"):
        await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert client.responses.create.await_count == 2
    assert "stage=appearance_search category=source_unconfirmed" in caplog.text
    assert "ドレス" not in caplog.text
    client.with_options.assert_not_called()


@pytest.mark.asyncio
async def test_display_fields_are_flattened_and_teacher_placeholder_keeps_its_position() -> None:
    client = Mock()
    card = _card("青い\nドレス").model_copy(
        update={
            "greeting": ("お待たせしました。", "警備は\nお任せを。", "{teacher} 段取りは完璧です。")
        },
    )
    client.responses.create = AsyncMock(return_value=_response(card))

    result = await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert result.appearance == "青い ドレス"
    assert result.greeting == (
        "お待たせしました。",
        "警備は お任せを。",
        f"{TEACHER_PLACEHOLDER} 段取りは完璧です。",
    )
    client.responses.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_incomplete_response_is_classified_before_parsing_with_locked_sdk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response_text = "raw-provider-incomplete-output"
    client = Mock()
    client.responses.create = AsyncMock(
        return_value=Response.model_construct(
            **{
                "id": "resp_test",
                "object": "response",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "incomplete",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": response_text, "annotations": []}
                        ],
                    }
                ],
            }
        ),
    )

    with pytest.raises(GachaGenerationFailure, match="gacha_presentation_unavailable"):
        await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    client.responses.create.assert_awaited_once()
    payload = client.responses.create.call_args.kwargs
    assert payload["max_output_tokens"] == 1_600
    assert payload["text"]["format"]["strict"] is True
    assert "stage=brief category=incomplete_response detail=max_output_tokens" in caplog.text
    assert response_text not in caplog.text


@pytest.mark.asyncio
async def test_provider_error_diagnostic_logs_only_allowlisted_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Mock()
    client.responses.create = AsyncMock(
        side_effect=APIStatusError(
            "raw provider message",
            response=httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.openai.com/v1/responses")
            ),
            body={"code": "invalid_json_schema", "param": "private-provider-field"},
        ),
    )

    with pytest.raises(GachaGenerationFailure, match="gacha_presentation_unavailable"):
        await _adapter(client).prepare(student=_STUDENT, performer=_PERFORMER)

    assert (
        "stage=brief category=api_status http_status=400 code=invalid_json_schema parameter=other"
        in (caplog.text)
    )
    assert "raw provider message" not in caplog.text
    assert "private-provider-field" not in caplog.text


@pytest.mark.asyncio
async def test_render_uses_approved_image_model_and_inserts_name_only_into_png() -> None:
    picture = io.BytesIO()
    Image.new("RGB", (1792, 832), (190, 183, 214)).save(picture, "PNG")
    image_response = SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(picture.getvalue()).decode("ascii"))]
    )
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))
    edit = AsyncMock(return_value=image_response)
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(student=replace(_STUDENT, rarity=3), performer=_PERFORMER)

    result = await adapter.render(
        presentation, teacher_name=_TEACHER, references=(b"layout", b"portrait")
    )
    payload = edit.call_args.kwargs
    assert payload["model"] == IMAGE_MODEL
    assert payload["image"] == [
        ("performer.webp", b"portrait", "image/webp"),
        ("format.jpg", b"layout", "image/jpeg"),
    ]
    assert "live-action photography" in payload["prompt"]
    assert "Backdrop palette: soft pink, lavender and ice-blue" in payload["prompt"]
    assert "the supplied layout's gentle light" in payload["prompt"]
    assert _TEACHER not in repr(payload)
    client.with_options.assert_called_once_with(max_retries=0)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (1792, 828)
        # Backdrop effects leave the center of the generated artwork intact.
        assert image.getpixel((770, 400)) == (190, 183, 214)

    other = await adapter.render(
        replace(presentation, student=_STUDENT, performer=ParticipantSlot.PARTICIPANT_B),
        teacher_name="別の先生",
        references=(b"layout", b"portrait"),
    )
    prompt = edit.call_args.kwargs["prompt"]
    assert "crisp anime illustration" in prompt
    assert "Plana from image 1" in prompt
    assert "halo, and head accessories exactly as shown" in prompt
    assert "iridescent rainbow hues, bright prismatic light" in prompt
    assert json.loads(prompt.split("Costume JSON: ", 1)[1]) == {"clothing": "青いドレス"}
    assert _STUDENT.name not in prompt
    assert other != result


@pytest.mark.asyncio
async def test_image_timeout_is_ambiguous_and_is_not_retried_by_adapter() -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))
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
