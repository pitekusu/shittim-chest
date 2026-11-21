"""Choose and remove a flat color backdrop from student recruitment art."""

from __future__ import annotations

import io
import logging
import warnings
from collections import Counter
from functools import partial
from itertools import groupby
from typing import Final, cast

from PIL import Image, ImageChops, ImageFilter

from shittim_chest.adapters.assets.images import MAX_IMAGE_BYTES
from shittim_chest.application.student_gacha_models import GACHA_GIF_SIZE

_GREEN: Final = "#00FF00"
_MAGENTA: Final = "#FF00FF"
_KEY_RGB: Final = {_GREEN: (0, 255, 0), _MAGENTA: (255, 0, 255)}
_MAX_REFERENCE_PIXELS: Final = 12_000_000
_REFERENCE_SAMPLE_EDGE: Final = 256
_MAX_FRAME_PIXELS: Final = 1024 * 480
_EDGE_REACH_PIXELS: Final = 24
_MIN_BACKGROUND_FRACTION: Final = 0.20
_MAX_BACKGROUND_FRACTION: Final = 0.98
_MIN_BORDER_COVERAGE: Final = 0.65
_FRAME_DIVIDER: Final = "gacha_provider_frame_divider"
_DARK_PIXEL_MASK: Final = [255] * 17 + [0] * 239
_MIN_DIVIDER_COVERAGE: Final = 0.90
_LOGGER = logging.getLogger(__name__)

type _RGB = tuple[int, int, int]
type _RGBA = tuple[int, int, int, int]


class GachaChromaFailure(ValueError):
    """A frame could not be keyed without risking its foreground or background."""


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


def key_gacha_frames(frames: list[Image.Image], key_color: str) -> list[Image.Image]:
    """Key complete frames and preserve the loop timing when artwork is rejected."""
    cutouts: dict[int, Image.Image] = {}
    rejected: Counter[str] = Counter()
    for index, frame in enumerate(frames):
        try:
            cutouts[index] = key_gacha_frame(frame, key_color)
        except GachaChromaFailure as error:
            rejected[str(error)] += 1

    if not cutouts:
        reason = rejected.most_common(1)[0][0] if rejected else "gacha_chroma_no_usable_frames"
        raise GachaChromaFailure(reason)
    if not rejected:
        return list(cutouts.values())

    _LOGGER.warning(
        "student_gacha_chroma_recovered rejected_frames=%d reasons=%s",
        rejected.total(),
        ",".join(f"{reason}:{count}" for reason, count in sorted(rejected.items())),
    )
    if rejected[_FRAME_DIVIDER]:
        return _replay_usable_segment(cutouts, len(frames))
    restored = []
    for index in range(len(frames)):
        if index in cutouts:
            restored.append(cutouts[index])
        else:
            nearest = min(cutouts, key=partial(_loop_distance, index, len(frames)))
            restored.append(cutouts[nearest])
    return restored


def _replay_usable_segment(cutouts: dict[int, Image.Image], frame_count: int) -> list[Image.Image]:
    # A sheet divider can separate fragments of different poses. Replay a continuous
    # run of intact frames forward and back so the recovered loop closes on its start.
    indices = (index % frame_count for index in range(2 * frame_count))
    runs = (list(run) for usable, run in groupby(indices, key=cutouts.__contains__) if usable)
    segment = max(runs, key=len)
    half_loop = frame_count // 2
    replay = []
    for index in range(frame_count):
        phase = min(index, frame_count - index) / half_loop
        replay.append(cutouts[segment[round(phase * (len(segment) - 1))]])
    return replay


def _loop_distance(index: int, frame_count: int, candidate: int) -> tuple[int, int]:
    steps_back = (index - candidate) % frame_count
    return min(steps_back, frame_count - steps_back), steps_back


def key_gacha_frame(frame: Image.Image, key_color: str) -> Image.Image:
    """Keep a valid GIF cutout or extract the actor from a measured flat key."""
    area = frame.width * frame.height
    if key_color not in (_GREEN, _MAGENTA) or not 1 <= area <= _MAX_FRAME_PIXELS:
        raise GachaChromaFailure("gacha_chroma_frame_invalid")
    try:
        rgba = frame.convert("RGBA")
    except OSError, ValueError:
        raise GachaChromaFailure("gacha_chroma_frame_invalid") from None
    rgb = rgba.convert("RGB")
    source_alpha = rgba.getchannel("A")
    if _has_frame_divider(rgb, source_alpha):
        raise GachaChromaFailure(_FRAME_DIVIDER)
    source_clear = None
    residual = None
    if source_alpha.getextrema() != (255, 255):
        histogram = source_alpha.histogram()
        clear_count = histogram[0]
        if clear_count + histogram[255] != area:
            raise GachaChromaFailure("gacha_chroma_alpha_invalid")
        if clear_count > area * _MAX_BACKGROUND_FRACTION:
            raise GachaChromaFailure("gacha_chroma_subject_or_background_missing")
        # Count existing transparency as known background without using its hidden RGB.
        source_clear = ImageChops.invert(source_alpha)
        if clear_count >= area * _MIN_BACKGROUND_FRACTION and _has_clear_border(source_alpha):
            residual = _measure_opaque_key_edge(rgb, key_color, source_clear)
            if residual is None:
                cutout = Image.new("RGBA", rgba.size)
                cutout.paste(rgba, mask=source_alpha)
                return cutout

    screen, tolerance = residual or _measure_screen(rgb, key_color, source_clear)
    green_key = key_color == _GREEN
    if source_clear is not None:
        rgb.paste((0, 0, 0), mask=source_clear)
    screen_contrast = _key_contrast(screen, green_key=green_key)
    pixels = cast("tuple[_RGB, ...]", rgb.get_flattened_data())
    colors = set(pixels)
    clear = {
        color
        for color in colors
        if _distance(color, screen) <= tolerance and _key_contrast(color, green_key=green_key) >= 44
    }
    hard = Image.frombytes("L", rgb.size, bytes(255 if color in clear else 0 for color in pixels))
    if source_clear is not None:
        hard = ImageChops.lighter(hard, source_clear)
    clear_count = hard.histogram()[255]
    if not area * _MIN_BACKGROUND_FRACTION <= clear_count <= area * _MAX_BACKGROUND_FRACTION:
        raise GachaChromaFailure("gacha_chroma_subject_or_background_missing")

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
    for color, near_screen, opacity in zip(pixels, edges, source_alpha.tobytes(), strict=True):
        if not opacity or color in clear:
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


