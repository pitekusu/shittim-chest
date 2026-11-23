from __future__ import annotations

import io

import pytest
from PIL import Image, ImageChops, ImageDraw

from shittim_chest.adapters.assets.student_gacha_matte import (
    GachaChromaFailure,
    choose_gacha_chroma_key,
    key_gacha_frame,
    key_gacha_frames,
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


def test_feathers_a_transparent_cutout_without_erasing_key_colored_details() -> None:
    with Image.new("RGBA", (64, 64), (16, 245, 128, 0)) as image:
        draw = ImageDraw.Draw(image)
        draw.polygon(((20, 8), (43, 16), (43, 53), (20, 53)), fill=(0, 255, 0, 255))
        draw.line((32, 53, 32, 63), fill=(0, 255, 0, 255))

        with key_gacha_frame(image, "#00FF00") as keyed:
            assert keyed.getpixel((32, 32)) == (0, 255, 0, 255)
            assert keyed.getpixel((4, 4)) == (0, 0, 0, 0)
            assert keyed.getpixel((30, 11)) == (0, 0, 0, 0)
            assert keyed.getbbox() == image.getbbox()
            for point in ((30, 12), (32, 63)):
                edge = keyed.getpixel(point)
                assert isinstance(edge, tuple)
                assert edge[:3] == (0, 255, 0) and 128 < edge[3] < 255


@pytest.mark.parametrize(
    ("key_color", "spill", "corrected"),
    [
        ("#00FF00", (43, 95, 85), (43, 85, 85)),
        ("#FF00FF", (113, 72, 101), (84, 72, 72)),
    ],
)
def test_despills_a_transparent_edge_without_recoloring_costume_details(
    key_color: str, spill: tuple[int, int, int], corrected: tuple[int, int, int]
) -> None:
    cyan = (45, 180, 216)
    with Image.new("RGBA", (64, 64)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((21, 12, 44, 54), fill="#18263e")
        draw.rectangle((31, 12, 44, 26), fill=key_color)
        draw.line((20, 29, 20, 40), fill=spill)
        draw.line((44, 33, 44, 42), fill=cyan)
        # Keep a detached detail in the key hue as well as the larger costume accent.
        draw.line((28, 7, 39, 7), fill=key_color)

        with key_gacha_frame(image, key_color) as cutout:
            fringe = cutout.getpixel((20, 35))
            assert isinstance(fringe, tuple)
            assert fringe[:3] == corrected and 128 < fringe[3] < 255
            assert cutout.getpixel((29, 35)) == (24, 38, 62, 255)
            for point in ((37, 12), (32, 7), (44, 37)):
                actual, original = cutout.getpixel(point), image.getpixel(point)
                assert isinstance(actual, tuple) and isinstance(original, tuple)
                assert actual[:3] == original[:3] and actual[3] > 128
            assert cutout.getpixel((19, 35)) == (0, 0, 0, 0)


@pytest.mark.parametrize("clear_through", [31, 47])
def test_removes_a_flat_screen_and_keeps_existing_transparency(clear_through: int) -> None:
    with Image.new("RGBA", (64, 64), (70, 245, 80, 255)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, clear_through, 63), fill=(230, 15, 40, 0))
        draw.rectangle((20, 8, 43, 53), fill=(40, 65, 180, 255))
        draw.rectangle((21, 22, 31, 40), fill=(20, 105, 50, 255))
        image.putpixel((30, 32), (230, 15, 40, 0))

        with key_gacha_frame(image, "#00FF00") as keyed:
            assert keyed.getpixel((4, 4)) == (0, 0, 0, 0)
            assert keyed.getpixel((60, 4)) == (0, 0, 0, 0)
            assert keyed.getpixel((38, 32)) == (40, 65, 180, 255)
            assert keyed.getpixel((30, 32)) == (0, 0, 0, 0)
            expected = keyed.tobytes()

        image.paste((0, 255, 0, 0), mask=ImageChops.invert(image.getchannel("A")))
        with key_gacha_frame(image, "#00FF00") as keyed_again:
            assert keyed_again.tobytes() == expected


def test_rejects_a_transparent_frame_without_a_visible_actor() -> None:
    with (
        Image.new("RGBA", (64, 64)) as image,
        pytest.raises(GachaChromaFailure, match="gacha_chroma_subject_or_background_missing"),
    ):
        key_gacha_frame(image, "#00FF00")


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


@pytest.mark.parametrize("key_color", ["#00FF00", "#FF00FF"])
def test_restores_rejected_frames_from_the_nearest_screen_that_was_keyed(
    key_color: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        Image.new("RGB", (64, 64), key_color) as first,
        Image.new("RGB", (64, 64), key_color) as last,
        Image.new("RGB", (64, 64), "blue") as rejected,
    ):
        ImageDraw.Draw(first).rectangle((20, 20, 40, 40), fill="red")
        ImageDraw.Draw(last).rectangle((20, 20, 40, 40), fill="white")

        restored = key_gacha_frames([rejected, rejected, first, rejected, last], key_color)

        assert len(restored) == 5
        assert restored[0] is restored[4]
        assert restored[1] is restored[2]
        assert restored[3] is restored[2]
        assert restored[2].tobytes() == key_gacha_frame(first, key_color).tobytes()
        assert restored[4].tobytes() == key_gacha_frame(last, key_color).tobytes()
        assert all(frame.getpixel((4, 4)) == (0, 0, 0, 0) for frame in restored)
        assert "student_gacha_chroma_recovered rejected_frames=3" in caplog.text


def test_fails_with_a_bounded_reason_when_no_frames_can_be_keyed() -> None:
    with (
        Image.new("RGB", (64, 64), "blue") as no_screen,
        Image.new("RGB", (64, 64), "#00FF00") as no_actor,
        pytest.raises(GachaChromaFailure, match="gacha_chroma_screen_not_found"),
    ):
        key_gacha_frames([no_screen, no_actor, no_screen], "#00FF00")
