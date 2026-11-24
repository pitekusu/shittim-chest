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
from PIL import Image, ImageDraw

from shittim_chest.adapters.openai.config import ParticipantProfile, ParticipantProfiles
from shittim_chest.adapters.openai.limiter import OpenAIRequestLimiter
from shittim_chest.adapters.openai.student_gacha import OpenAIStudentGacha, _CardBrief
from shittim_chest.application.student_gacha_models import (
    GACHA_APNG_FRAME_DURATIONS,
    GACHA_GIF_SIZE,
    GACHA_SEXUAL_MODERATION_BLOCKED,
    TEACHER_PLACEHOLDER,
    GachaExpression,
    GachaGenerationFailure,
    GachaPresentation,
    SchoolBranding,
    Student,
)
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot


def _reference_image(image_format: str, color: str = "#303678") -> bytes:
    with io.BytesIO() as output:
        Image.new("RGB", (64, 64), color).save(output, format=image_format, lossless=True)
        return output.getvalue()


_STUDENT = Student("アコ\uff08ドレス\uff09", "ゲヘナ", rarity=4)
_TEACHER = "サンプル先生"
_COSTUME = _reference_image("WEBP")
_COSTUME_WITH_GREEN = _reference_image("WEBP", "#00FF00")
_PORTRAIT_PNG = _reference_image("PNG")
_PORTRAIT_WEBP = _reference_image("WEBP")
_SCHOOL_BRANDING = SchoolBranding("ゲヘナ学園")
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
    assert payload["model"] == "gpt-6.2-sol"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["tools"] == []
    assert payload["tool_choice"] == "none"
    assert not payload["store"]
    assert "instructions" not in payload
    developer, user = payload["input"]
    assert developer["role"] == "developer"
    assert user["role"] == "user"
    assert "挨拶はperformer_referenceの着用者本人" in developer["content"]
    content = user["content"]
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
async def test_render_requests_sunburst_gif_and_composes_the_apng_locally() -> None:
    picture = io.BytesIO()
    frames = []
    for index in range(12):
        frame = Image.new("RGB", (1024, 480), "#00FF00")
        ImageDraw.Draw(frame).rectangle((360, 42, 570, 479), fill=(75, 90, 214 - 6 * index))
        frames.append(frame)
    frames[0].save(picture, "GIF", save_all=True, append_images=frames[1:], duration=130)
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
        references=(b"layout", _PORTRAIT_WEBP),
        costume_reference=_COSTUME,
        school_branding=_SCHOOL_BRANDING,
    )
    edit.assert_awaited_once()
    payload = edit.call_args.kwargs
    assert payload["model"] == "gpt-image-2.6-sunburst"
    assert payload["size"] == "1024x480"
    assert payload["quality"] == "max"
    assert payload["output_format"] == "gif"
    assert payload["frame_count"] == 12
    assert payload["frame_duration_ms"] == 130
    assert payload["n"] == 1
    assert payload["image"] == [
        ("performer.webp", _PORTRAIT_WEBP, "image/webp"),
        ("format.jpg", b"layout", "image/jpeg"),
        ("costume.webp", _COSTUME, "image/webp"),
    ]
    assert "Image 3 is the exact outfit variant" in payload["prompt"]
    assert "live-action photography" in payload["prompt"]
    assert "exact solid chroma-key color #00FF00" in payload["prompt"]
    assert "Keep the same background color in every frame" in payload["prompt"]
    assert "draw shadows, gradients, reflections, scenery, lights or particles" in payload["prompt"]
    assert "seamless looping animation" in payload["prompt"]
    assert "Keep the camera and framing fixed" in payload["prompt"]
    assert "visible from the first frame through the last" in payload["prompt"]
    assert "natural movement by the performer" in payload["prompt"]
    assert "back toward the starting body position" in payload["prompt"]
    assert "last frame should flow into the first like any two adjacent frames" in payload["prompt"]
    assert "Avoid duplicating the first frame at the end" in payload["prompt"]
    assert "No entrances, exits or fades" in payload["prompt"]
    assert "frame separators or borders" in payload["prompt"]
    assert "Keep the specified facial expression." in payload["prompt"]
    assert "Give the live-action cosplay a cute, natural motion" in payload["prompt"]
    assert _TEACHER not in repr(payload)
    client.with_options.assert_called_once_with(max_retries=0)
    with Image.open(io.BytesIO(result)) as image:
        assert (image.format, image.size, getattr(image, "n_frames", 1)) == (
            "PNG",
            GACHA_GIF_SIZE,
            12,
        )
        assert image.info["loop"] == 0
        for index, delay in enumerate(GACHA_APNG_FRAME_DURATIONS):
            image.seek(index)
            assert image.info["duration"] == delay

    other = await adapter.render(
        replace(presentation, student=_STUDENT, performer=ParticipantSlot.PARTICIPANT_B),
        teacher_name="別の先生",
        references=(b"layout", _PORTRAIT_WEBP),
        costume_reference=_COSTUME,
        school_branding=_SCHOOL_BRANDING,
    )
    prompt = edit.call_args.kwargs["prompt"]
    assert "crisp anime illustration" in prompt
    assert "Live2D-like gentle blink, breathing and subtle motion" in prompt
    assert "Plana from image 1" in prompt
    assert "hair, eye shape and color, and native halo as shown there" in prompt
    assert "exact solid chroma-key color #00FF00" in prompt
    assert _STUDENT.name not in prompt
    assert other != result


