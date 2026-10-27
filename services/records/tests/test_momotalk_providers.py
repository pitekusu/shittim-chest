"""Provider contracts and paid-image recovery use only fictional local fixtures."""

import base64
import io
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from openai import OpenAI
from PIL import Image
from pydantic import ValidationError
from tests.test_momotalk import WEEK, State, snapshot

from shittim_records.momotalk import (
    PARTICIPANTS,
    ConversationPlan,
    ImageChoice,
    MomotalkFailure,
    SavedImage,
    SavedMessage,
    Turn,
    Utterance,
    WeekDigest,
    image_key,
    image_targets,
)
from shittim_records.momotalk_adapters import MomotalkAssets
from shittim_records.momotalk_generation import collect_week
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator, requester_attitude


@pytest.mark.parametrize(
    "code,param", [("moderation_blocked", "prompt"), ("private-secret", "private-question")]
)
def test_image_failure_logs_only_allowlisted_diagnostics(caplog, code, param):
    value = snapshot()
    state = State(value)
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    room.plan = state.prepare(
        value, value.requesters[0], room, value.requesters[0].questions, final=True
    )

    def reject(_request):
        return httpx2.Response(
            400,
            json={
                "error": {
                    "message": "private provider message and prompt",
                    "code": code,
                    "param": param,
                }
            },
        )

    client = OpenAI(
        api_key="fictional-key",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(reject)),
    )
    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, SimpleNamespace(load_participant_reference=lambda _: b"fixture")),
        client=client,
    )
    try:
        with pytest.raises(MomotalkFailure) as caught:
            generator.selfie(value, value.requesters[0], room.images[0], room.plan.images[0])
    finally:
        generator.close()
    assert "operation=image category=api_status status=400" in caplog.text
    assert f"code={code if code == 'moderation_blocked' else 'unclassified'}" in caplog.text
    assert "private" not in caplog.text
    assert "fictional-key" not in caplog.text
    assert value.requesters[0].questions[0].text not in caplog.text
    assert caught.value.code == (
        "MOMOTALK_IMAGE_MODERATION_BLOCKED"
        if code == "moderation_blocked"
        else "MOMOTALK_GENERATION_FAILED"
    )


@pytest.mark.parametrize("record_id", ["r" * 43, "q" * 43, "x" * 43])
def test_image_reselection_uses_only_other_weekly_discussions(record_id):
    value = snapshot()
    requester = value.requesters[0]
    candidate = requester.questions[0].model_copy(
        update={"record_id": "r" * 43, "text": "雨の日に読む本を選ぶなら?"}
    )
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_parsed=kwargs["text_format"].model_validate(
                {"record_id": record_id, "brief": "本を片手に室内で自撮り"}
            ),
        )

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, None),
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    image = image_targets(requester, WEEK.week_id)[0]
    if record_id == candidate.record_id:
        choice = generator.reselect_image(value, requester, image, [candidate])
        assert choice.record_id == candidate.record_id and choice.mood == image.mood
    else:
        with pytest.raises(MomotalkFailure, match="MOMOTALK_OUTPUT_INVALID"):
            generator.reselect_image(value, requester, image, [candidate])
    assert len(calls) == 1
    request = calls[0]
    assert json.loads(request["input"]) == {"questions": [candidate.model_dump(mode="json")]}
    assert requester.questions[0].text not in request["input"]
    assert request["store"] is False and request["tools"] == []
    assert request["max_output_tokens"] == 1000
    assert all(
        value.personas[subject.participant] in request["instructions"]
        for subject in image.image_subjects
    )