def _has_frame_divider(rgb: Image.Image, alpha: Image.Image) -> bool:
    # A near-black opaque row spanning the canvas survives chroma keying. Ignore
    # the edge padding that is cropped off when the actor is fitted onto the card.
    red, green, blue = rgb.split()
    brightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    dark = ImageChops.darker(brightest.point(_DARK_PIXEL_MASK), alpha)
    cropped_rows = max(0, (rgb.height - GACHA_GIF_SIZE[1]) // 2)
    row_coverage = dark.reduce((rgb.width, 1)).tobytes()
    visible_rows = row_coverage[cropped_rows : rgb.height - cropped_rows]
    return any(coverage >= 255 * _MIN_DIVIDER_COVERAGE for coverage in visible_rows)


def _border_boxes(size: tuple[int, int]) -> tuple[tuple[int, int, int, int], ...]:
    width, height = size
    inset_x, inset_y = max(1, width // 40), max(1, height // 26)
    return (
        (0, 0, inset_x, height),
        (width - inset_x, 0, width, height),
        (0, 0, width, inset_y),
        (0, height - inset_y, width, height),
    )


def _has_clear_border(alpha: Image.Image) -> bool:
    clear = total = 0
    for box in _border_boxes(alpha.size):
        border = alpha.crop(box)
        clear += border.histogram()[0]
        total += border.width * border.height
    return clear >= total * _MIN_BORDER_COVERAGE


def _measure_screen(
    frame: Image.Image, key_color: str, source_clear: Image.Image | None
) -> tuple[_RGB, int]:
    border = Counter[_RGB]()
    transparent = 0
    for box in _border_boxes(frame.size):
        pixels = cast("tuple[_RGB, ...]", frame.crop(box).get_flattened_data())
        if source_clear is None:
            border.update(pixels)
        else:
            mask = source_clear.crop(box).tobytes()
            transparent += mask.count(255)
            border.update(color for color, clear in zip(pixels, mask, strict=True) if not clear)
    measurement = _screen_candidate(border, key_color)
    if measurement is None:
        raise GachaChromaFailure("gacha_chroma_screen_not_found")
    screen, tolerance = measurement
    matching = _matching_screen_pixels(border, screen, tolerance)
    if matching + transparent < (border.total() + transparent) * _MIN_BORDER_COVERAGE:
        raise GachaChromaFailure("gacha_chroma_screen_not_flat")
    return measurement


def _measure_opaque_key_edge(
    frame: Image.Image, key_color: str, source_clear: Image.Image
) -> tuple[_RGB, int] | None:
    # A flat opaque strip is evidence of a remaining screen even when the other edges
    # are transparent. Isolated key-colored pixels may belong to the costume.
    for box in _border_boxes(frame.size):
        mask = source_clear.crop(box).tobytes()
        if len(mask) - mask.count(255) < len(mask) * _MIN_BORDER_COVERAGE:
            continue
        pixels = cast("tuple[_RGB, ...]", frame.crop(box).get_flattened_data())
        border = Counter(color for color, clear in zip(pixels, mask, strict=True) if not clear)
        measurement = _screen_candidate(border, key_color)
        if (
            measurement is not None
            and _matching_screen_pixels(border, *measurement) >= len(mask) * _MIN_BORDER_COVERAGE
        ):
            return measurement
    return None


def _screen_candidate(border: Counter[_RGB], key_color: str) -> tuple[_RGB, int] | None:
    candidate = max(
        (
            (frequency, color)
            for color, frequency in border.items()
            if _distance(color, _KEY_RGB[key_color]) <= 94
            and _key_contrast(color, green_key=key_color == _GREEN) >= 80
        ),
        default=None,
    )
    if candidate is None:
        return None
    _frequency, screen = candidate
    tolerance = max(24, _key_contrast(screen, green_key=key_color == _GREEN) // 5)
    return screen, tolerance


def _matching_screen_pixels(border: Counter[_RGB], screen: _RGB, tolerance: int) -> int:
    return sum(count for color, count in border.items() if _distance(color, screen) <= tolerance)


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
