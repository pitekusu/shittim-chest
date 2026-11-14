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
from PIL import Image

from shittim_chest.adapters.openai.config import ParticipantProfile, ParticipantProfiles
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
from shittim_chest.adapters.openai.student_gacha import (
    IMAGE_MODEL,
    OpenAIStudentGacha,
    _CardBrief,
)
from shittim_chest.application.student_gacha_models import (
    TEACHER_PLACEHOLDER,
    GachaGenerationFailure,
    GachaPresentation,
    Student,
)
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

_STUDENT = Student("アコ\uff08ドレス\uff09", "ゲヘナ", rarity=4)
_TEACHER = "サンプル先生"
_COSTUME = b"costume-webp"
_PERFORMER = ParticipantSlot.PARTICIPANT_C


def _adapter(client: Mock) -> OpenAIStudentGacha:
    return OpenAIStudentGacha(
        client,
        OpenAIRequestLimiter(1),
        ParticipantProfiles(
            {
                participant: ParticipantProfile(
                    participant.value, f"test-voice-{participant.value}"
                )
                for participant in PARTICIPANTS
            }
        ),
    )


def _card(appearance: str) -> _CardBrief:
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


def _response(brief: _CardBrief) -> SimpleNamespace:
    message = SimpleNamespace(
        type="message",
        status="completed",
        phase="final_answer",
        content=[SimpleNamespace(type="output_text", text=brief.model_dump_json())],
    )
    return SimpleNamespace(
        status="completed",
        output=[message],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("performer", PARTICIPANTS)
async def test_brief_uses_the_selected_wearer_and_costume_image(performer: ParticipantSlot) -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))

    result = await _adapter(client).prepare(
        student=_STUDENT, performer=performer, costume_reference=_COSTUME
    )

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
    assert "挨拶はperformer_referenceの着用者本人" in payload["instructions"]
    content = payload["input"][0]["content"]
    inputs = json.loads(content[0]["text"])
    assert inputs["performer_reference"] == {
        "name": performer.value,
        "preferences": f"test-voice-{performer.value}",
    }
    for other in set(PARTICIPANTS) - {performer}:
        assert f"test-voice-{other.value}" not in repr(payload)
    assert inputs["student"] == {"name": _STUDENT.name, "school": _STUDENT.school}
    assert content[-1] == {
        "type": "input_image",
        "image_url": f"data:image/webp;base64,{base64.b64encode(_COSTUME).decode()}",
        "detail": "high",
    }
    assert _TEACHER not in repr(payload)


@pytest.mark.asyncio
async def test_display_fields_are_flattened_and_teacher_placeholder_keeps_its_position() -> None:
    client = Mock()
    card = _card("青い\nドレス").model_copy(
        update={
            "greeting": ("お待たせしました。", "警備は\nお任せを。", "{teacher} 段取りは完璧です。")
        },
    )
    client.responses.create = AsyncMock(return_value=_response(card))

    result = await _adapter(client).prepare(
        student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
    )

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
        await _adapter(client).prepare(
            student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
        )

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
        await _adapter(client).prepare(
            student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
        )

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
    presentation = await adapter.prepare(
        student=replace(_STUDENT, rarity=3), performer=_PERFORMER, costume_reference=_COSTUME
    )

    result = await adapter.render(
        presentation,
        teacher_name=_TEACHER,
        references=(b"layout", b"portrait"),
        costume_reference=_COSTUME,
    )
    payload = edit.call_args.kwargs
    assert payload["model"] == IMAGE_MODEL
    assert payload["image"] == [
        ("performer.webp", b"portrait", "image/webp"),
        ("format.jpg", b"layout", "image/jpeg"),
        ("costume.webp", _COSTUME, "image/webp"),
    ]
    assert "Image 3 is the exact outfit variant" in payload["prompt"]
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
        costume_reference=_COSTUME,
    )
    prompt = edit.call_args.kwargs["prompt"]
    assert "crisp anime illustration" in prompt
    assert "Plana from image 1" in prompt
    assert "halo, and head accessories exactly as shown" in prompt
    assert "iridescent rainbow hues, bright prismatic light" in prompt
    assert _STUDENT.name not in prompt
    assert other != result


@pytest.mark.asyncio
async def test_render_uses_costume_image_for_a_previously_prepared_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    edit = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="YXJ0")]))
    client.with_options.return_value.images.edit = edit
    render_card = Mock(return_value=b"card")
    monkeypatch.setattr("shittim_chest.adapters.openai.student_gacha.render_card", render_card)
    presentation = GachaPresentation(
        _STUDENT,
        ParticipantSlot.PARTICIPANT_A,
        "以前の説明",
        "戦略研究会",
        "サポーター",
        "HG",
        ("お待たせしました。", f"{TEACHER_PLACEHOLDER}。", "よろしくお願いします。"),
    )

    result = await _adapter(client).render(
        presentation,
        teacher_name=_TEACHER,
        references=(b"layout", b"portrait"),
        costume_reference=_COSTUME,
    )

    edit.assert_awaited_once()
    payload = edit.call_args.kwargs
    assert payload["image"] == [
        ("performer.png", b"portrait", "image/png"),
        ("format.jpg", b"layout", "image/jpeg"),
        ("costume.webp", _COSTUME, "image/webp"),
    ]
    prompt = payload["prompt"]
    assert "Image 3 is the exact outfit variant" in prompt
    assert "head accessories come exclusively from image 1" in prompt
    assert "以前の説明" not in prompt
    assert _TEACHER not in repr(payload)
    render_card.assert_called_once_with(b"art", presentation, _TEACHER)
    assert result == b"card"


@pytest.mark.asyncio
async def test_image_timeout_is_ambiguous_and_is_not_retried_by_adapter() -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))
    edit = AsyncMock(
        side_effect=APITimeoutError(request=httpx2.Request("POST", "https://api.openai.com"))
    )
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(
        student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
    )

    with pytest.raises(GachaGenerationFailure, match="gacha_image_response_unknown") as failure:
        await adapter.render(
            presentation,
            teacher_name=_TEACHER,
            references=(b"layout", b"portrait"),
            costume_reference=_COSTUME,
        )

    assert failure.value.ambiguous
    edit.assert_awaited_once()
