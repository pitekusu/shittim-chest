"""Shared values for the student recruitment feature."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from unicodedata import normalize

from shittim_chest.domain import ParticipantSlot

STUDENT_GACHA_IMAGE_PREFIX = "student-gacha/results/"
TEACHER_PLACEHOLDER = "{teacher}"
STUDENT_GACHA_RARITIES = frozenset(range(5))
GACHA_GIF_FRAME_DURATIONS = (170, 160, 170) * 4
GACHA_APNG_FRAME_DURATIONS = (150,) * 12
GACHA_LEGACY_APNG_FRAME_DURATIONS = (125,) * 12
GACHA_APNG_FRAME_COUNT = len(GACHA_APNG_FRAME_DURATIONS)
GACHA_GIF_SIZE = (1024, 473)
_DRAW_ID = re.compile(r"[0-9a-f]{32}")
_IMAGE_KEY = re.compile(re.escape(STUDENT_GACHA_IMAGE_PREFIX) + r"[0-9a-f]{32}\.(?:png|gif|apng)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class GachaExpression(StrEnum):
    UNHAPPY = "unhappy"
    NEUTRAL = "neutral"
    SMILE = "smile"


class GachaImageFormat(StrEnum):
    PNG = "png"
    GIF = "gif"
    APNG = "apng"

    @property
    def content_type(self) -> str:
        return f"image/{self.value}"

    @property
    def pillow_format(self) -> str:
        return "GIF" if self is GachaImageFormat.GIF else "PNG"

    @property
    def is_animated(self) -> bool:
        return self is not GachaImageFormat.PNG

    @property
    def frame_durations(self) -> tuple[int, ...]:
        if self is GachaImageFormat.GIF:
            return GACHA_GIF_FRAME_DURATIONS
        if self is GachaImageFormat.APNG:
            return GACHA_APNG_FRAME_DURATIONS
        return ()

    @property
    def size(self) -> tuple[int, int]:
        return GACHA_GIF_SIZE if self.is_animated else (1792, 828)


@dataclass(frozen=True, slots=True)
class Student:
    name: str
    school: str
    rarity: int | None = None

    def __post_init__(self) -> None:
        _require_printable(self.name, 60)
        _require_printable(self.school, 40)
        if self.rarity is not None and (
            type(self.rarity) is not int or self.rarity not in STUDENT_GACHA_RARITIES
        ):
            raise ValueError("invalid_student_gacha_rarity")


@dataclass(frozen=True, slots=True)
class GachaPresentation:
    """Prepared costume description and the text to put on the reveal card."""

    student: Student
    performer: ParticipantSlot
    appearance: str
    club: str
    role: str
    weapon: str
    greeting: tuple[str, str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.performer, ParticipantSlot) or len(self.greeting) != 3:
            raise ValueError("invalid_student_gacha_presentation")
        for text, limit in (
            (self.appearance, 600),
            (self.club, 18),
            (self.role, 24),
            (self.weapon, 16),
            *((line, 50) for line in self.greeting),
        ):
            _require_printable(text, limit)
        if sum(line.count(TEACHER_PLACEHOLDER) for line in self.greeting) != 1:
            raise ValueError("invalid_student_gacha_greeting")


@dataclass(frozen=True, slots=True)
class SchoolBranding:
    name: str
    logo: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_printable(self.name, 40)


@dataclass(frozen=True, slots=True)
class StoredGachaImage:
    key: str
    version_id: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not _IMAGE_KEY.fullmatch(self.key)
            or not 1 <= len(self.version_id) <= 1024
            or self.version_id == "null"
            or not self.version_id.isprintable()
            or not _SHA256.fullmatch(self.sha256)
        ):
            raise ValueError("invalid_student_gacha_image_receipt")

    @property
    def image_format(self) -> GachaImageFormat:
        return GachaImageFormat(self.key.rsplit(".", 1)[1])


class GachaGenerationFailure(RuntimeError):
    """A content-free failure, possibly after a paid image call was sent."""

    def __init__(self, code: str, *, ambiguous: bool = False) -> None:
        self.code = code
        self.ambiguous = ambiguous
        super().__init__(code)


def normalize_teacher_name(value: str) -> str:
    if not value.isprintable():
        raise ValueError("invalid_student_gacha_teacher_name")
    name = normalize("NFC", value.strip())
    _require_printable(name, 24)
    return name


def student_gacha_image_key(
    draw_id: str, image_format: GachaImageFormat = GachaImageFormat.PNG
) -> str:
    if not _DRAW_ID.fullmatch(draw_id):
        raise ValueError("invalid_student_gacha_draw_id")
    return f"{STUDENT_GACHA_IMAGE_PREFIX}{draw_id}.{image_format.value}"


def _require_printable(text: str, limit: int) -> None:
    if not 1 <= len(text) <= limit or text != text.strip() or not text.isprintable():
        raise ValueError("invalid_student_gacha_text")
