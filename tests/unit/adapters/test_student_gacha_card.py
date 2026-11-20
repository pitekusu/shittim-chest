from __future__ import annotations

from io import BytesIO
from itertools import pairwise
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops, ImageDraw

from shittim_chest.adapters.assets import student_gacha_card
from shittim_chest.adapters.assets.student_gacha_card import GIF_ART_SIZE, render_card
from shittim_chest.application.student_gacha_models import (
    GACHA_APNG_FRAME_COUNT,
    GACHA_APNG_FRAME_DURATIONS,
    GACHA_GIF_SIZE,
    GachaGenerationFailure,
    GachaPresentation,
    SchoolBranding,
    Student,
)
from shittim_chest.domain import ParticipantSlot

_ART_COLOR = (184, 176, 212)
_GOLD = (248, 206, 97)
_KEY_COLOR = "#00FF00"


@pytest.fixture(scope="module")
def artwork() -> Image.Image:
    actor = Image.new("RGBA", GIF_ART_SIZE)
    ImageDraw.Draw(actor).rectangle((440, 96, 620, 414), fill=_ART_COLOR)
    return actor


def _presentation(
    rarity: int | None,
    *,
    student_name: str = "サンプル生徒",
    performer: ParticipantSlot = ParticipantSlot.PARTICIPANT_A,
) -> GachaPresentation:
    return GachaPresentation(
        student=Student(student_name, "サンプル学園", rarity=rarity),
        performer=performer,
        appearance="濃紺のジャケット",
        club="天体観測部",
        role="サポーター",
        weapon="SMG",
        greeting=("{teacher}、よろしくね!", "今日は星を観にいこう。", "案内は任せて!"),
    )


def _panels(
    rarity: int | None,
    *,
    student_name: str = "サンプル生徒",
    performer: ParticipantSlot = ParticipantSlot.PARTICIPANT_A,
    school_branding: SchoolBranding | None = None,
) -> Image.Image:
    presentation = _presentation(rarity, student_name=student_name, performer=performer)
    return student_gacha_card._card_panels(presentation, "サンプル先生", school_branding).convert(
        "RGB"
    )


def _gif(
    *,
    size: tuple[int, int] = GIF_ART_SIZE,
    frame_count: int = GACHA_APNG_FRAME_COUNT,
    panel_motion_only: bool = False,
    bad_key_frames: tuple[int, ...] = (),
) -> bytes:
    frames = []
    for index in range(frame_count):
        color = (19 + 19 * index, 105, 175)
        frame = Image.new("RGB", size, "#0011EE" if index in bad_key_frames else _KEY_COLOR)
        draw = ImageDraw.Draw(frame)
        shift = 0 if panel_motion_only else index
        draw.rectangle(
            (460 + shift, 144, 600 + shift, 330),
            fill=_ART_COLOR if panel_motion_only else color,
        )
        draw.rectangle((180, 395, 210, 425), fill=color)
        draw.rectangle((747, 160, 785, 180), fill=color)
        frames.append(frame)
    with BytesIO() as output:
        frames[0].save(output, "GIF", save_all=True, append_images=frames[1:], duration=170)
        return output.getvalue()


