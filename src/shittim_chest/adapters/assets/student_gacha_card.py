"""Compose the recruitment reveal with deterministic Japanese text and artwork."""

from __future__ import annotations

import io
import math
import warnings
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from shittim_chest.adapters.assets.student_gacha import RESULT_MAX_BYTES, RESULT_SIZE
from shittim_chest.application.student_gacha_models import (
    TEACHER_PLACEHOLDER,
    GachaGenerationFailure,
)
from shittim_chest.domain import ParticipantSlot

if TYPE_CHECKING:
    from shittim_chest.application.student_gacha_models import GachaPresentation

_PERFORMER_NAMES: dict[ParticipantSlot, str] = {
    ParticipantSlot.PARTICIPANT_A: "アロナ",
    ParticipantSlot.PARTICIPANT_B: "プラナ",
    ParticipantSlot.PARTICIPANT_C: "安倍晋三",
}
_ART_SIZE = (1792, 832)
_NAVY = "#14284b"
_INK = "#293b54"
_WHITE = "#fcfdff"
_MUTED = "#506680"
_GOLD = "#f8ce61"
_STAR_CENTERS = (
    (),
    (69,),
    (52, 86),
    (35, 69, 103),
    (27, 56, 85, 114),
)
_SPARKLES = (
    (84, 96, 26),
    (154, 180, 10),
    (276, 66, 17),
    (366, 133, 8),
    (55, 320, 16),
    (231, 284, 20),
    (322, 368, 8),
    (117, 449, 24),
    (44, 562, 12),
    (72, 707, 20),
    (104, 761, 7),
    (1280, 70, 13),
    (1380, 106, 10),
    (1488, 56, 25),
    (1598, 120, 9),
    (1687, 94, 22),
    (1738, 251, 13),
    (1659, 350, 9),
    (1717, 493, 24),
    (1658, 603, 13),
    (1574, 714, 19),
    (1429, 765, 12),
    (1719, 750, 22),
)


