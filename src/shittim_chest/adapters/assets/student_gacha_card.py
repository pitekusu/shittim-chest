"""Compose the recruitment reveal with deterministic Japanese text and artwork."""

from __future__ import annotations

import io
import math
import re
import warnings
from functools import cache
from importlib.resources import files
from struct import error as StructError
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont, ImageOps

from shittim_chest.adapters.assets.student_gacha import (
    RESULT_MAX_BYTES,
    RESULT_SIZE,
    encode_gacha_apng,
    validate_gacha_result,
)
from shittim_chest.adapters.assets.student_gacha_matte import GachaChromaFailure, key_gacha_frames
from shittim_chest.application.student_gacha_models import (
    GACHA_APNG_FRAME_COUNT,
    GACHA_GIF_SIZE,
    TEACHER_PLACEHOLDER,
    GachaGenerationFailure,
    GachaImageFormat,
)
from shittim_chest.domain import ParticipantSlot

if TYPE_CHECKING:
    from shittim_chest.application.student_gacha_models import GachaPresentation, SchoolBranding

_PERFORMER_NAMES: dict[ParticipantSlot, str] = {
    ParticipantSlot.PARTICIPANT_A: "アロナ",
    ParticipantSlot.PARTICIPANT_B: "プラナ",
    ParticipantSlot.PARTICIPANT_C: "安倍晋三",
}
GIF_ART_SIZE = (1024, 480)
_NAVY = "#14284b"
_INK = "#293b54"
_WHITE = "#fcfdff"
_MUTED = "#506680"
_GOLD = "#f8ce61"
_COSTUME_NAME = re.compile(
    r"([^()\uff08\uff09]+)(?:\uff08([^()\uff08\uff09]+)\uff09|\(([^()\uff08\uff09]+)\))"
)
_STAR_CENTERS = (
    (),
    (69,),
    (52, 86),
    (35, 69, 103),
    (27, 56, 85, 114),
)
_BACKGROUND_PALETTES = (
    ("#8b9db6", "#dde6f2", "#ffffff"),
    None,
    ("#a4510e", "#f3b140", "#fff6cc"),
    ("#583bb2", "#b19bed", "#fff3ff"),
)
_RAINBOW = (
    "#4d40ed",
    "#ad49ef",
    "#ff4fac",
    "#ff9a56",
    "#fff47d",
    "#56e6ac",
    "#3acbf5",
    "#7764ff",
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


def render_card(
    art: bytes,
    presentation: GachaPresentation,
    teacher: str,
    *,
    key_color: str,
    school_branding: SchoolBranding | None = None,
) -> bytes:
    """Composite keyed animation frames over a static background and fixed text panels."""
    art_frames = _read_art_frames(art)
    try:
        actors = key_gacha_frames(art_frames, key_color)
    except GachaChromaFailure as error:
        raise GachaGenerationFailure(str(error)) from None
    try:
        background = _card_background(presentation.student.rarity)
        panels = _card_panels(presentation, teacher, school_branding).resize(
            GACHA_GIF_SIZE, Image.Resampling.LANCZOS
        )
        frames = [_compose_frame(actor, background, panels).convert("RGB") for actor in actors]
        # Reuse pixels under translucent borders and shadows as well as opaque panel pixels.
        panel_mask = panels.getchannel("A").point(lambda alpha: 255 if alpha else 0, mode="1")
        for frame in frames[1:]:
            frame.paste(frames[0], mask=panel_mask)
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ):
        raise GachaGenerationFailure("gacha_card_render_failed") from None
    try:
        result = encode_gacha_apng(frames)
    except OSError, ValueError:
        raise GachaGenerationFailure("gacha_card_apng_encode_failed") from None
    try:
        validate_gacha_result(result, image_format=GachaImageFormat.APNG)
    except GachaGenerationFailure:
        raise GachaGenerationFailure("gacha_card_apng_invalid") from None
    return result


def _read_art_frames(art: bytes) -> list[Image.Image]:
    if not 1 <= len(art) <= RESULT_MAX_BYTES or not art.endswith(b";"):
        raise GachaGenerationFailure("gacha_image_invalid")
    try:
        frames: list[Image.Image] = []
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with io.BytesIO(art) as buffer, Image.open(buffer, formats=["GIF"]) as source:
                for index in range(GACHA_APNG_FRAME_COUNT):
                    source.seek(index)
                    if source.size != GIF_ART_SIZE:
                        raise GachaGenerationFailure("gacha_image_invalid")
                    frames.append(source.convert("RGBA"))
                try:
                    source.seek(GACHA_APNG_FRAME_COUNT)
                except EOFError:
                    return frames
    except (
        EOFError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        IndexError,
        OSError,
        StructError,
        SyntaxError,
        ValueError,
    ):
        raise GachaGenerationFailure("gacha_image_invalid") from None
    raise GachaGenerationFailure("gacha_image_invalid")


def _card_panels(
    presentation: GachaPresentation, teacher: str, school_branding: SchoolBranding | None
) -> Image.Image:
    panels = Image.new("RGBA", RESULT_SIZE)
    panels.alpha_composite(_dossier(presentation, school_branding), dest=(1125, 141))
    panels.alpha_composite(_dialogue(presentation, teacher), dest=(132, 555))
    return panels


