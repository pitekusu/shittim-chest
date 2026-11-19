"""Choose and remove a flat color backdrop from student recruitment art."""

from __future__ import annotations

import io
import warnings
from collections import Counter
from typing import Final, cast

from PIL import Image, ImageChops, ImageFilter

from shittim_chest.adapters.assets.images import MAX_IMAGE_BYTES

_GREEN: Final = "#00FF00"
_MAGENTA: Final = "#FF00FF"
_KEY_RGB: Final = {_GREEN: (0, 255, 0), _MAGENTA: (255, 0, 255)}
_MAX_REFERENCE_PIXELS: Final = 12_000_000
_REFERENCE_SAMPLE_EDGE: Final = 256
_MAX_FRAME_PIXELS: Final = 1024 * 480
_EDGE_REACH_PIXELS: Final = 24

type _RGB = tuple[int, int, int]
type _RGBA = tuple[int, int, int, int]


def choose_gacha_chroma_key(portrait: bytes, costume: bytes) -> str:
    """Prefer the color farther from visible colors in both of the art references."""
    green_risk = magenta_risk = 0.0
    for reference in (portrait, costume):
        pixels = _reference_sample(reference)
        green_risk += sum(_collision((r, g, b), green_key=True) * a for r, g, b, a in pixels) / len(
            pixels
        )
        magenta_risk += sum(
            _collision((r, g, b), green_key=False) * a for r, g, b, a in pixels
        ) / len(pixels)
    return _GREEN if green_risk <= magenta_risk else _MAGENTA


def _reference_sample(content: bytes) -> tuple[_RGBA, ...]:
    if not 1 <= len(content) <= MAX_IMAGE_BYTES:
        raise ValueError("gacha_chroma_reference_invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=["PNG", "WEBP", "JPEG"]) as source:
                if (
                    not 1 <= source.width * source.height <= _MAX_REFERENCE_PIXELS
                    or getattr(source, "n_frames", 1) != 1
                ):
                    raise ValueError("gacha_chroma_reference_invalid")
                source.thumbnail((_REFERENCE_SAMPLE_EDGE, _REFERENCE_SAMPLE_EDGE))
                return cast("tuple[_RGBA, ...]", source.convert("RGBA").get_flattened_data())
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ):
        raise ValueError("gacha_chroma_reference_invalid") from None


def _key_contrast(color: _RGB, *, green_key: bool) -> int:
    red, green, blue = color
    return green - max(red, blue) if green_key else min(red, blue) - green


def _distance(left: _RGB, right: _RGB) -> int:
    return max(abs(a - b) for a, b in zip(left, right, strict=True))


def _collision(color: _RGB, *, green_key: bool) -> int:
    key = _KEY_RGB[_GREEN if green_key else _MAGENTA]
    return (
        max(0, 160 - _distance(color, key))
        if _key_contrast(color, green_key=green_key) >= 25
        else 0
    )


