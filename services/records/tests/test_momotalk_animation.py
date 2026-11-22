"""Weekly animated selfie selection, generation and private media recovery."""

import base64
import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageSequence, ImageStat
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
from shittim_records.momotalk_adapters import MomotalkAssets, decode_selfie_animation
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


@pytest.mark.parametrize("slips", [(0, 16, 32, 48), (0, 0, 0, 0)])
def test_only_a_slipping_provider_panel_is_realigned(slips):
    width, height = ANIMATION_SIZE
    background = Image.new("RGB", ANIMATION_SIZE)
    draw = ImageDraw.Draw(background)
    for x in range(0, width, 21):
        color = (64 + x * 7 % 172, 64 + x * 11 % 172, 64 + x * 17 % 172)
        draw.rectangle((x, 0, x + 20, height), fill=color)
    draw.rectangle((465, 0, 466, height), fill="black")
    frames = []
    for index in range(ANIMATION_FRAME_COUNT):
        frame = background.copy()
        ImageDraw.Draw(frame).rectangle((110 + index * 2, 280, 130 + index * 2, 300), fill="white")
        frames.append(ImageChops.offset(frame, -slips[index % len(slips)], 0))

    with io.BytesIO() as buffer:
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=ANIMATION_FRAME_MS,
            loop=0,
            disposal=2,
        )
        raw = buffer.getvalue()
    encoded, poster = decode_selfie_animation(raw)
    if not any(slips):
        assert encoded == raw
        return

    assert encoded != raw and poster.size == ANIMATION_SIZE
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.info["loop"] == 0
        decoded = []
        for frame in ImageSequence.Iterator(image):
            assert frame.info["duration"] == ANIMATION_FRAME_MS
            decoded.append(frame.convert("RGB"))
    assert len(decoded) == ANIMATION_FRAME_COUNT
    # The background stays registered and the small foreground motion remains.
    for frame in decoded:
        assert not sum(frame.crop((0, 20, width, 21)).convert("L").histogram()[:33])
        moving_area = ImageChops.difference(frame, decoded[0]).getbbox()
        if moving_area:
            assert 230 < moving_area[1] < 360 and moving_area[3] < 390
    assert ImageChops.difference(decoded[-1], decoded[0]).getbbox()


@pytest.mark.parametrize("wraps", [True, False], ids=["moving-wrap", "stationary-edge"])
def test_realigns_nonblack_wrap_even_when_some_frames_have_no_seam(wraps):
    width, height = ANIMATION_SIZE
    background = Image.new("RGB", (width + 64, height), (169, 186, 194))
    architecture = ImageDraw.Draw(background)
    for index, x in enumerate(range(0, background.width, 23)):
        ridge = x + 4 + index * 7 % 13
        architecture.rectangle(
            (x, 0, ridge, 180), fill=(75 + index * 73 % 135, 79 + index * 19 % 140, 100)
        )
        architecture.rectangle(
            (ridge, 550, x + 22, height), fill=(115, 65 + index * 29 % 172, 78 + index * 61 % 140)
        )
    architecture.rectangle((0, 210, background.width, 510), fill=(169, 190, 205))
    architecture.rectangle((0, 525, background.width, 548), fill=(193, 164, 132))

    frames = []
    for index in range(ANIMATION_FRAME_COUNT):
        slip, seam = ((0, 465), (16, 448), (35, 430), (51, None))[index % 4] if wraps else (0, 448)
        scene = background.copy()
        character_x = 212 + 5 * index
        ImageDraw.Draw(scene).rectangle(
            (character_x, 322, character_x + 23, 347), fill=(209, 57, 56)
        )
        frame = scene.crop((slip, 0, width + slip, height))
        if seam is not None:
            ImageDraw.Draw(frame).rectangle((seam, 0, width - 1, height - 1), fill=(65, 69, 185))
        frames.append(frame)

    with io.BytesIO() as buffer:
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=ANIMATION_FRAME_MS,
            loop=0,
            disposal=2,
        )
        raw = buffer.getvalue()

    encoded, poster = decode_selfie_animation(raw)
    if not wraps:
        assert encoded == raw
        return

    assert encoded != raw
    assert poster.size == ANIMATION_SIZE
    with Image.open(io.BytesIO(encoded)) as animation:
        assert animation.size == ANIMATION_SIZE
        assert animation.info["loop"] == 0
        decoded = []
        for frame in ImageSequence.Iterator(animation):
            assert frame.info["duration"] == ANIMATION_FRAME_MS
            decoded.append(frame.convert("RGB"))
    assert len(decoded) == ANIMATION_FRAME_COUNT

    band_boxes = ((16, 25, width - 16, 110), (16, 607, width - 16, 690))
    for frame in decoded:
        pixels = cast(tuple[tuple[int, int, int], ...], frame.get_flattened_data())
        assert not any(r < 100 and g < 100 and b > 160 for r, g, b in pixels)
        for box in band_boxes:
            change = ImageChops.difference(frame.crop(box), decoded[0].crop(box))
            assert max(ImageStat.Stat(change).mean) < 6
    foreground = (0, 260, width, 400)
    red_mask = [0] * 195 + [255] * 61
    first = decoded[0].crop(foreground).getchannel("R").point(red_mask).getbbox()
    last = decoded[-1].crop(foreground).getchannel("R").point(red_mask).getbbox()
    assert first is not None and last is not None
    assert first[2] - first[0] >= 20 and last[2] - last[0] >= 20
    assert last[0] - first[0] >= 40