@pytest.mark.parametrize("scores", [(850, 500, 500), (850, 850, 500), (850, 150, 850)])
@pytest.mark.parametrize("legacy", [False, True])
def test_selfie_submits_all_frozen_references_for_one_photo(scores, legacy):
    value = snapshot(scores=scores)
    requester = value.requesters[0]
    image = (
        SavedImage(participant="participant-b", mood="unhappy")
        if legacy
        else image_targets(requester, WEEK.week_id)[0]
    )
    # A resumed room keeps its frozen subjects even if current affection changes.
    requester.scores = dict.fromkeys(PARTICIPANTS, 500)
    calls = []
    loaded = []

    def reference(slot):
        loaded.append(slot)
        return f"fictional-reference-{slot}".encode()

    def edit(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(b"one-fictional-photo").decode())]
        )

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, SimpleNamespace(load_participant_reference=reference)),
        client=SimpleNamespace(images=SimpleNamespace(edit=edit)),
    )
    result = generator.selfie(
        value,
        requester,
        image,
        ImageChoice(
            mood=image.mood, record_id=requester.questions[0].record_id, brief="公園で一緒に撮影"
        ),
    )
    assert result == b"one-fictional-photo" and len(calls) == 1
    request = calls[0]
    slots = [subject.participant for subject in image.image_subjects]
    assert len(slots) == (1 if legacy else sum(score <= 200 or score >= 800 for score in scores))
    assert loaded == slots
    assert request["image"] == [
        (f"{slot}.png", f"fictional-reference-{slot}".encode(), "image/png") for slot in slots
    ]
    assert request["n"] == 1 and request["model"] == "gpt-image-2.5-sunburst"
    assert request["size"] == "1024x1536" and request["quality"] == "high"
    assert f"指定の{len(slots)}人全員が同じ場所で1枚" in request["prompt"]
    assert request["output_format"] == "webp" and request["timeout"] == 240
    for subject in image.image_subjects:
        line = next(
            line for line in request["prompt"].splitlines() if f"{subject.participant}.png" in line
        )
        assert ("嬉しそうな表情" if subject.mood == "happy" else "嫌そうな表情") in line
        assert ("写実的" if subject.participant == "participant-c" else "アニメ調") in line


@pytest.mark.parametrize(
    "score,feeling",
    [
        (199, "大嫌い"),
        (200, "冷めた"),
        (399, "冷めた"),
        (400, "普通の距離感"),
        (599, "普通の距離感"),
        (600, "この質問者が好き"),
        (799, "この質問者が好き"),
        (800, "大好き"),
    ],
)
def test_affection_band_is_resolved_before_generation(score, feeling):
    assert feeling in requester_attitude(score)


@pytest.mark.parametrize(
    "name,address",
    [("架空の利用者", "架空の利用者先生"), ("架空の利用者先生", "架空の利用者先生")],
)
def test_independent_persona_request_contains_history_and_week_facts(name, address):
    state = State(snapshot(scores=(500, 500, 500)))
    state.snapshot.requesters[0].display_name = name
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    room.plan = state.prepare(
        state.snapshot,
        state.snapshot.requesters[0],
        room,
        state.snapshot.requesters[0].questions,
        final=True,
    )
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="completed", output_parsed=Utterance(text="架空の相槌です。"))

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, None),
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    for _ in range(3):
        participant = room.plan.turns[len(room.messages)].participant
        output = generator.utter(state.snapshot, state.snapshot.requesters[0], room)
        room.messages.append(SavedMessage(participant=participant, text=output.text))
    for index, request in enumerate(calls):
        slot = room.plan.turns[index].participant
        payload = json.loads(request["input"])
        assert payload["requester"] == name
        assert payload["requesterAddress"] == address
        assert payload["topic"] == room.plan.turns[index].topic
        assert payload["previousTopic"] == (room.plan.turns[index - 1].topic if index else None)
        assert name not in request["instructions"]
        assert request["model"] == "gpt-6.1-terra"
        assert request["store"] is False and request["tools"] == []
        assert state.snapshot.personas[slot] in request["instructions"]
        assert all(
            prompt not in request["instructions"]
            for other, prompt in state.snapshot.personas.items()
            if other != slot
        )
        assert len(payload["conversation"]) == index
        history = room.messages[:index]
        assert payload["yourPreviousMessages"] == [
            message.text for message in history if message.participant == slot
        ]
        if history:
            assert payload["lastMessage"] == payload["conversation"][-1]
            assert payload["lastMessage"]["text"] == history[-1].text
            assert payload["lastMessage"]["speaker"] in ("アロナ", "プラナ", "安倍晋三AI")
        else:
            assert payload["lastMessage"] is None
        assert payload["weeklyQuestionCount"] == 1
        assert payload["questions"][0]["text"] == state.snapshot.requesters[0].questions[0].text
        assert payload["yourAffection"] == 500
        assert requester_attitude(500) in request["instructions"]