def test_animated_card_freezes_both_panels_while_the_art_moves() -> None:
    with BytesIO() as output:
        Image.new("RGBA", (48, 48), "#66b4dfbb").save(output, "PNG")
        school = SchoolBranding("サンプル学園", output.getvalue())
    presentation = _presentation(3)
    teacher = "サンプル先生"
    panels = student_gacha_card._card_panels(presentation, teacher, school)
    panel_mask = (
        panels.getchannel("A")
        .resize(GACHA_GIF_SIZE, Image.Resampling.LANCZOS)
        .point(lambda alpha: 255 if alpha else 0)
    )
    assert panel_mask.getpixel((195, 400)) == 255
    assert panel_mask.getpixel((765, 166)) == 255
    outside_actor = Image.new("L", GACHA_GIF_SIZE, 255)
    ImageDraw.Draw(outside_actor).rectangle((450, 130, 625, 350), fill=0)
    result = render_card(
        _gif(), presentation, teacher, key_color=_KEY_COLOR, school_branding=school
    )

    with Image.open(BytesIO(result)) as card:
        assert (card.format, card.size, getattr(card, "n_frames", 1)) == ("PNG", GACHA_GIF_SIZE, 12)
        assert card.mode == "RGB"
        assert card.info["loop"] == 0
        assert len(result) <= student_gacha_card.RESULT_MAX_BYTES

        artwork_colors = set()
        durations = []
        with card.convert("RGB") as first:
            assert first.getcolors(maxcolors=256) is None
            for index in range(GACHA_APNG_FRAME_COUNT):
                card.seek(index)
                durations.append(card.info["duration"])
                with card.convert("RGB") as frame:
                    difference = ImageChops.difference(first, frame)
                    for unchanged in (panel_mask, outside_actor):
                        assert all(
                            ImageChops.multiply(band, unchanged).getbbox() is None
                            for band in difference.split()
                        )
                    artwork_colors.add(frame.getpixel((525, 200)))
        assert tuple(durations) == GACHA_APNG_FRAME_DURATIONS
        assert sum(durations) == 1800
        assert len(artwork_colors) == 12


@pytest.mark.parametrize(
    ("size", "frame_count"),
    [
        ((512, 512), 12),
        (GIF_ART_SIZE, 1),
        (GIF_ART_SIZE, 11),
        (GIF_ART_SIZE, 13),
    ],
)
def test_card_rejects_unexpected_input_dimensions_or_frame_count(
    size: tuple[int, int], frame_count: int
) -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_image_invalid"):
        render_card(
            _gif(size=size, frame_count=frame_count),
            _presentation(3),
            "サンプル先生",
            key_color=_KEY_COLOR,
        )


def test_card_rejects_truncated_input() -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_image_invalid"):
        render_card(_gif()[:-120] + b";", _presentation(3), "サンプル先生", key_color=_KEY_COLOR)


def test_card_preserves_timing_when_animation_becomes_still_after_composition() -> None:
    result = render_card(
        _gif(panel_motion_only=True), _presentation(3), "サンプル先生", key_color=_KEY_COLOR
    )

    with Image.open(BytesIO(result)) as image:
        assert getattr(image, "n_frames", 1) == GACHA_APNG_FRAME_COUNT
        assert image.info["loop"] == 0
        first = image.convert("RGB").tobytes()
        durations = []
        for index in range(GACHA_APNG_FRAME_COUNT):
            image.seek(index)
            durations.append(image.info["duration"])
            assert image.convert("RGB").tobytes() == first
    assert tuple(durations) == GACHA_APNG_FRAME_DURATIONS


def test_card_reuses_a_keyed_frame_when_one_source_frame_loses_its_background() -> None:
    result = render_card(
        _gif(bad_key_frames=(3,)), _presentation(3), "サンプル先生", key_color=_KEY_COLOR
    )

    with Image.open(BytesIO(result)) as image:
        assert getattr(image, "n_frames", 1) == GACHA_APNG_FRAME_COUNT
        image.seek(2)
        previous = image.convert("RGB").tobytes()
        image.seek(3)
        assert image.info["duration"] == GACHA_APNG_FRAME_DURATIONS[3]
        assert image.convert("RGB").tobytes() == previous
        image.seek(4)
        assert image.convert("RGB").tobytes() != previous


def test_card_distinguishes_unusable_chroma_from_failed_apng_validation() -> None:
    presentation = _presentation(3)
    teacher = "サンプル先生"
    with pytest.raises(GachaGenerationFailure, match="gacha_chroma_screen_not_found"):
        render_card(
            _gif(bad_key_frames=tuple(range(GACHA_APNG_FRAME_COUNT))),
            presentation,
            teacher,
            key_color=_KEY_COLOR,
        )

    with (
        patch.object(student_gacha_card, "encode_gacha_apng", return_value=b"invalid"),
        pytest.raises(GachaGenerationFailure, match="gacha_card_apng_invalid"),
    ):
        render_card(_gif(), presentation, teacher, key_color=_KEY_COLOR)