@cache
def _card_background(rarity: int | None) -> Image.Image:
    with (
        files(__package__).joinpath("student-gacha-backdrop.png").open("rb") as source,
        Image.open(source, formats=["PNG"]) as art,
    ):
        background = art.convert("RGBA")
    if rarity == 4:
        background.alpha_composite(_rare_reveal().resize(GACHA_GIF_SIZE, Image.Resampling.LANCZOS))
    else:
        palette = _BACKGROUND_PALETTES[3 if rarity is None else rarity]
        if palette is not None:
            black, mid, white = palette
            background = ImageOps.colorize(
                ImageOps.grayscale(background), black=black, mid=mid, white=white
            ).convert("RGBA")
    return background


def _compose_frame(art: Image.Image, background: Image.Image, panels: Image.Image) -> Image.Image:
    canvas = background.copy()
    canvas.alpha_composite(ImageOps.fit(art, canvas.size, Image.Resampling.LANCZOS))
    canvas.alpha_composite(panels)
    return canvas


@cache
def _edge_mask(strength: int) -> Image.Image:
    width = RESULT_SIZE[0]
    # The tint fades to zero before the central portrait area.
    alpha = bytes(
        round(strength * max(0, 1 - min(x, width - 1 - x) / 480) ** 1.3) for x in range(width)
    )
    return Image.frombytes("L", (width, 1), alpha).resize(RESULT_SIZE)


@cache
def _rare_reveal() -> Image.Image:
    """Add bright spectral bands and translucent facets outside the portrait area."""
    width, height = RESULT_SIZE
    swatch = Image.new("RGB", (1, len(_RAINBOW)))
    swatch_draw = ImageDraw.Draw(swatch)
    for y, color in enumerate(_RAINBOW):
        swatch_draw.point((0, y), fill=color)
    spectrum = swatch.resize((width // 2, height), Image.Resampling.BILINEAR)
    wash = Image.new("RGBA", RESULT_SIZE)
    wash.paste(spectrum, (0, 0))
    wash.paste(ImageOps.flip(spectrum), (width // 2, 0))
    wash.putalpha(_edge_mask(202))

    effects = Image.new("RGBA", RESULT_SIZE)
    draw = ImageDraw.Draw(effects)
    draw.polygon(((0, 50), (390, 161), (0, 218)), fill="#f7f4ff30", outline="#fcf6ff74", width=2)
    draw.polygon(((0, 489), (327, 315), (0, 562)), fill="#edfcff3d", outline="#fbffff78", width=2)
    draw.line(((0, 663), (297, 497)), fill="#faffff80", width=3)
    draw.line(((0, 672), (270, 518)), fill="#c8faff44", width=2)
    effects = Image.alpha_composite(effects, effects.transpose(Image.Transpose.ROTATE_180))
    draw = ImageDraw.Draw(effects)
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
    return Image.alpha_composite(wash, effects)


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
    student_name = presentation.student.name
    costume = _COSTUME_NAME.fullmatch(student_name)
    if costume is None:
        name = f"{performer}\uff08{student_name}\uff09"
    else:
        student, fullwidth_variant, ascii_variant = costume.groups()
        name = f"{performer}\uff08{student}\uff09\uff08{fullwidth_variant or ascii_variant}\uff09"
    _label(draw, (name_x, 23), name, 37, name_width, _WHITE, bold=True)
    for index, greeting in enumerate(presentation.greeting):
        line = greeting.replace(TEACHER_PLACEHOLDER, teacher)
        _label(draw, (43, 95 + index * 34), line, 26, 600, _INK)
    return layer


def _dossier(
    presentation: GachaPresentation, school_branding: SchoolBranding | None
) -> Image.Image:
    layer = Image.new("RGBA", (483, 506))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((20, 30, 478, 498), radius=17, fill="#273a663d")
    draw.rounded_rectangle(
        (12, 16, 469, 487), radius=14, fill="#b7c7e5dd", outline="#e1ecff", width=2
    )
    draw.rounded_rectangle((21, 7, 450, 475), radius=8, fill=_WHITE, outline="#ffffff", width=2)
    draw.rectangle((41, 55, 431, 452), outline="#a9b7cc")
    _label(draw, (43, 25), "生徒募集", 16, 360, "#54748f", bold=True)
    school = school_branding.name if school_branding is not None else presentation.student.school
    school_x = 63
    if school_branding is not None and school_branding.logo is not None:
        logo = _school_logo(school_branding.logo)
        if logo is not None:
            layer.alpha_composite(logo, dest=(93 - logo.width // 2, 109 - logo.height // 2))
            school_x = 141
    _label(draw, (school_x, 93), school, 33, 408 - school_x, "#182a41", bold=True)

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


def _school_logo(png: bytes) -> Image.Image | None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(png), formats=["PNG"]) as source:
            logo = source.convert("RGBA")
    bounds = logo.getchannel("A").getbbox()
    if bounds is None:
        return None
    logo = logo.crop(bounds)
    logo.thumbnail((64, 64), resample=Image.Resampling.LANCZOS)
    return logo


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