def test_discussion_switch_passes_only_the_current_question():
    value = snapshot(scores=(500, 500, 500))
    requester = value.requesters[0]
    requester.questions = [
        requester.questions[0].model_copy(update={"record_id": str(i) * 43, "text": text})
        for i, text in enumerate(("本を選ぶなら?", "休日にどこへ出かける?", "雨の日は何をする?"))
    ]
    plan = ConversationPlan(
        summary="架空の3つの相談。",
        images=[],
        turns=[
            Turn(
                participant=PARTICIPANTS[slot],
                topic=f"架空の相談{index // 3 + 1}についての本音",
                record_id=requester.questions[index // 3].record_id,
            )
            for index, slot in enumerate((0, 1, 2, 1, 0, 2, 0, 2, 1))
        ],
    )
    state = State(value)
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    requests = []

    def parse(**kwargs):
        requests.append(kwargs)
        output = (
            Utterance(text="架空の本音。")
            if kwargs["text_format"] is Utterance
            else kwargs["text_format"].model_validate(
                {
                    "summary": plan.summary,
                    "image_briefs": [],
                    "turns": [turn.model_dump() for turn in plan.turns],
                }
            )
        )
        return SimpleNamespace(status="completed", output_parsed=output)

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, None),
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    generator.prepare(value, requester, room, requester.questions, final=True)
    assert len(json.loads(requests[0]["input"])["questions"]) == 3
    room.plan = plan
    for index, turn in enumerate(plan.turns):
        output = generator.utter(value, requester, room)
        payload = json.loads(requests[-1]["input"])
        assert payload["questions"] == [requester.questions[index // 3].model_dump(mode="json")]
        assert payload["weeklyQuestionCount"] == 3
        assert payload["weeklySummary"] is None
        assert len(payload["conversation"]) == index
        room.messages.append(SavedMessage(participant=turn.participant, text=output.text))

    # Do not activate a plan that invents a source, omits one, or returns to an old topic.
    for replacement in ("x" * 43, None, requester.questions[0].record_id):
        invalid = plan.model_copy(deep=True)
        invalid.turns[6].record_id = replacement
        with pytest.raises(MomotalkFailure):
            generator._validate_topics(invalid, requester)
    invalid = plan.model_copy(deep=True)
    for turn in invalid.turns:
        turn.record_id = requester.questions[0].record_id
    with pytest.raises(MomotalkFailure):
        generator._validate_topics(invalid, requester)


@pytest.mark.parametrize(
    "scores",
    [
        (500, 500, 500),
        (800, 500, 500),
        (500, 200, 500),
        (800, 200, 500),
        (800, 800, 800),
        (200, 200, 200),
        (800, 800, 200),
        (800, 200, 200),
    ],
)
@pytest.mark.parametrize("final", [False, True])
def test_preparation_fixes_image_slots_and_accepts_four_discussions(scores, final):
    value = snapshot(scores=scores)
    requester = value.requesters[0]
    requester.questions = [
        requester.questions[0].model_copy(update={"record_id": str(index) * 43})
        for index in range(4)
    ]
    state = State(value)
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    # Reproduce the valid four-topic plan that used to fail on both topic count
    # and fixed rotation, before any utterances or paid images were generated.
    turns = [
        Turn(
            participant=PARTICIPANTS[index % 3],
            topic=f"架空の反応{index}",
            record_id=requester.questions[index // 3].record_id,
        )
        for index in range(12)
    ]
    brief = {"record_id": requester.questions[0].record_id, "brief": "架空の散歩道で自撮り"}
    payload: dict[str, Any] = {
        "summary": "架空の4つの相談。",
        "image_briefs": [
            {**brief, "brief": f"架空の撮影場面{index}"} for index in range(len(room.images))
        ],
        **({"turns": [turn.model_dump() for turn in turns]} if final else {}),
    }
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed", output_parsed=kwargs["text_format"].model_validate(payload)
        )

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, None),
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    result = generator.prepare(value, requester, room, requester.questions, final=final)
    assert isinstance(result, ConversationPlan if final else WeekDigest)
    assert len(calls) == 1
    assert [image.mood for image in result.images] == [image.mood for image in room.images]
    assert [image.brief for image in result.images] == [
        brief["brief"] for brief in payload["image_briefs"]
    ]
    assert json.loads(calls[0]["input"])["imageTargets"] == [
        image.model_dump() for image in room.images
    ]
    schema = calls[0]["text_format"]
    # Wrong image counts fail at the provider schema, before any image API call.
    for briefs in (payload["image_briefs"] + [brief], payload["image_briefs"][:-1]):
        if len(briefs) != len(room.images):
            with pytest.raises(ValidationError):
                schema.model_validate({**payload, "image_briefs": briefs})
    if isinstance(result, ConversationPlan):
        assert Counter(turn.participant for turn in result.turns) == dict.fromkeys(PARTICIPANTS, 4)
        assert [turn.record_id for turn in result.turns] == [turn.record_id for turn in turns]
        assert result.turns[0] == turns[0]
        assert Counter((t.participant, t.record_id, t.topic) for t in result.turns) == Counter(
            (t.participant, t.record_id, t.topic) for t in turns
        )
        # The assembled checkpoint still passes the original strict plan contract.
        ConversationPlan.model_validate_json(result.model_dump_json())
        payload["turns"][0]["participant"] = PARTICIPANTS[1]
        payload["turns"][2]["participant"] = PARTICIPANTS[1]
        with pytest.raises(MomotalkFailure):
            generator.prepare(value, requester, room, requester.questions, final=True)
    elif room.images:
        payload["image_briefs"][0]["record_id"] = "x" * 43
        with pytest.raises(MomotalkFailure):
            generator.prepare(value, requester, room, requester.questions, final=False)


@pytest.mark.parametrize("legacy", [False, True])
def test_stored_full_image_repairs_missing_thumbnail_without_overwriting_original(legacy):
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1536), "skyblue").save(buffer, format="WEBP")
    state = State(snapshot(scores=(850, 150, 500)))
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    if legacy:
        room.images = [SavedImage(participant="participant-a", mood="happy")]
    target = room.images[0]
    full = image_key(room, target)
    assert full.endswith("/happy.webp" if legacy else "/selfie.webp")
    writes = []
    client = SimpleNamespace(
        list_objects_v2=lambda **_: {"Contents": [{"Key": full}]},
        get_object=lambda **_: {"Body": io.BytesIO(buffer.getvalue())},
        put_object=lambda **kwargs: writes.append(kwargs),
    )
    assets = MomotalkAssets(cast(Any, client), "private-media")
    assert assets.image_exists(room, target)
    assert len(writes) == 1
    assert writes[0]["Key"] == image_key(room, target, thumbnail=True)
    assert writes[0]["ServerSideEncryption"] == "AES256"
    with Image.open(io.BytesIO(writes[0]["Body"])) as thumbnail:
        assert thumbnail.size == (320, 480)