@pytest.mark.parametrize(
    ("rarity", "expected"), [(None, 3), (0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
)
def test_card_shows_the_selected_number_of_stars(rarity: int | None, expected: int) -> None:
    with _panels(rarity) as card:
        # Count distinct gold spans across the centers of the stars on the nameplate.
        star_line = [card.getpixel((x, 585)) == _GOLD for x in range(132, 271)]
        stars = sum(value and not previous for previous, value in pairwise([False, *star_line]))

    assert stars == expected


@pytest.mark.parametrize(
    ("student_name", "expected"),
    [
        ("キキョウ\uff08水着\uff09", "プラナ\uff08キキョウ\uff09\uff08水着\uff09"),
        ("キキョウ(水着)", "プラナ\uff08キキョウ\uff09\uff08水着\uff09"),
        ("キキョウ", "プラナ\uff08キキョウ\uff09"),
    ],
)
def test_card_displays_performer_then_student_and_variant(student_name: str, expected: str) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        _panels(3, student_name=student_name, performer=ParticipantSlot.PARTICIPANT_B)

    labels = [call.args[2] for call in label.call_args_list]
    assert labels.count(expected) == 1
    assert "プラナ" not in labels
    assert all(not name.startswith("コスプレ") for name in labels)


def test_card_renders_the_supplied_school_name_and_logo() -> None:
    png = BytesIO()
    logo = Image.new("RGBA", (400, 300))
    ImageDraw.Draw(logo).rectangle((40, 120, 359, 199), fill="#ff00ea")
    logo.save(png, "PNG")
    school = SchoolBranding(name="トリニティ総合学園", logo=png.getvalue())

    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        output = _panels(3, school_branding=school)

    labels = [(call.args[1][0], call.args[2]) for call in label.call_args_list]
    assert (141, "トリニティ総合学園") in labels
    assert (63, "サンプル学園") not in labels
    with output as card:
        school_panel = card.crop((1160, 190, 1270, 315))
        pixels = [
            (index % school_panel.width, index // school_panel.width)
            for index, pixel in enumerate(school_panel.get_flattened_data())
            if pixel == (255, 0, 234)
        ]
    xs, ys = zip(*pixels, strict=True)
    assert 59 <= max(xs) - min(xs) <= 65
    assert 12 <= max(ys) - min(ys) <= 22


@pytest.mark.parametrize(
    ("branding", "expected_name"),
    [(None, "サンプル学園"), (SchoolBranding(name="柵川中学", logo=None), "柵川中学")],
)
def test_school_name_uses_full_header_when_no_logo_exists(
    branding: SchoolBranding | None, expected_name: str
) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        output = _panels(3, school_branding=branding)

    labels = [(call.args[1][0], call.args[2]) for call in label.call_args_list]
    assert (63, expected_name) in labels
    with output as card:
        assert card.getpixel((1217, 251)) == (252, 253, 255)


def test_rarity_changes_the_backdrop_without_tinting_the_actor_or_school_panel(
    artwork: Image.Image,
) -> None:
    presentation = _presentation(3)
    panels = student_gacha_card._card_panels(presentation, "サンプル先生", None).resize(
        GACHA_GIF_SIZE, Image.Resampling.LANCZOS
    )
    backdrop_region = (50, 60, 280, 300)
    actor_region = (460, 120, 580, 300)
    school_region = (728, 150, 843, 260)
    palette_samples = set()
    actors = set()
    school_panels = set()
    for rarity in range(5):
        background = student_gacha_card._card_background(rarity)
        with student_gacha_card._compose_frame(artwork, background, panels).convert("RGB") as frame:
            assert frame.size == GACHA_GIF_SIZE
            assert frame.getpixel((525, 200)) == _ART_COLOR
            palette_samples.add(frame.crop(backdrop_region).tobytes())
            actors.add(frame.crop(actor_region).tobytes())
            school_panels.add(frame.crop(school_region).tobytes())

    assert len(palette_samples) == 5
    assert len(actors) == len(school_panels) == 1