def render_card(art: bytes, presentation: GachaPresentation, teacher: str) -> bytes:
    """Render the approved layout, without sending the display name to the provider."""
    if not 1 <= len(art) <= RESULT_MAX_BYTES:
        raise GachaGenerationFailure("gacha_image_invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(art), formats=["PNG"]) as source:
                if source.size != _ART_SIZE or getattr(source, "n_frames", 1) != 1:
                    raise GachaGenerationFailure("gacha_image_invalid")
                canvas = ImageOps.fit(source.convert("RGBA"), RESULT_SIZE)
        if presentation.student.rarity == 4:
            canvas.alpha_composite(_rare_reveal())
        canvas.alpha_composite(_dossier(presentation), dest=(1125, 141))
        canvas.alpha_composite(_dialogue(presentation, teacher), dest=(132, 555))
        output = io.BytesIO()
        canvas.convert("RGB").save(output, format="PNG", compress_level=6)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        raise GachaGenerationFailure("gacha_card_render_failed") from None
    return output.getvalue()


@cache
def _rare_reveal() -> Image.Image:
    """Decorate the edges while keeping the center of the artwork untouched."""
    width = RESULT_SIZE[0]
    wash = ImageOps.colorize(
        Image.linear_gradient("L"),
        black="#8357ff",
        mid="#ed419c",
        white="#36d7f2",
    ).resize(RESULT_SIZE, Image.Resampling.BILINEAR)
    alpha = bytes(round(150 * max(0, 1 - min(x, width - 1 - x) / 440) ** 1.5) for x in range(width))
    wash.putalpha(Image.frombytes("L", (width, 1), alpha).resize(RESULT_SIZE))
    sparkles = Image.new("RGBA", RESULT_SIZE)
    draw = ImageDraw.Draw(sparkles)
    for x, y, radius in _SPARKLES:
        glow = radius * 1.35
        draw.ellipse((x - glow, y - glow, x + glow, y + glow), fill="#ffe5ff38")
        inner = max(2, radius // 4)
        draw.polygon(
            (
                (x, y - radius),
                (x + inner, y - inner),
                (x + radius, y),
                (x + inner, y + inner),
                (x, y + radius),
                (x - inner, y + inner),
                (x - radius, y),
                (x - inner, y - inner),
            ),
            fill="#fff3bf" if radius >= 18 else "#effdff",
        )
    return Image.alpha_composite(wash, sparkles)


def _dialogue(presentation: GachaPresentation, teacher: str) -> Image.Image:
    layer = Image.new("RGBA", (702, 225))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((17, 77, 694, 221), radius=14, fill="#24325348")
    draw.rounded_rectangle(
        (13, 72, 688, 214), radius=13, fill="#f9fcfff7", outline="#ffffff", width=2
    )
    draw.rounded_rectangle((21, 80, 680, 206), radius=9, outline="#b5c0d487")
    draw.rounded_rectangle((0, 0, 547, 81), radius=12, fill=_NAVY, outline="#4e628b")
    rarity = 3 if presentation.student.rarity is None else presentation.student.rarity
    for x in _STAR_CENTERS[rarity]:
        draw.polygon(_star(x, 30, radius=13 if rarity == 4 else 16), fill=_GOLD)
    name_x, name_width = 23, 491
    if rarity:
        draw.line((139, 12, 139, 69), fill="#687da4")
        name_x, name_width = 159, 355
    performer = _PERFORMER_NAMES[presentation.performer]
    name = f"{presentation.student.name}\uff08{performer}\uff09"
    _label(draw, (name_x, 23), name, 37, name_width, _WHITE, bold=True)
    for index, greeting in enumerate(presentation.greeting):
        line = greeting.replace(TEACHER_PLACEHOLDER, teacher)
        _label(draw, (43, 95 + index * 34), line, 26, 600, _INK)
    return layer


def _dossier(presentation: GachaPresentation) -> Image.Image:
    layer = Image.new("RGBA", (483, 506))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((20, 30, 478, 498), radius=17, fill="#273a663d")
    draw.rounded_rectangle(
        (12, 16, 469, 487), radius=14, fill="#b7c7e5dd", outline="#e1ecff", width=2
    )
    draw.rounded_rectangle((21, 7, 450, 475), radius=8, fill=_WHITE, outline="#ffffff", width=2)
    draw.rectangle((41, 55, 431, 452), outline="#a9b7cc")
    draw.rounded_rectangle((63, 79, 123, 140), radius=12, fill="#17283f")
    draw.polygon(((93, 87), (115, 121), (93, 133), (71, 121)), outline="#e9f2ff", width=2)
    draw.polygon(((93, 94), (104, 117), (93, 123), (82, 117)), outline="#e9f2ff", width=2)
    _label(draw, (43, 25), "生徒募集", 16, 360, "#54748f", bold=True)
    school = (
        "ゲヘナ学園" if presentation.student.school == "ゲヘナ" else presentation.student.school
    )
    school_y = 80 if school == "ゲヘナ学園" else 91
    _label(draw, (141, school_y), school, 33, 267, "#182a41", bold=True)
    if school == "ゲヘナ学園":
        _label(draw, (142, 121), "GEHENNA ACADEMY", 16, 267, "#59708a")

    rows = (
        ("部活", presentation.club, _INK),
        ("戦術的な役割", presentation.role, "#b34831"),
        ("使用武器種", presentation.weapon, _INK),
    )
    for index, (label, value, color) in enumerate(rows):
        y = 161 + index * 98
        draw.rectangle((42, y, 430, y + 35), fill="#f0f4fc")
        _label(draw, (236, y + 7), label, 18, 350, _MUTED, center=True)
        _label(draw, (236, y + 48), value, 31, 350, color, bold=True, center=True)
    return layer.rotate(3, resample=Image.Resampling.BICUBIC, expand=True)


def _star(x: int, y: int, *, radius: int) -> list[tuple[float, float]]:
    points = []
    for index in range(10):
        angle = index * math.pi / 5 - math.pi / 2
        distance = radius if index % 2 == 0 else round(radius * 7 / 16)
        points.append((x + distance * math.cos(angle), y + distance * math.sin(angle)))
    return points


@cache
def _font_bytes(bold: bool) -> bytes:
    weight = "Bold" if bold else "Regular"
    return (
        files("shittim_chest.adapters.assets")
        .joinpath(f"fonts/LINESeedJP-{weight}.woff2")
        .read_bytes()
    )


def _label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    size: int,
    width: int,
    color: str,
    *,
    bold: bool = False,
    center: bool = False,
) -> None:
    font_bytes = _font_bytes(bold)
    font = ImageFont.truetype(io.BytesIO(font_bytes), size)
    while font.getlength(text) > width and size > 8:
        size -= 1
        font = ImageFont.truetype(io.BytesIO(font_bytes), size)
    draw.text(xy, text, fill=color, font=font, anchor="mt" if center else "lt")
