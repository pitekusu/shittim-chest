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


@pytest.fixture(scope="module")
def artwork() -> Image.Image:
    return Image.new("RGBA", (1792, 832), _ART_COLOR)


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


def _render(
    artwork: Image.Image,
    rarity: int | None,
    *,
    student_name: str = "サンプル生徒",
    performer: ParticipantSlot = ParticipantSlot.PARTICIPANT_A,
    school_branding: SchoolBranding | None = None,
) -> Image.Image:
    presentation = _presentation(rarity, student_name=student_name, performer=performer)
    panels = student_gacha_card._card_panels(presentation, "サンプル先生", school_branding)
    return student_gacha_card._compose_frame(artwork, rarity, panels).convert("RGB")


def _gif(
    *,
    size: tuple[int, int] = GIF_ART_SIZE,
    frame_count: int = GACHA_APNG_FRAME_COUNT,
    panel_motion_only: bool = False,
) -> bytes:
    frames = []
    for index in range(frame_count):
        color = (19 + 19 * index, 105, 175)
        if panel_motion_only:
            frame = Image.new("RGB", size, "#a5b4cd")
            ImageDraw.Draw(frame).rectangle((180, 395, 210, 425), fill=color)
        else:
            frame = Image.new("RGB", size, color)
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
    result = render_card(_gif(), presentation, teacher, school_branding=school)

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
                    assert all(
                        ImageChops.multiply(band, panel_mask).getbbox() is None
                        for band in difference.split()
                    )
                    artwork_colors.add(frame.getpixel((525, 200)))
        assert tuple(durations) == GACHA_APNG_FRAME_DURATIONS
        assert sum(durations) == 1600
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
        render_card(_gif(size=size, frame_count=frame_count), _presentation(3), "サンプル先生")


def test_card_rejects_truncated_input() -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_image_invalid"):
        render_card(_gif()[:-120] + b";", _presentation(3), "サンプル先生")


def test_card_rejects_animation_that_becomes_still_after_composition() -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_card_render_failed"):
        render_card(_gif(panel_motion_only=True), _presentation(3), "サンプル先生")


@pytest.mark.parametrize(
    ("rarity", "expected"), [(None, 3), (0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
)
def test_card_shows_the_selected_number_of_stars(
    artwork: Image.Image, rarity: int | None, expected: int
) -> None:
    with _render(artwork, rarity) as card:
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
def test_card_displays_performer_then_student_and_variant(
    artwork: Image.Image, student_name: str, expected: str
) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        _render(artwork, 3, student_name=student_name, performer=ParticipantSlot.PARTICIPANT_B)

    labels = [call.args[2] for call in label.call_args_list]
    assert labels.count(expected) == 1
    assert "プラナ" not in labels
    assert all(not name.startswith("コスプレ") for name in labels)


def test_card_renders_the_supplied_school_name_and_logo(artwork: Image.Image) -> None:
    png = BytesIO()
    logo = Image.new("RGBA", (400, 300))
    ImageDraw.Draw(logo).rectangle((40, 120, 359, 199), fill="#ff00ea")
    logo.save(png, "PNG")
    school = SchoolBranding(name="トリニティ総合学園", logo=png.getvalue())

    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        output = _render(artwork, 3, school_branding=school)

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
    artwork: Image.Image, branding: SchoolBranding | None, expected_name: str
) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        output = _render(artwork, 3, school_branding=branding)

    labels = [(call.args[1][0], call.args[2]) for call in label.call_args_list]
    assert (63, expected_name) in labels
    with output as card:
        assert card.getpixel((1217, 251)) == (252, 253, 255)


@pytest.mark.parametrize("rarity", [None, 0, 1, 2, 3])
def test_card_background_matches_rarity_without_tinting_the_center(
    artwork: Image.Image, rarity: int | None
) -> None:
    with _render(artwork, rarity) as card:
        edge = card.getpixel((0, 30))
        assert isinstance(edge, tuple)
        red, green, blue = edge
        match rarity:
            case None | 3:
                assert edge == _ART_COLOR
            case 0:
                assert min(edge) > 228 and max(edge) - min(edge) < 18
            case 1:
                assert blue > green > red and blue - red > 60
            case 2:
                assert red > green > blue and red - blue > 70
        assert card.getpixel((640, 240)) == _ART_COLOR


def test_four_star_reveal_changes_the_edges_and_preserves_portrait_and_panel(
    artwork: Image.Image,
) -> None:
    with _render(artwork, 4) as card, _render(artwork, None) as normal:
        assert card.size == (1792, 828)
        assert card.getpixel((84, 96)) == (255, 243, 191)
        assert normal.getpixel((84, 96)) == _ART_COLOR
        assert card.getpixel((24, 230)) != normal.getpixel((24, 230))
        assert len({card.getpixel((0, y)) for y in (0, 118, 236, 354, 472, 590, 708)}) == 7
        assert (
            card.crop((560, 100, 1000, 550)).tobytes()
            == normal.crop((560, 100, 1000, 550)).tobytes()
        )
        assert (
            card.crop((1250, 270, 1495, 520)).tobytes()
            == normal.crop((1250, 270, 1495, 520)).tobytes()
        )
