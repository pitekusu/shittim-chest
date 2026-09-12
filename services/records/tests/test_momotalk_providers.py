"""Provider contracts and paid-image recovery use only fictional local fixtures."""

import io
import json
from collections import Counter
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import ValidationError
from tests.test_momotalk import WEEK, State, snapshot

from shittim_records.momotalk import (
    PARTICIPANTS,
    ConversationPlan,
    MomotalkFailure,
    SavedMessage,
    Turn,
    Utterance,
    WeekDigest,
    image_key,
)
from shittim_records.momotalk_adapters import MomotalkAssets
from shittim_records.momotalk_generation import collect_week
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator, requester_attitude


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
        assert request["model"] == "gpt-5.6-luna"
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
    "scores", [(500, 500, 500), (800, 500, 500), (500, 200, 500), (800, 200, 500)]
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
        "image_briefs": [brief.copy() for _ in room.images],
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


def test_stored_full_image_repairs_missing_thumbnail_without_overwriting_original():
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 1536), "skyblue").save(buffer, format="WEBP")
    state = State()
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    room = state.room
    assert room is not None
    full = image_key(WEEK.week_id, room.room_id, "happy")
    writes = []
    client = SimpleNamespace(
        list_objects_v2=lambda **_: {"Contents": [{"Key": full}]},
        get_object=lambda **_: {"Body": io.BytesIO(buffer.getvalue())},
        put_object=lambda **kwargs: writes.append(kwargs),
    )
    assets = MomotalkAssets(cast(Any, client), "private-media")
    assert assets.image_exists(room, "happy")
    assert len(writes) == 1
    assert writes[0]["Key"] == image_key(WEEK.week_id, room.room_id, "happy", thumbnail=True)
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
    assets.image_url(cast(Any, state.room), "happy", download=True)
    assert calls[1]["ExpiresIn"] == 300
    assert calls[1]["Params"]["ResponseContentDisposition"].startswith("attachment;")
