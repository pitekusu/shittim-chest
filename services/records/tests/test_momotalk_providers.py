"""Provider contracts and paid-image recovery use only fictional local fixtures."""

import io
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image
from tests.test_momotalk import WEEK, State, snapshot

from shittim_records.momotalk import SavedMessage, Utterance, image_key
from shittim_records.momotalk_adapters import MomotalkAssets
from shittim_records.momotalk_generation import collect_week
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator, requester_attitude


@pytest.mark.parametrize(
    "score,feeling",
    [
        (199, "strongly dislike"),
        (200, "cold"),
        (399, "cold"),
        (400, "neutral"),
        (599, "neutral"),
        (600, "like"),
        (799, "like"),
        (800, "adore"),
    ],
)
def test_affection_band_is_resolved_before_generation(score, feeling):
    assert feeling in requester_attitude(score)


def test_independent_persona_request_contains_history_and_week_facts():
    state = State(snapshot(scores=(500, 500, 500)))
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
        assert request["model"] == "gpt-5.6-luna"
        assert request["store"] is False and request["tools"] == []
        assert state.snapshot.personas[slot] in request["instructions"]
        assert all(
            prompt not in request["instructions"]
            for other, prompt in state.snapshot.personas.items()
            if other != slot
        )
        assert len(payload["conversation"]) == index
        assert payload["questionCount"] == 1
        assert payload["questions"][0]["text"] == state.snapshot.requesters[0].questions[0].text
        assert payload["yourAffection"] == 500
        assert requester_attitude(500) in request["instructions"]


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
