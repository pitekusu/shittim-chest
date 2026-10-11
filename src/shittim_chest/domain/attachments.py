"""Immutable image metadata independent of storage and provider SDKs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

MAX_DEBATE_IMAGES = 3
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def validate_attachment_id(value: str) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid image identifier") from error


@dataclass(frozen=True, slots=True)
class DebateImage:
    """A validated original and its immutable provider-ready derivative."""

    attachment_id: str
    content_type: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    asset_key: str = field(repr=False)
    vision_asset_key: str = field(repr=False)
    asset_version_id: str | None = field(default=None, repr=False)
    vision_asset_version_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        validate_attachment_id(self.attachment_id)
        if self.content_type not in IMAGE_CONTENT_TYPES:
            raise ValueError("unsupported image format")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or not 1 <= self.size_bytes <= MAX_IMAGE_BYTES
        ):
            raise ValueError("invalid image size")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256):
            raise ValueError("invalid image checksum")
        if (
            not isinstance(self.width, int)
            or not isinstance(self.height, int)
            or isinstance(self.width, bool)
            or isinstance(self.height, bool)
            or min(self.width, self.height) < 1
            or self.width * self.height > MAX_IMAGE_PIXELS
        ):
            raise ValueError("invalid image dimensions")
        extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[
            self.content_type
        ]
        prefix = f"debate-images/{self.attachment_id}/"
        if self.asset_key != f"{prefix}original.{extension}" or (
            self.vision_asset_key != f"{prefix}vision.webp"
        ):
            raise ValueError("invalid image asset reference")
        for version in (self.asset_version_id, self.vision_asset_version_id):
            if version is not None and (not isinstance(version, str) or not version.strip()):
                raise ValueError("invalid image object version")


@dataclass(frozen=True, slots=True)
class ImageInput:
    """Validated image bytes in memory; never serialize them into records or logs."""

    attachment_id: str
    content_type: str
    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        validate_attachment_id(self.attachment_id)
        if self.content_type not in IMAGE_CONTENT_TYPES or not isinstance(self.content, bytes):
            raise ValueError("invalid image input")
        if not 1 <= len(self.content) <= MAX_IMAGE_BYTES:
            raise ValueError("invalid image input size")


def validate_images(images: tuple[DebateImage | ImageInput, ...]) -> None:
    if len(images) > MAX_DEBATE_IMAGES or len({image.attachment_id for image in images}) != len(
        images
    ):
        raise ValueError("invalid image collection")
