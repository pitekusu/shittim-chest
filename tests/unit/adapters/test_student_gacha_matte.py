from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from shittim_chest.adapters.assets.student_gacha_matte import (
    choose_gacha_chroma_key,
    key_gacha_frame,
)


def _reference(color: tuple[int, int, int, int]) -> bytes:
    with Image.new("RGBA", (64, 64), color) as image, io.BytesIO() as output:
        image.save(output, format="PNG")
        return output.getvalue()


def test_picks_a_key_away_from_visible_portrait_and_costume_colors() -> None:
    navy = _reference((27, 45, 132, 255))
    green = _reference((14, 197, 30, 255))
    magenta = _reference((236, 13, 223, 255))
    hidden_green = _reference((0, 255, 0, 0))

    assert choose_gacha_chroma_key(navy, navy) == "#00FF00"
    assert choose_gacha_chroma_key(navy, green) == "#FF00FF"
    assert choose_gacha_chroma_key(green, magenta) == "#00FF00"
    assert choose_gacha_chroma_key(navy, hidden_green) == "#00FF00"


def test_rejects_an_invalid_reference() -> None:
    with pytest.raises(ValueError, match="gacha_chroma_reference_invalid"):
        choose_gacha_chroma_key(b"not an image", _reference((12, 48, 75, 255)))


def test_removes_the_measured_green_screen_without_losing_color_or_hardening_edges() -> None:
    screen = (5, 246, 12)
    cyan = (46, 197, 220)
    deep_green = (32, 155, 82)
    pale_hair = (186, 219, 224)
    navy = (25, 34, 62)
    with Image.new("RGB", (64, 64), screen) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 4, 61, 15), fill=(21, 232, 32))
        draw.rectangle((22, 16, 42, 48), fill=cyan)
        draw.rectangle((29, 22, 34, 29), fill=deep_green)
        draw.rectangle((22, 40, 34, 48), fill=pale_hair)
        draw.line((20, 40, 20, 48), fill=(74, 231, 142))
        draw.line((21, 40, 21, 48), fill=(134, 223, 187))
        draw.line((35, 32, 42, 32), fill=(18, 109, 16))
        draw.line((42, 20, 47, 20), fill=navy)

        with key_gacha_frame(image, "#00FF00") as keyed:
            assert keyed.mode == "RGBA"
            assert keyed.getpixel((4, 4)) == (0, 0, 0, 0)
            assert keyed.getpixel((48, 8)) == (0, 0, 0, 0)
            assert keyed.getpixel((25, 20)) == (*cyan, 255)
            assert keyed.getpixel((31, 25)) == (*deep_green, 255)
            assert keyed.getpixel((25, 44)) == (*pale_hair, 255)
            wisp = keyed.getpixel((45, 20))
            assert isinstance(wisp, tuple) and wisp[:3] == navy and 128 < wisp[3] < 255
            edge1, edge2 = keyed.getpixel((20, 44)), keyed.getpixel((21, 44))
            assert isinstance(edge1, tuple) and isinstance(edge2, tuple)
            assert 0 < edge1[3] < edge2[3] < 255
            for red, green, blue, _alpha in (edge1, edge2):
                assert green <= max(red, blue) + 7
            hair_gap = keyed.getpixel((36, 32))
            assert isinstance(hair_gap, tuple)
            red, green, blue, alpha = hair_gap
            assert alpha < 255 and green <= max(red, blue) + 7


def test_removes_magenta_without_losing_green_or_preserving_magenta_spill() -> None:
    actor = (44, 150, 80)
    violet = (131, 60, 159)
    with Image.new("RGB", (64, 64), (247, 8, 242)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((24, 14, 44, 49), fill=actor)
        draw.rectangle((31, 27, 38, 39), fill=violet)
        draw.line((23, 14, 23, 49), fill=(161, 79, 172))

        with key_gacha_frame(image, "#FF00FF") as keyed:
            assert keyed.getpixel((4, 4)) == (0, 0, 0, 0)
            assert keyed.getpixel((27, 25)) == (*actor, 255)
            assert keyed.getpixel((34, 33)) == (*violet, 255)
            hard_edge = keyed.getpixel((35, 14))
            assert isinstance(hard_edge, tuple)
            assert hard_edge[:3] == actor and 0 < hard_edge[3] < 255
            edge = keyed.getpixel((23, 25))
            assert isinstance(edge, tuple)
            red, green, blue, alpha = edge
            assert 0 < alpha < 255
            assert min(red, blue) <= green + 7


@pytest.mark.parametrize(
    ("color", "key_color", "error"),
    [
        ((0, 255, 0), "#00FF00", "gacha_chroma_subject_or_background_missing"),
        ((0, 255, 0), "#FF00FF", "gacha_chroma_screen_not_found"),
        ((0, 255, 0), "black", "gacha_chroma_frame_invalid"),
    ],
)
def test_rejects_unusable_frames(color: tuple[int, int, int], key_color: str, error: str) -> None:
    with Image.new("RGB", (64, 64), color) as image, pytest.raises(ValueError, match=error):
        key_gacha_frame(image, key_color)