def test_image_download_is_short_lived_and_input_cleanup_removes_exact_version():
    calls = []
    client = SimpleNamespace(
        delete_object=lambda **kwargs: calls.append(kwargs),
        generate_presigned_url=lambda operation, **kwargs: (
            calls.append({"operation": operation, **kwargs}) or "https://example.invalid/image"
        ),
    )
    state = State()
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    assets = MomotalkAssets(cast(Any, client), "private-media")
    assets.delete_input(WEEK.week_id, "exact-frozen-version")
    assert calls[0]["VersionId"] == "exact-frozen-version"
    room = state.room
    assert room is not None
    assets.image_url(room, room.images[0], download=True)
    assert calls[1]["ExpiresIn"] == 300
    assert calls[1]["Params"]["ResponseContentDisposition"].startswith("attachment;")


@pytest.mark.parametrize("scores", [(850, 850, 850), (150, 150, 150), (850, 850, 150)])
def test_all_subjects_share_one_stored_image_and_one_download(scores):
    state = State(snapshot(scores=scores))
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None and len(room.images) == 1
    assert len(room.images[0].image_subjects) == 3
    stored: dict[str, bytes] = {}
    writes = []
    signed = []

    def put_object(**kwargs):
        writes.append(kwargs)
        stored[kwargs["Key"]] = kwargs["Body"]

    def presign(operation, **kwargs):
        signed.append({"operation": operation, **kwargs})
        return "https://media.example.invalid/" + kwargs["Params"]["Key"]

    client = SimpleNamespace(
        list_objects_v2=lambda **kwargs: {
            "Contents": [{"Key": key} for key in stored if key.startswith(kwargs["Prefix"])]
        },
        put_object=put_object,
        generate_presigned_url=presign,
    )
    assets = MomotalkAssets(cast(Any, client), "private-media")
    for index, target in enumerate(room.images):
        assert not assets.image_exists(room, target)
        buffer = io.BytesIO()
        Image.new("RGB", (256, 256), (index * 80, 100, 200)).save(buffer, format="PNG")
        assets.store_image(room, target, buffer.getvalue())
        assert assets.image_exists(room, target)

    assert len(stored) == len(writes) == 2
    originals = [assets.image_url(room, image) for image in room.images]
    thumbnails = [assets.image_url(room, image, thumbnail=True) for image in room.images]
    downloads = [assets.image_url(room, image, download=True) for image in room.images]
    assert len(set(originals + thumbnails)) == 2
    assert downloads == originals
    assert signed[-1]["Params"]["ResponseContentDisposition"] == (
        f'attachment; filename="momotalk-{room.week_id}-selfie.webp"'
    )
    assert all(entry["ExpiresIn"] == 300 for entry in signed)
    assert all(entry["ContentType"] == "image/webp" for entry in writes)
    assert all(entry["ServerSideEncryption"] == "AES256" for entry in writes)
