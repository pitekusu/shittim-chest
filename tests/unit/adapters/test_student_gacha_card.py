from __future__ import annotations

from io import BytesIO
from itertools import pairwise
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from shittim_chest.adapters.assets.student_gacha_card import render_card
from shittim_chest.application.student_gacha_models import (
    GachaPresentation,
    SchoolBranding,
    Student,
)
from shittim_chest.domain import ParticipantSlot

_ART_COLOR = (184, 176, 212)
_GOLD = (248, 206, 97)


@pytest.fixture(scope="module")
def artwork() -> bytes:
    output = BytesIO()
    Image.new("RGB", (1792, 832), _ART_COLOR).save(output, "PNG")
    return output.getvalue()


def _render(
    artwork: bytes,
    rarity: int | None,
    *,
    student_name: str = "サンプル生徒",
    performer: ParticipantSlot = ParticipantSlot.PARTICIPANT_A,
    school_branding: SchoolBranding | None = None,
) -> bytes:
    presentation = GachaPresentation(
        student=Student(student_name, "サンプル学園", rarity=rarity),
        performer=performer,
        appearance="濃紺のジャケット",
        club="天体観測部",
        role="サポーター",
        weapon="SMG",
        greeting=("{teacher}、よろしくね!", "今日は星を観にいこう。", "案内は任せて!"),
    )
    return render_card(artwork, presentation, "サンプル先生", school_branding=school_branding)


@pytest.mark.parametrize(
    ("rarity", "expected"), [(None, 3), (0, 0), (1, 1), (2, 2), (3, 3), (4, 4)]
)
def test_card_shows_the_selected_number_of_stars(
    artwork: bytes, rarity: int | None, expected: int
) -> None:
    with Image.open(BytesIO(_render(artwork, rarity))) as card:
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
    artwork: bytes, student_name: str, expected: str
) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        _render(artwork, 3, student_name=student_name, performer=ParticipantSlot.PARTICIPANT_B)

    labels = [call.args[2] for call in label.call_args_list]
    assert labels.count(expected) == 1
    assert "プラナ" not in labels
    assert all(not name.startswith("コスプレ") for name in labels)


def test_card_renders_the_supplied_school_name_and_logo(artwork: bytes) -> None:
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
    with Image.open(BytesIO(output)) as card:
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
    artwork: bytes, branding: SchoolBranding | None, expected_name: str
) -> None:
    with patch("shittim_chest.adapters.assets.student_gacha_card._label") as label:
        output = _render(artwork, 3, school_branding=branding)

    labels = [(call.args[1][0], call.args[2]) for call in label.call_args_list]
    assert (63, expected_name) in labels
    with Image.open(BytesIO(output)) as card:
        assert card.getpixel((1217, 251)) == (252, 253, 255)


@pytest.mark.parametrize("rarity", [None, 0, 1, 2, 3])
def test_card_background_matches_rarity_without_tinting_the_center(
    artwork: bytes, rarity: int | None
) -> None:
    with Image.open(BytesIO(_render(artwork, rarity))) as card:
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
    artwork: bytes,
) -> None:
    rare = _render(artwork, 4)
    assert rare == _render(artwork, 4)
    with Image.open(BytesIO(rare)) as card, Image.open(BytesIO(_render(artwork, None))) as normal:
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