@pytest.mark.asyncio
@pytest.mark.parametrize("performer", PARTICIPANTS)
@pytest.mark.parametrize(
    ("expression", "expression_prompt"),
    [
        (None, "Preserve the performer's facial expression from image 1."),
        (GachaExpression.UNHAPPY, "a visibly displeased expression"),
        (GachaExpression.NEUTRAL, "a calm, neutral expression"),
        (GachaExpression.SMILE, "a natural, clearly visible smile"),
    ],
)
async def test_render_uses_costume_image_and_selected_performer_expression(
    monkeypatch: pytest.MonkeyPatch,
    performer: ParticipantSlot,
    expression: GachaExpression | None,
    expression_prompt: str,
) -> None:
    client = Mock()
    edit = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="YXJ0")]))
    client.with_options.return_value.images.edit = edit
    render_card = Mock(return_value=b"card")
    pose_choice = Mock(return_value="Use a composed starting pose for this example.")
    monkeypatch.setattr("shittim_chest.adapters.openai.student_gacha.render_card", render_card)
    monkeypatch.setattr("shittim_chest.adapters.openai.student_gacha.choice", pose_choice)
    is_arona = performer is ParticipantSlot.PARTICIPANT_A
    costume_reference = _COSTUME_WITH_GREEN if is_arona else _COSTUME
    portrait = _PORTRAIT_PNG if is_arona else _PORTRAIT_WEBP
    key_color = "#FF00FF" if is_arona else "#00FF00"
    presentation = GachaPresentation(
        replace(_STUDENT, rarity=1),
        performer,
        "以前の説明",
        "戦略研究会",
        "サポーター",
        "HG",
        ("お待たせしました。", f"{TEACHER_PLACEHOLDER}。", "よろしくお願いします。"),
    )

    result = await _adapter(client).render(
        presentation,
        teacher_name=_TEACHER,
        references=(b"layout", portrait),
        costume_reference=costume_reference,
        school_branding=_SCHOOL_BRANDING,
        expression=expression,
    )

    edit.assert_awaited_once()
    payload = edit.call_args.kwargs
    assert payload["extra_body"] == {"moderation": "low"}
    portrait_type = "png" if is_arona else "webp"
    assert payload["image"] == [
        (f"performer.{portrait_type}", portrait, f"image/{portrait_type}"),
        ("format.jpg", b"layout", "image/jpeg"),
        ("costume.webp", costume_reference, "image/webp"),
    ]
    prompt = payload["prompt"]
    assert "Image 3 is the exact outfit variant" in prompt
    assert "the sole wardrobe reference" in prompt
    assert "Dress\nthe character in the exact costume design shown in image 3" in prompt
    assert "Keep the costume from image 3 visible in every frame" in prompt
    assert "For every performer, put any clearly visible wearable costume headwear" in prompt
    assert "from image 3 on their\nhead" in prompt
    assert "hat, cap, beret, hood worn up, or a headband with costume ears" in prompt
    assert "A hood worn down stays down" in prompt
    assert "Never copy anatomical ears or invent headwear" in prompt
    assert "facial features, gaze direction, eye shape and color" in prompt
    assert "and hair style from image 1." in prompt
    assert "Use a three-quarter length portrait, from the head down to the knees." in prompt
    assert "about 18% of the canvas width" in prompt
    assert "beyond the top edge" in prompt
    assert "at about x=42%" in prompt
    assert f"exact solid chroma-key color {key_color}" in prompt
    assert "Keep the same background color in every frame" in prompt
    assert "Backdrop palette:" not in prompt
    assert "Ignore its background\nand lighting" in prompt
    assert "Do not reproduce its person, face,\nhair, halo, headwear, accessories" in prompt
    original_head = "Keep the performer's hair color and style and native halo from image 1"
    cosplay_head = "Use image 3 as the only source for the cosplay wig, halo and head"
    if performer is ParticipantSlot.PARTICIPANT_C:
        assert cosplay_head in prompt
        assert "wig matching the hairstyle and hair color visible in image 3" in prompt
        assert "If image 3 has no halo, do not add one" in prompt
        assert "Do not borrow a halo or head accessories from image 1 or image 2" in prompt
        assert original_head not in prompt
        pose_choice.assert_called_once()
        assert len(pose_choice.call_args.args[0]) > 1
        assert pose_choice.return_value in prompt
    else:
        assert original_head in prompt
        assert "Fit the costume headwear over that hair" in prompt
        assert "Do not copy hair, a halo or other hair ornaments from image 3" in prompt
        assert "If image 1 has no\nhalo, keep it absent" in prompt
        assert cosplay_head not in prompt
        pose_choice.assert_not_called()
        assert pose_choice.return_value not in prompt
    assert expression_prompt in prompt
    if expression is not None:
        assert "Preserve the performer's facial expression from image 1." not in prompt
    assert "以前の説明" not in prompt
    assert _TEACHER not in repr(payload)
    assert _SCHOOL_BRANDING.name not in repr(payload)
    render_card.assert_called_once_with(
        b"art",
        presentation,
        _TEACHER,
        school_branding=_SCHOOL_BRANDING,
        key_color=key_color,
    )
    assert result == b"card"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("student", "guidance"),
    [
        (Student("シフォン", "MNC", 4), "floppy fabric dog ears"),
        (Student("Grus", "MNC", 4), "white long-sleeve shirt, blue jeans and jewelry"),
        (Student("シフォン", "別の学校", 4), None),
    ],
)
async def test_render_applies_mnc_costume_details_to_the_matching_student(
    monkeypatch: pytest.MonkeyPatch, student: Student, guidance: str | None
) -> None:
    client = Mock()
    edit = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="YXJ0")]))
    client.with_options.return_value.images.edit = edit
    monkeypatch.setattr("shittim_chest.adapters.openai.student_gacha.render_card", Mock())
    presentation = GachaPresentation(
        student,
        ParticipantSlot.PARTICIPANT_A,
        "衣装の説明",
        "衣装研究会",
        "サポーター",
        "なし",
        (f"{TEACHER_PLACEHOLDER}、こんにちは。", "衣装を着ました。", "よろしくお願いします。"),
    )

    await _adapter(client).render(
        presentation,
        teacher_name=_TEACHER,
        references=(b"layout", _PORTRAIT_PNG),
        costume_reference=_COSTUME,
        school_branding=SchoolBranding(student.school),
    )

    prompt = edit.call_args.kwargs["prompt"]
    assert "Arona from image 1" in prompt
    assert "light blue bob and floating blue halo" in prompt
    if guidance is not None:
        assert guidance in prompt
    else:
        assert "floppy fabric dog ears" not in prompt


