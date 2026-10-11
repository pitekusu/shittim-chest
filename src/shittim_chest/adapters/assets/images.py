"""Bounded image validation and private, immutable S3 inputs.

Small put_object operations are intentional: conditional writes and exact version
IDs fence replay, unlike managed multipart transfers. Source names and URLs never
become object keys, metadata, exception text, or logs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx2
from botocore.exceptions import ClientError
from PIL import Image, ImageOps, UnidentifiedImageError

from shittim_chest.domain.attachments import DebateImage, ImageInput, validate_attachment_id

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

    from shittim_chest.application.models import PendingDiscordImage

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
VISION_MAX_EDGE = 2048
_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


class DebateImageFailure(ValueError):
    def __init__(self, code: str = "ATTACHMENT_INVALID") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    content_type: str
    extension: str
    width: int
    height: int
    sha256: str
    original: bytes = field(repr=False)
    vision: bytes = field(repr=False)


def validate_image(
    content: bytes, *, content_type: str, sha256: str | None = None
) -> ValidatedImage:
    if not 1 <= len(content) <= MAX_IMAGE_BYTES:
        raise DebateImageFailure()
    digest = hashlib.sha256(content).hexdigest()
    if sha256 is not None and digest != sha256:
        raise DebateImageFailure()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                actual = _FORMATS.get(image.format or "")
                if (
                    actual is None
                    or actual[0] != content_type
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise DebateImageFailure()
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > MAX_IMAGE_PIXELS
                ):
                    raise DebateImageFailure()
                image.load()
                oriented = ImageOps.exif_transpose(image)
                width, height = image.size
                mode = (
                    "RGBA" if "A" in oriented.getbands() or "transparency" in image.info else "RGB"
                )
                oriented.thumbnail((VISION_MAX_EDGE, VISION_MAX_EDGE), Image.Resampling.LANCZOS)
                normalized = oriented.convert(mode)
                output = io.BytesIO()
                normalized.save(output, format="WEBP", quality=90, method=4)
                vision = output.getvalue()
                if not 1 <= len(vision) <= MAX_IMAGE_BYTES:
                    raise DebateImageFailure()
                return ValidatedImage(actual[0], actual[1], width, height, digest, content, vision)
    except DebateImageFailure:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ):
        raise DebateImageFailure() from None


class S3DebateImageStore:
    def __init__(
        self, client: S3Client, *, media_bucket_name: str, upload_bucket_name: str | None = None
    ) -> None:
        if not media_bucket_name:
            raise ValueError("debate_image_configuration_invalid")
        self._client = client
        self._media_bucket = media_bucket_name
        self._upload_bucket = upload_bucket_name
        # Core shares one process across debates; decode only one 40M-pixel image
        # at a time. Lambda uses the synchronous upload path in its own process.
        self._normalization = asyncio.Semaphore(1)

    def upload_ticket(
        self, *, key: str, content_type: str, sha256: str, size_bytes: int, expires_in: int = 600
    ) -> dict[str, Any]:
        if self._upload_bucket is None or not key.startswith("debate-uploads/") or ".." in key:
            raise DebateImageFailure("ATTACHMENT_UNAVAILABLE")
        checksum = base64.b64encode(bytes.fromhex(sha256)).decode()
        fields = {"key": key, "Content-Type": content_type, "x-amz-checksum-sha256": checksum}
        return self._client.generate_presigned_post(
            Bucket=self._upload_bucket,
            Key=key,
            Fields=fields,
            Conditions=[
                *({name: value} for name, value in fields.items()),
                ["content-length-range", size_bytes, size_bytes],
            ],
            ExpiresIn=expires_in,
        )

    def import_upload(
        self, *, attachment_id: str, key: str, content_type: str, size_bytes: int, sha256: str
    ) -> DebateImage:
        if self._upload_bucket is None or not key.startswith("debate-uploads/") or ".." in key:
            raise DebateImageFailure("ATTACHMENT_UNAVAILABLE")
        content = self._load(self._upload_bucket, key, maximum=MAX_IMAGE_BYTES)
        if len(content) != size_bytes:
            raise DebateImageFailure()
        return self.persist(
            attachment_id, validate_image(content, content_type=content_type, sha256=sha256)
        )

    def persist(self, attachment_id: str, image: ValidatedImage) -> DebateImage:
        validate_attachment_id(attachment_id)
        prefix = f"debate-images/{attachment_id}/"
        original_key, vision_key = prefix + "original." + image.extension, prefix + "vision.webp"
        original_version = self._put(
            original_key, image.original, image.content_type, source_sha256=image.sha256
        )
        vision_version = self._put(
            vision_key, image.vision, "image/webp", source_sha256=image.sha256
        )
        return DebateImage(
            attachment_id=attachment_id,
            content_type=image.content_type,
            size_bytes=len(image.original),
            sha256=image.sha256,
            width=image.width,
            height=image.height,
            asset_key=original_key,
            vision_asset_key=vision_key,
            asset_version_id=original_version,
            vision_asset_version_id=vision_version,
        )

    def _put(
        self, key: str, content: bytes, content_type: str, *, source_sha256: str
    ) -> str | None:
        checksum = base64.b64encode(hashlib.sha256(content).digest()).decode()
        try:
            response = self._client.put_object(
                Bucket=self._media_bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                ChecksumSHA256=checksum,
                IfNoneMatch="*",
                CacheControl="private, no-store",
                Metadata={"source-sha256": source_sha256},
            )
            return response.get("VersionId")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "PreconditionFailed",
                "412",
                "ConditionalRequestConflict",
                "409",
            }:
                raise
            existing = self._client.head_object(
                Bucket=self._media_bucket, Key=key, ChecksumMode="ENABLED"
            )
            if (
                existing.get("ChecksumSHA256") != checksum
                or existing.get("ContentLength") != len(content)
                or existing.get("ContentType") != content_type
                or existing.get("Metadata", {}).get("source-sha256") != source_sha256
            ):
                raise DebateImageFailure("ATTACHMENT_CONFLICT") from None
            return existing.get("VersionId")

    def _load(self, bucket: str, key: str, *, maximum: int, version: str | None = None) -> bytes:
        params: dict[str, Any] = {"Bucket": bucket, "Key": key, "ChecksumMode": "ENABLED"}
        if version:
            params["VersionId"] = version
        response = self._client.get_object(**params)
        body = response["Body"]
        try:
            content = body.read(maximum + 1)
        finally:
            body.close()
        if not 1 <= len(content) <= maximum or response.get("ContentLength") != len(content):
            raise DebateImageFailure()
        return content

    async def load_original(self, image: DebateImage) -> bytes:
        content = await asyncio.to_thread(
            self._load,
            self._media_bucket,
            image.asset_key,
            maximum=MAX_IMAGE_BYTES,
            version=image.asset_version_id,
        )
        if len(content) != image.size_bytes or hashlib.sha256(content).hexdigest() != image.sha256:
            raise DebateImageFailure()
        return content

    async def load_vision_images(self, images: tuple[DebateImage, ...]) -> tuple[ImageInput, ...]:
        result = []
        for image in images:
            content = await asyncio.to_thread(
                self._load,
                self._media_bucket,
                image.vision_asset_key,
                maximum=MAX_IMAGE_BYTES,
                version=image.vision_asset_version_id,
            )
            result.append(
                ImageInput(
                    attachment_id=image.attachment_id, content_type="image/webp", content=content
                )
            )
        return tuple(result)

    async def import_discord_attachment(self, pending: PendingDiscordImage) -> DebateImage:
        if pending.size_bytes > MAX_IMAGE_BYTES:
            raise DebateImageFailure()
        original_extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[
            pending.content_type
        ]
        try:
            cached = await asyncio.to_thread(
                self._load,
                self._media_bucket,
                f"debate-images/{pending.attachment_id}/original.{original_extension}",
                maximum=MAX_IMAGE_BYTES,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404", "NotFound"}:
                raise DebateImageFailure("ATTACHMENT_UNAVAILABLE") from None
        else:
            if len(cached) != pending.size_bytes:
                raise DebateImageFailure("ATTACHMENT_CONFLICT")
            return await self._normalize_discord(pending, cached)
        if pending.url is None:
            raise DebateImageFailure()
        parsed = urlsplit(pending.url)
        path = parsed.path.split("/")
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.fragment
            or len(path) != 5
            or path[1] not in {"attachments", "ephemeral-attachments"}
            or path[3] != pending.discord_attachment_id
            or not path[2].isdigit()
        ):
            raise DebateImageFailure()
        try:
            async with (
                asyncio.timeout(30),
                httpx2.AsyncClient(follow_redirects=False, timeout=20) as client,
                client.stream("GET", pending.url) as response,
            ):
                if response.status_code != 200:
                    raise DebateImageFailure("ATTACHMENT_UNAVAILABLE")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_IMAGE_BYTES:
                        raise DebateImageFailure()
                    chunks.extend(chunk)
            if len(chunks) != pending.size_bytes:
                raise DebateImageFailure()
            return await self._normalize_discord(pending, bytes(chunks))
        except httpx2.HTTPError, TimeoutError:
            raise DebateImageFailure("ATTACHMENT_UNAVAILABLE") from None

    async def _normalize_discord(self, pending: PendingDiscordImage, content: bytes) -> DebateImage:
        async with self._normalization:
            validated = await asyncio.to_thread(
                validate_image, content, content_type=pending.content_type
            )
            return await asyncio.to_thread(self.persist, pending.attachment_id, validated)

    def image_url(self, image: DebateImage) -> str:
        params = {
            "Bucket": self._media_bucket,
            "Key": image.asset_key,
            "ResponseContentType": image.content_type,
            "ResponseCacheControl": "private, no-store",
        }
        if image.asset_version_id:
            params["VersionId"] = image.asset_version_id
        return self._client.generate_presigned_url("get_object", Params=params, ExpiresIn=300)

    def delete_image(self, image: DebateImage) -> None:
        for key, version in (
            (image.asset_key, image.asset_version_id),
            (image.vision_asset_key, image.vision_asset_version_id),
        ):
            params: dict[str, Any] = {"Bucket": self._media_bucket, "Key": key}
            if version:
                params["VersionId"] = version
            self._client.delete_object(**params)

    def delete_orphan(self, attachment_id: str, content_type: str, sha256: str) -> None:
        validate_attachment_id(attachment_id)
        # Global UUID ownership plus the expired manifest fence prevent this prefix
        # from ever referring to another request or an accepted image.
        extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
        for key in (
            f"debate-images/{attachment_id}/original.{extension}",
            f"debate-images/{attachment_id}/vision.webp",
        ):
            try:
                response = self._client.head_object(Bucket=self._media_bucket, Key=key)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                    continue
                raise
            if response.get("Metadata", {}).get("source-sha256") != sha256:
                raise DebateImageFailure("ATTACHMENT_CONFLICT")
            version = response.get("VersionId")
            if not isinstance(version, str) or not version or version == "null":
                raise DebateImageFailure("ATTACHMENT_UNAVAILABLE")
            self._client.delete_object(Bucket=self._media_bucket, Key=key, VersionId=version)