def key_gacha_frame(frame: Image.Image, key_color: str) -> Image.Image:
    """Extract one opaque actor and its feathered edges from a measured flat key."""
    if (
        key_color not in (_GREEN, _MAGENTA)
        or not 1 <= frame.width * frame.height <= _MAX_FRAME_PIXELS
    ):
        raise ValueError("gacha_chroma_frame_invalid")
    try:
        rgba = frame.convert("RGBA")
    except OSError, ValueError:
        raise ValueError("gacha_chroma_frame_invalid") from None
    if rgba.getchannel("A").getextrema() != (255, 255):
        raise ValueError("gacha_chroma_frame_invalid")
    rgb = rgba.convert("RGB")
    green_key = key_color == _GREEN
    screen, tolerance = _measure_screen(rgb, key_color)
    screen_contrast = _key_contrast(screen, green_key=green_key)
    pixels = cast("tuple[_RGB, ...]", rgb.get_flattened_data())
    colors = set(pixels)
    clear = {
        color
        for color in colors
        if _distance(color, screen) <= tolerance and _key_contrast(color, green_key=green_key) >= 44
    }
    hard = Image.frombytes("L", rgb.size, bytes(255 if color in clear else 0 for color in pixels))
    if not len(pixels) * 0.20 <= hard.histogram()[255] <= len(pixels) * 0.98:
        raise ValueError("gacha_chroma_subject_or_background_missing")

    # Green spill in a narrow hair gap can be well away from any perfectly flat pixel.
    # Expand through connected key-like colors, leaving isolated costume accents intact.
    edge_colors = {color for color in colors if _key_contrast(color, green_key=green_key) > 7}
    eligible = Image.frombytes(
        "L", rgb.size, bytes(255 if color in edge_colors else 0 for color in pixels)
    )
    reached = hard
    for _ in range(_EDGE_REACH_PIXELS):
        expanded = ImageChops.darker(reached.filter(ImageFilter.MaxFilter(3)), eligible)
        if ImageChops.difference(reached, expanded).getbbox() is None:
            break
        reached = expanded
    # A short fringe covers isolated spill pixels across a dark ink outline.
    fringe = reached.filter(ImageFilter.MaxFilter(9))
    # A closed hair or arm gap may contain no perfectly flat seed. Despill narrow
    # traces of the key hue there, while leaving broad costume details intact.
    strong_spill = {
        color
        for color in edge_colors
        if _key_contrast(color, green_key=green_key) >= max(18, max(color) // 7)
    }
    strong = Image.frombytes(
        "L", rgb.size, bytes(255 if color in strong_spill else 0 for color in pixels)
    )
    solid_colors = strong.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    edges = ImageChops.lighter(fringe, ImageChops.subtract(strong, solid_colors)).tobytes()
    despill: dict[_RGB, _RGBA] = {}
    output = bytearray()
    for color, near_screen in zip(pixels, edges, strict=True):
        if color in clear:
            output.extend((0, 0, 0, 0))
        elif near_screen:
            if color not in despill:
                despill[color] = _soft_pixel(color, screen, screen_contrast, green_key=green_key)
            output.extend(despill[color])
        else:
            output.extend((*color, 255))
    cutout = Image.frombytes("RGBA", rgb.size, bytes(output))
    alpha = cutout.getchannel("A")
    # Soften abrupt indexed-GIF edges toward the actor, without adding screen pixels.
    softened = Image.blend(alpha, alpha.filter(ImageFilter.GaussianBlur(0.6)), 0.45)
    cutout.putalpha(ImageChops.darker(alpha, softened))
    return cutout


def _measure_screen(frame: Image.Image, key_color: str) -> tuple[_RGB, int]:
    width, height = frame.size
    inset_x, inset_y = max(1, width // 40), max(1, height // 26)
    border = Counter[_RGB]()
    for box in (
        (0, 0, inset_x, height),
        (width - inset_x, 0, width, height),
        (0, 0, width, inset_y),
        (0, height - inset_y, width, height),
    ):
        border.update(cast("tuple[_RGB, ...]", frame.crop(box).get_flattened_data()))
    candidates = [
        (frequency, color)
        for color, frequency in border.items()
        if _distance(color, _KEY_RGB[key_color]) <= 94
        and _key_contrast(color, green_key=key_color == _GREEN) >= 80
    ]
    if not candidates:
        raise ValueError("gacha_chroma_screen_not_found")
    _frequency, screen = max(candidates)
    tolerance = max(24, _key_contrast(screen, green_key=key_color == _GREEN) // 5)
    matching = sum(
        count for color, count in border.items() if _distance(color, screen) <= tolerance
    )
    if matching < border.total() * 0.65:
        raise ValueError("gacha_chroma_screen_not_flat")
    return screen, tolerance


def _clamp_channel(value: float) -> int:
    return max(0, min(255, round(value)))


def _soft_pixel(color: _RGB, screen: _RGB, screen_contrast: int, *, green_key: bool) -> _RGBA:
    gap = _key_contrast(color, green_key=green_key) - 7
    if gap <= 0:
        return (*color, 255)
    # Undo the estimated flat-screen contribution before blending on the new plate.
    coverage = max(0.23, 1 - gap / screen_contrast)
    red, green, blue = (
        _clamp_channel((value - key * (1 - coverage)) / coverage)
        for value, key in zip(color, screen, strict=True)
    )
    spill = max(0, _key_contrast((red, green, blue), green_key=green_key) - 7)
    alpha = _clamp_channel(255 * max(0.0, 1 - gap / max(35, screen_contrast * 0.47)) ** 0.85)
    if green_key:
        return red, green - spill, blue, alpha
    return red - spill, green, blue - spill, alpha
