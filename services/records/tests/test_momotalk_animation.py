"""Weekly animated selfie selection, generation and private media recovery."""

import base64
import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image, ImageSequence
from tests.test_momotalk import snapshot

from shittim_records.momotalk import (
    ANIMATION_FIRST_WEEK,
    ANIMATION_FRAME_COUNT,
    ANIMATION_FRAME_MS,
    ANIMATION_SIZE,
    ImageChoice,
    MomotalkFailure,
    Room,
    SavedImage,
    image_key,
    image_targets,
)
from shittim_records.momotalk_adapters import MomotalkAssets
from shittim_records.momotalk_openai import OpenAIMomotalkGenerator


@pytest.mark.parametrize(
    "scores,expected",
    [
        ((800, 399, 400), ("happy", "unhappy", "neutral")),
        ((799, 600, 599), ("happy", "happy", "neutral")),
        ((201, 200, 0), ("unhappy", "unhappy", "unhappy")),
        ((500, 500, 500), ("neutral", "neutral", "neutral")),
    ],
)
def test_new_week_has_one_frozen_selfie_for_every_active_requester(scores, expected):
    requester = snapshot(scores=scores).requesters[0]
    (image,) = image_targets(requester, ANIMATION_FIRST_WEEK)
    image = SavedImage.model_validate_json(image.model_dump_json())
    assert image.animated
    subjects = {subject.participant: subject for subject in image.image_subjects}
    assert len(subjects) == 3
    assert {
        slot: (subject.affection_score, subject.mood) for slot, subject in subjects.items()
    } == {
        slot: (score, mood)
        for (slot, score), mood in zip(requester.scores.items(), expected, strict=True)
    }
    requester.questions.clear()
    assert image_targets(requester, ANIMATION_FIRST_WEEK) == []


def test_cutover_and_existing_snapshot_keep_static_assets():
    requester = snapshot(scores=(500, 500, 500)).requesters[0]
    assert image_targets(requester, ANIMATION_FIRST_WEEK - timedelta(weeks=1)) == []
    image = SavedImage.model_validate(
        {"participant": "participant-a", "mood": "happy", "state": "ready"}
    )
    assert not image.animated


@pytest.mark.parametrize(
    "scores,poses",
    [
        ((200, 201, 400), ("身を引いて", "控えめに距離を取る", "まっすぐ立つ")),
        ((599, 600, 800), ("まっすぐ立つ", "親しげに微笑み", "身を乗り出す")),
    ],
)
def test_generator_requests_native_animation_with_frozen_poses(scores, poses):
    weekly = snapshot(scores=scores)
    requester = weekly.requesters[0]
    (image,) = image_targets(requester, ANIMATION_FIRST_WEEK)
    # A repair recollects current scores. The checkpoint still owns the original poses.
    requester.scores = dict.fromkeys(requester.scores, 500)
    calls = []
    raw = b"fictional-provider-gif"

    def edit(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(raw).decode())])

    generator = OpenAIMomotalkGenerator(
        cast(Any, None),
        cast(Any, SimpleNamespace(load_participant_reference=lambda slot: slot.encode())),
        client=SimpleNamespace(images=SimpleNamespace(edit=edit)),
    )
    choice = ImageChoice(
        mood=image.mood, record_id=requester.questions[0].record_id, brief="紅葉の公園で撮影"
    )
    assert generator.selfie(weekly, requester, image, choice) == raw
    (call,) = calls
    assert (call["output_format"], call["frame_count"], call["frame_duration_ms"], call["n"]) == (
        "gif",
        ANIMATION_FRAME_COUNT,
        ANIMATION_FRAME_MS,
        1,
    )
    assert call["size"] == "480x720"
    assert len(call["image"]) == 3
    for slot, pose in zip(requester.scores, poses, strict=True):
        line = next(line for line in call["prompt"].splitlines() if f"({slot}.png)" in line)
        assert pose in line
    assert choice.brief in call["prompt"]
    assert requester.questions[0].text not in call["prompt"]


def test_store_repairs_gif_poster_and_signs_each_format():
    requester = snapshot().requesters[0]
    (image,) = image_targets(requester, ANIMATION_FIRST_WEEK)
    room = Room(
        week_id=ANIMATION_FIRST_WEEK,
        room_id=requester.room_id,
        display_name=requester.display_name,
        question_count=len(requester.questions),
        images=[image],
    )
    frames = [
        Image.new("RGB", ANIMATION_SIZE, (index * 15, 90, 180))
        for index in range(ANIMATION_FRAME_COUNT)
    ]
    source = io.BytesIO()
    frames[0].save(
        source, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=1
    )
    stored = {}
    writes = []
    signed = []

    def put_object(**kwargs):
        writes.append(kwargs)
        stored[kwargs["Key"]] = kwargs["Body"]

    client = SimpleNamespace(
        list_objects_v2=lambda **kwargs: {
            "Contents": [{"Key": key} for key in stored if key.startswith(kwargs["Prefix"])]
        },
        put_object=put_object,
        get_object=lambda **kwargs: {"Body": io.BytesIO(stored[kwargs["Key"]])},
        generate_presigned_url=lambda _, **kwargs: signed.append(kwargs),
    )
    assets = MomotalkAssets(cast(Any, client), "private-media")
    with pytest.raises(MomotalkFailure, match="MOMOTALK_IMAGE_INVALID"):
        assets.store_image(room, image, source.getvalue()[:-1])
    assert not writes
    assets.store_image(room, image, source.getvalue())
    original = image_key(room, image)
    poster = image_key(room, image, thumbnail=True)
    assert original.endswith("/selfie.gif") and poster.endswith("/selfie-thumb.webp")
    assert [entry["ContentType"] for entry in writes] == ["image/gif", "image/webp"]
    assert all(entry["ServerSideEncryption"] == "AES256" for entry in writes)
    with Image.open(io.BytesIO(stored[original])) as animation:
        assert animation.size == ANIMATION_SIZE
        assert animation.info["loop"] == 0
        assert [frame.info["duration"] for frame in ImageSequence.Iterator(animation)] == [
            ANIMATION_FRAME_MS
        ] * ANIMATION_FRAME_COUNT
    before = stored.pop(poster)
    assert assets.image_exists(room, image)
    assert len(writes) == 3 and writes[-1]["Key"] == poster and stored[poster] == before
    with Image.open(io.BytesIO(stored[poster])) as thumbnail:
        assert thumbnail.format == "WEBP" and thumbnail.size == (320, 480)
        with pytest.raises(EOFError):
            thumbnail.seek(1)
    assets.image_url(room, image, download=True)
    assets.image_url(room, image, thumbnail=True, download=True)
    assert [call["Params"]["Key"] for call in signed] == [original, poster]
    assert signed[0]["Params"]["ResponseContentDisposition"].endswith('-selfie.gif"')
    assert signed[1]["Params"]["ResponseContentDisposition"].endswith('-selfie.webp"')
