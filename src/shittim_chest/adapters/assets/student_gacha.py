"""Private student recruitment assets and immutable, versioned result images."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import warnings
from struct import error as StructError
from typing import TYPE_CHECKING, Final

from botocore.exceptions import BotoCoreError, ClientError, FlexibleChecksumError
from PIL import Image, ImageSequence

from shittim_chest.adapters.assets.images import DebateImageFailure, validate_image
from shittim_chest.adapters.assets.student_gacha_costumes import load_wiki_costume
from shittim_chest.adapters.assets.student_gacha_schools import (
    SCHOOL_CATALOG_MAX_BYTES,
    school_branding,
)
from shittim_chest.application.student_gacha_models import (
    GACHA_GIF_FRAME_DURATIONS,
    STUDENT_GACHA_RARITIES,
    GachaGenerationFailure,
    GachaImageFormat,
    StoredGachaImage,
    Student,
    student_gacha_image_key,
)
from shittim_chest.domain import ParticipantSlot

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import GetObjectOutputTypeDef, HeadObjectOutputTypeDef

    from shittim_chest.application.student_gacha_models import SchoolBranding

STUDENT_GACHA_ASSET_PREFIX: Final = "student-gacha/assets/"
ROSTER_KEY: Final = "student-gacha/roster/blue_archive_characters_with_npcs.json"
LOADING_GIF_KEY: Final = STUDENT_GACHA_ASSET_PREFIX + "show.gif"
FORMAT_KEY: Final = STUDENT_GACHA_ASSET_PREFIX + "format.jpg"
SCHOOL_LOGOS_KEY: Final = STUDENT_GACHA_ASSET_PREFIX + "blue_archive_school_logos.zip"
# Same private objects already used by Records memorial and Momotalk generation.
PARTICIPANT_REFERENCE_KEYS: dict[ParticipantSlot, str] = {
    ParticipantSlot.PARTICIPANT_A: "participants/participant-a/memorial-reference.png",
    ParticipantSlot.PARTICIPANT_B: "participants/participant-b/memorial-reference.webp",
    ParticipantSlot.PARTICIPANT_C: "participants/participant-c/memorial-reference.webp",
}
_SPECIAL_COSTUME_PARTICIPANTS: Final = {
    ("アロナ", "シッテムの箱"): ParticipantSlot.PARTICIPANT_A,
    ("プラナ", "シッテムの箱"): ParticipantSlot.PARTICIPANT_B,
    ("安倍晋三", "自民党"): ParticipantSlot.PARTICIPANT_C,
}
_ASSET_DIGESTS: Final = {
    LOADING_GIF_KEY: "a902598c659185353850dbe9e26c18fa0dd57d9f5d6288d2673b3c1e7e47ad63",
    FORMAT_KEY: "4d921dc67519a8b826a7ef6cf4853acaffca6db69c81e737816378f2ea9e80e0",
    SCHOOL_LOGOS_KEY: "c4090aa1613efab81b3255b0ee74db79f8ff240a1cd5cbec6ab51d453e4a23e8",
}
_MAX_REFERENCE_BYTES = 10 * 1024 * 1024
_ROSTER_MAX_BYTES = 256 * 1024
_ROSTER_MAX_ROWS = 2_000
RESULT_MAX_BYTES: Final = 8 * 1024 * 1024
RESULT_SIZE: Final = GachaImageFormat.PNG.size
_MAX_PIXELS = 12_000_000


class StudentGachaMedia:
    """Read only the approved inputs and conditionally create one object per draw."""

    def __init__(self, client: S3Client, *, bucket_name: str) -> None:
        if not bucket_name:
            raise ValueError("student_gacha_media_configuration_invalid")
        self._client = client
        self._bucket = bucket_name

    async def roster(self) -> tuple[Student, ...]:
        return await asyncio.to_thread(self._roster)

    async def loading_gif(self) -> bytes:
        # The pinned hash validates the animated, indefinitely looping source byte for byte.
        return await asyncio.to_thread(self._read_asset, LOADING_GIF_KEY, maximum=512 * 1024)

    async def references(self, performer: ParticipantSlot) -> tuple[bytes, bytes]:
        return await asyncio.to_thread(self._references, performer)

    async def costume_reference(self, student: Student) -> bytes:
        performer = _SPECIAL_COSTUME_PARTICIPANTS.get((student.name, student.school))
        if performer is not None and student.rarity in {None, 4}:
            return await asyncio.to_thread(self._special_costume_reference, performer)
        return await load_wiki_costume(student)

    async def school_branding(self, school: str) -> SchoolBranding:
        return await asyncio.to_thread(self._school_branding, school)

    async def save(self, draw_id: str, apng: bytes) -> StoredGachaImage:
        return await asyncio.to_thread(self._save, draw_id, apng)

    async def load(self, stored: StoredGachaImage) -> bytes:
        return await asyncio.to_thread(self._load, stored)

    async def find(self, draw_id: str) -> StoredGachaImage | None:
        return await asyncio.to_thread(self._find, draw_id)

    def _roster(self) -> tuple[Student, ...]:
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=ROSTER_KEY, ChecksumMode="ENABLED"
            )
            content = _read_body(response, maximum=_ROSTER_MAX_BYTES)
        except FlexibleChecksumError:
            raise GachaGenerationFailure("gacha_asset_checksum_mismatch") from None
        except BotoCoreError, ClientError:
            raise GachaGenerationFailure("gacha_media_unavailable") from None
        if response.get("ContentType") != "application/json" or response.get("VersionId") in {
            None,
            "",
            "null",
        }:
            raise GachaGenerationFailure("gacha_roster_invalid")
        checksum = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        if (
            response.get("ChecksumType") != "FULL_OBJECT"
            or response.get("ChecksumSHA256") != checksum
        ):
            raise GachaGenerationFailure("gacha_asset_checksum_mismatch")
        return _parse_roster(content)

    def _references(self, performer: ParticipantSlot) -> tuple[bytes, bytes]:
        layout = self._read_asset(FORMAT_KEY, maximum=512 * 1024)
        key = PARTICIPANT_REFERENCE_KEYS[performer]
        portrait = self._read(key, maximum=_MAX_REFERENCE_BYTES)
        expected_format = "PNG" if key.endswith(".png") else "WEBP"
        _require_image(portrait, expected_format=expected_format)
        return layout, portrait

    def _special_costume_reference(self, performer: ParticipantSlot) -> bytes:
        key = PARTICIPANT_REFERENCE_KEYS[performer]
        content = self._read(key, maximum=_MAX_REFERENCE_BYTES)
        content_type = "image/png" if key.endswith(".png") else "image/webp"
        try:
            return validate_image(content, content_type=content_type).vision
        except DebateImageFailure:
            raise GachaGenerationFailure("gacha_costume_reference_invalid") from None

    def _school_branding(self, school: str) -> SchoolBranding:
        catalog = self._read_asset(SCHOOL_LOGOS_KEY, maximum=SCHOOL_CATALOG_MAX_BYTES)
        return school_branding(catalog, school)

    def _read_asset(self, key: str, *, maximum: int) -> bytes:
        content = self._read(key, maximum=maximum)
        if hashlib.sha256(content).hexdigest() != _ASSET_DIGESTS[key]:
            raise GachaGenerationFailure("gacha_asset_checksum_mismatch")
        return content

    def _read(self, key: str, *, maximum: int) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return _read_body(response, maximum=maximum)
        except BotoCoreError, ClientError:
            raise GachaGenerationFailure("gacha_media_unavailable") from None

    def _save(self, draw_id: str, apng: bytes) -> StoredGachaImage:
        image_format = GachaImageFormat.APNG
        key = student_gacha_image_key(draw_id, image_format)
        validate_gacha_result(apng, image_format=image_format)
        if any(
            self._head_result(draw_id, legacy_format) is not None
            for legacy_format in GachaImageFormat
            if legacy_format is not image_format
        ):
            raise GachaGenerationFailure("gacha_result_conflict", ambiguous=True)
        digest = hashlib.sha256(apng).digest()
        checksum = base64.b64encode(digest).decode("ascii")
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=apng,
                IfNoneMatch="*",
                ContentType=image_format.content_type,
                CacheControl="private, no-store",
                ChecksumSHA256=checksum,
            )
        except BotoCoreError, ClientError:
            # The request may have committed before the SDK lost its response.
            current = self._find(draw_id)
            if current is None:
                raise GachaGenerationFailure(
                    "gacha_result_save_unconfirmed", ambiguous=True
                ) from None
            if current.key != key or current.sha256 != digest.hex():
                raise GachaGenerationFailure("gacha_result_conflict", ambiguous=True) from None
            return current
        try:
            return StoredGachaImage(key, response.get("VersionId", ""), digest.hex())
        except ValueError:
            raise GachaGenerationFailure("gacha_result_save_unconfirmed", ambiguous=True) from None

    def _load(self, stored: StoredGachaImage) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=stored.key, VersionId=stored.version_id
            )
            content = _read_body(response, maximum=RESULT_MAX_BYTES)
        except BotoCoreError, ClientError:
            raise GachaGenerationFailure("gacha_media_unavailable") from None
        if (
            response.get("VersionId") != stored.version_id
            or response.get("ContentType") != stored.image_format.content_type
            or hashlib.sha256(content).hexdigest() != stored.sha256
        ):
            raise GachaGenerationFailure("gacha_result_conflict")
        validate_gacha_result(content, image_format=stored.image_format)
        return content

    def _find(self, draw_id: str) -> StoredGachaImage | None:
        receipt = None
        for image_format in GachaImageFormat:
            current = self._head_result(draw_id, image_format)
            if current is not None:
                if receipt is not None:
                    raise GachaGenerationFailure("gacha_result_conflict", ambiguous=True)
                receipt = current
        return receipt

    def _head_result(self, draw_id: str, image_format: GachaImageFormat) -> StoredGachaImage | None:
        key = student_gacha_image_key(draw_id, image_format)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return None
            if status == 403 and self._missing_result(key):
                return None
            raise GachaGenerationFailure("gacha_result_lookup_failed", ambiguous=True) from None
        except BotoCoreError:
            raise GachaGenerationFailure("gacha_result_lookup_failed", ambiguous=True) from None
        return _receipt(key, head, content_type=image_format.content_type)

    def _missing_result(self, key: str) -> bool:
        # With restricted ListBucket permissions S3 HEAD can return 403 for an absent key.
        try:
            listing = self._client.list_objects_v2(Bucket=self._bucket, Prefix=key, MaxKeys=1)
        except BotoCoreError, ClientError:
            raise GachaGenerationFailure("gacha_result_lookup_failed", ambiguous=True) from None
        return not any(item.get("Key") == key for item in listing.get("Contents", []))


def _read_body(response: GetObjectOutputTypeDef, *, maximum: int) -> bytes:
    body = response["Body"]
    try:
        size = response.get("ContentLength", 0)
        if not 1 <= size <= maximum:
            raise GachaGenerationFailure("gacha_media_invalid")
        content = body.read(maximum + 1)
    finally:
        body.close()
    if len(content) != size:
        raise GachaGenerationFailure("gacha_media_invalid")
    return content


def _receipt(key: str, head: HeadObjectOutputTypeDef, *, content_type: str) -> StoredGachaImage:
    if (
        head.get("ContentType") != content_type
        or not 1 <= head.get("ContentLength", 0) <= RESULT_MAX_BYTES
    ):
        raise GachaGenerationFailure("gacha_result_invalid", ambiguous=True)
    try:
        digest = base64.b64decode(head.get("ChecksumSHA256", ""), validate=True).hex()
        return StoredGachaImage(key, head.get("VersionId", ""), digest)
    except binascii.Error, ValueError:
        raise GachaGenerationFailure("gacha_result_invalid", ambiguous=True) from None


def _parse_roster(content: bytes) -> tuple[Student, ...]:
    try:
        rows: object = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_roster_row)
        if not isinstance(rows, list) or not 1 <= len(rows) <= _ROSTER_MAX_ROWS:
            raise ValueError
        students: list[Student] = []
        names: set[str] = set()
        rarities: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"name", "school", "rarity"}:
                raise ValueError
            name, school, rarity = row["name"], row["school"], row["rarity"]
            if (
                not isinstance(name, str)
                or not isinstance(school, str)
                or name in names
                or type(rarity) is not int
                or rarity not in STUDENT_GACHA_RARITIES
                or (rarity == 0 and name in {"アロナ", "プラナ"})
            ):
                raise ValueError
            students.append(Student(name, school, rarity=rarity))
            names.add(name)
            rarities.add(rarity)
        if rarities != STUDENT_GACHA_RARITIES:
            raise ValueError
        return tuple(students)
    except UnicodeError, ValueError, RecursionError:
        raise GachaGenerationFailure("gacha_roster_invalid") from None


def _unique_roster_row(pairs: list[tuple[str, object]]) -> dict[str, object]:
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError
    return row


def validate_gacha_result(content: bytes, *, image_format: GachaImageFormat) -> None:
    if not 1 <= len(content) <= RESULT_MAX_BYTES:
        raise GachaGenerationFailure("gacha_result_invalid")
    if image_format is GachaImageFormat.GIF and not content.endswith(b";"):
        raise GachaGenerationFailure("gacha_media_invalid")
    _require_image(
        content,
        expected_format=image_format.pillow_format,
        expected_size=image_format.size,
        frame_durations=GACHA_GIF_FRAME_DURATIONS if image_format.is_animated else (),
    )


def encode_gacha_discord_webp(apng: bytes) -> bytes:
    """Preserve the saved frames in a Discord-compatible animated attachment."""
    validate_gacha_result(apng, image_format=GachaImageFormat.APNG)
    try:
        with Image.open(io.BytesIO(apng), formats=["PNG"]) as artwork, io.BytesIO() as output:
            first, *remaining = (frame.copy() for frame in ImageSequence.Iterator(artwork))
            first.save(
                output,
                format="WEBP",
                save_all=True,
                append_images=remaining,
                duration=GACHA_GIF_FRAME_DURATIONS,
                loop=0,
                lossless=True,
            )
            result = output.getvalue()
    except EOFError, OSError, ValueError:
        raise GachaGenerationFailure("gacha_discord_image_invalid") from None
    if not 1 <= len(result) <= RESULT_MAX_BYTES:
        raise GachaGenerationFailure("gacha_discord_image_invalid")
    _require_image(
        result,
        expected_format="WEBP",
        expected_size=GachaImageFormat.APNG.size,
        frame_durations=GACHA_GIF_FRAME_DURATIONS,
    )
    return result


def _require_image(
    content: bytes,
    *,
    expected_format: str,
    expected_size: tuple[int, int] | None = None,
    frame_durations: tuple[int, ...] = (),
) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=[expected_format]) as image:
                if (
                    image.format != expected_format
                    or image.width * image.height > _MAX_PIXELS
                    or (expected_size is not None and image.size != expected_size)
                ):
                    raise GachaGenerationFailure("gacha_media_invalid")
                if frame_durations:
                    _require_animation_frames(image, frame_durations)
                else:
                    if getattr(image, "n_frames", 1) != 1:
                        raise GachaGenerationFailure("gacha_media_invalid")
                    image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        EOFError,
        IndexError,
        OSError,
        StructError,
        SyntaxError,
        ValueError,
    ):
        raise GachaGenerationFailure("gacha_media_invalid") from None


def _require_animation_frames(image: Image.Image, durations: tuple[int, ...]) -> None:
    # Inspect at most one frame beyond the expected animation.
    if image.info.get("loop") != 0 or (
        image.format == "PNG"
        and (image.info.get("default_image") or image.mode not in {"RGB", "RGBA"})
    ):
        raise GachaGenerationFailure("gacha_media_invalid")
    size = image.size
    for index, duration in enumerate(durations):
        image.seek(index)
        if image.size != size:
            raise GachaGenerationFailure("gacha_media_invalid")
        image.load()
        if image.info.get("duration") != duration:
            raise GachaGenerationFailure("gacha_media_invalid")
    try:
        image.seek(len(durations))
    except EOFError:
        return
    raise GachaGenerationFailure("gacha_media_invalid")