@pytest.mark.asyncio
async def test_invalid_chroma_reference_fails_before_paid_image_request() -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))
    edit = AsyncMock()
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(
        student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
    )

    with pytest.raises(GachaGenerationFailure, match="gacha_chroma_reference_invalid"):
        await adapter.render(
            presentation,
            teacher_name=_TEACHER,
            references=(b"layout", b"invalid portrait"),
            costume_reference=_COSTUME,
            school_branding=_SCHOOL_BRANDING,
        )

    edit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "categories", "expected_code", "expected_diagnostics"),
    [
        (
            "output",
            ["sexual", "private-provider-category"],
            GACHA_SEXUAL_MODERATION_BLOCKED,
            "moderation_stage=output moderation_categories=sexual",
        ),
        (
            "input",
            ["sexual"],
            GACHA_SEXUAL_MODERATION_BLOCKED,
            "moderation_stage=input moderation_categories=sexual",
        ),
        (
            "output",
            ["violence", "private-provider-category"],
            "gacha_image_moderation_blocked",
            "moderation_stage=output moderation_categories=violence",
        ),
        (
            "private-provider-stage",
            "sexual",
            "gacha_image_moderation_blocked",
            "moderation_stage=unknown moderation_categories=unknown",
        ),
    ],
)
async def test_image_moderation_only_classifies_explicit_sexual_category(
    caplog: pytest.LogCaptureFixture,
    stage: str,
    categories: list[str] | str,
    expected_code: str,
    expected_diagnostics: str,
) -> None:
    client = Mock()
    client.responses.create = AsyncMock(return_value=_response(_card("青いドレス")))
    edit = AsyncMock(
        side_effect=APIStatusError(
            "private-provider-message",
            response=httpx2.Response(
                400, request=httpx2.Request("POST", "https://api.openai.com/v1/images/edits")
            ),
            body={
                "code": "moderation_blocked",
                "moderation_details": {"moderation_stage": stage, "categories": categories},
            },
        )
    )
    client.with_options.return_value.images.edit = edit
    adapter = _adapter(client)
    presentation = await adapter.prepare(
        student=_STUDENT, performer=_PERFORMER, costume_reference=_COSTUME
    )

    with pytest.raises(GachaGenerationFailure) as failure:
        await adapter.render(
            presentation,
            teacher_name=_TEACHER,
            references=(b"layout", _PORTRAIT_WEBP),
            costume_reference=_COSTUME,
            school_branding=_SCHOOL_BRANDING,
        )

    assert failure.value.code == expected_code
    assert not failure.value.ambiguous
    assert expected_diagnostics in caplog.text
    assert "private-provider" not in caplog.text
    edit.assert_awaited_once()


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
            references=(b"layout", _PORTRAIT_WEBP),
            costume_reference=_COSTUME,
            school_branding=_SCHOOL_BRANDING,
        )

    assert failure.value.ambiguous
    edit.assert_awaited_once()
