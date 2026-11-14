"""Project confirmed recruitment cards and their WebP thumbnails into Records."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from PIL import Image
from shittim_chest.adapters.assets.student_gacha import (
    _ASSET_DIGESTS,
    RESULT_MAX_BYTES,
    RESULT_SIZE,
    SCHOOL_LOGOS_KEY,
)
from shittim_chest.adapters.assets.student_gacha_schools import (
    SCHOOL_CATALOG_MAX_BYTES,
    school_branding,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application.student_gacha_models import (
    StoredGachaImage,
    Student,
    student_gacha_image_key,
)
from shittim_chest.domain import ParticipantSlot

from shittim_records.gallery_keys import (
    GALLERY_RECORD_TYPE,
    GALLERY_RESULT_KEY,
    GALLERY_RESULT_PREFIX,
    GALLERY_THUMBNAIL_PREFIX,
    GALLERY_THUMBNAIL_SIZE,
)
from shittim_records.projector import ProjectionResult

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import GetObjectOutputTypeDef
    from shittim_chest.adapters.dynamodb.serializer import DynamoItem

_OWNER_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_THUMBNAIL_MAX_BYTES = 512 * 1024
_BACKFILL_ROW_BUDGET = timedelta(minutes=2)


class GalleryProjector:
    """Create immutable gallery metadata before generating the optional thumbnail."""

    def __init__(
        self,
        dynamodb: DynamoDBClient,
        s3: S3Client,
        *,
        source_table_name: str,
        statistics_table_name: str,
        media_bucket_name: str,
        guild_id: str,
    ) -> None:
        if re.fullmatch(r"[0-9]{17,20}", guild_id) is None:
            raise ValueError("gallery_config_invalid")
        self._dynamodb = dynamodb
        self._s3 = s3
        self._source = source_table_name
        self._statistics = statistics_table_name
        self._bucket = media_bucket_name
        self._guild_id = guild_id
        self._source_pk = f"GACHA#GUILD#{guild_id}"
        self._gallery_pk = f"GACHA#GALLERY#{guild_id}"

    def project_result(self, result: DynamoItem, *, apply: bool = True) -> ProjectionResult:
        if result.get("PK") != self._source_pk:
            return ProjectionResult(created=False)

        sk = _text(result, "SK")
        posted_at, offer_id = _parse_result_key(sk)
        if result.get("posted_at") != posted_at or result.get("offer_id") != offer_id:
            raise ValueError("gallery_source_key_invalid")
        stored = _stored_image(_map(result, "stored"))
        if stored.key != student_gacha_image_key(offer_id):
            raise ValueError("gallery_source_image_invalid")
        raw_student = _map(result, "student")
        rarity = raw_student.get("rarity")
        if rarity is not None and type(rarity) is not int:
            raise ValueError("gallery_source_rarity_invalid")
        student = Student(_text(raw_student, "name"), _text(raw_student, "school"), rarity)
        metadata: DynamoItem = {
            "PK": self._gallery_pk,
            "SK": sk,
            "record_type": GALLERY_RECORD_TYPE,
            "schema_version": 1,
            "posted_at": posted_at,
            "offer_id": offer_id,
            "performer": ParticipantSlot(_text(result, "performer")).value,
            "student_name": student.name,
            "stored": {"key": stored.key, "version_id": stored.version_id, "sha256": stored.sha256},
        }
        if student.rarity is not None:
            metadata["rarity"] = student.rarity

        current = self._existing(metadata)
        if apply and current is not None and "thumbnail" in current:
            return ProjectionResult(created=False)
        original = self._read_original(stored)
        if current is None:
            metadata["school_name"] = school_branding(self._school_catalog, student.school).name
            metadata.update(self._collector(offer_id))
            if apply and not self._put_new(metadata):
                current = self._existing(metadata)
                if current is None:
                    raise ValueError("gallery_projection_missing")
                if "thumbnail" in current:
                    return ProjectionResult(created=False)

        webp = _render_thumbnail(original)
        if not apply:
            return ProjectionResult(created=False)
        thumbnail = self._store_thumbnail(offer_id, stored.sha256, webp)
        projection = metadata if current is None else current
        if not self._put_thumbnail(projection, thumbnail):
            confirmed = self._existing(metadata)
            if confirmed is None or confirmed.get("thumbnail") != thumbnail:
                raise ValueError("gallery_thumbnail_projection_conflict")
            return ProjectionResult(created=False)
        return ProjectionResult(created=True)

    def backfill_page(
        self, *, apply: bool, limit: int, cursor: str | None, deadline: datetime
    ) -> dict[str, object]:
        """Visit one bounded page, returning only counters and a continuation key."""
        if not 1 <= limit <= 50:
            raise ValueError("gallery_backfill_limit_invalid")
        parameters: dict[str, Any] = {
            "TableName": self._source,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": marshal_item(
                {":pk": self._source_pk, ":prefix": GALLERY_RESULT_PREFIX}
            ),
            "ConsistentRead": True,
            "Limit": limit,
        }
        if cursor is not None:
            _parse_result_key(cursor)
            parameters["ExclusiveStartKey"] = marshal_item({"PK": self._source_pk, "SK": cursor})
        response = self._dynamodb.query(**parameters)
        counts: dict[str, int] = {
            "candidates": 0,
            "validated": 0,
            "projected": 0,
            "skipped": 0,
            "failed": 0,
        }
        for raw_item in response.get("Items", []):
            if datetime.now(UTC) + _BACKFILL_ROW_BUDGET >= deadline:
                return {**counts, "complete": False, "next_cursor": cursor}
            counts["candidates"] += 1
            # Keep the last attempted key when a page cannot finish in one invocation.
            item = unmarshal_item(raw_item)
            if item.get("PK") != self._source_pk:
                raise ValueError("gallery_backfill_key_invalid")
            cursor = _text(item, "SK")
            _parse_result_key(cursor)
            try:
                result = self.project_result(item, apply=apply)
            except Exception:
                counts["failed"] += 1
                continue
            counts["validated"] += 1
            if apply:
                counts["projected" if result.created else "skipped"] += 1
        last_key = response.get("LastEvaluatedKey")
        key = unmarshal_item(last_key) if last_key else None
        if key is not None and key.get("PK") != self._source_pk:
            raise ValueError("gallery_backfill_key_invalid")
        next_cursor = _text(key, "SK") if key is not None else None
        if next_cursor is not None:
            _parse_result_key(next_cursor)
        return {**counts, "complete": next_cursor is None, "next_cursor": next_cursor}

    @cached_property
    def _school_catalog(self) -> bytes:
        response = self._s3.get_object(Bucket=self._bucket, Key=SCHOOL_LOGOS_KEY)
        content = _read_bounded(response, maximum=SCHOOL_CATALOG_MAX_BYTES)
        if hashlib.sha256(content).hexdigest() != _ASSET_DIGESTS[SCHOOL_LOGOS_KEY]:
            raise ValueError("gallery_school_catalog_checksum_invalid")
        return content

    def _collector(self, offer_id: str) -> DynamoItem:
        offer = self._dynamodb.get_item(
            TableName=self._source,
            Key=marshal_item({"PK": f"GACHA#OFFER#{offer_id}", "SK": "META"}),
            ConsistentRead=True,
            ProjectionExpression="PK, SK, guild_id, #state, owner_key",
            ExpressionAttributeNames={"#state": "state"},
        ).get("Item")
        if offer is None:
            return {}
        item = unmarshal_item(offer)
        owner_key = _text(item, "owner_key")
        if (
            item.get("PK") != f"GACHA#OFFER#{offer_id}"
            or item.get("SK") != "META"
            or item.get("guild_id") != self._guild_id
            or item.get("state") != "redeemed"
            or _OWNER_KEY.fullmatch(owner_key) is None
        ):
            raise ValueError("gallery_offer_invalid")
        profile = self._dynamodb.get_item(
            TableName=self._statistics,
            Key=marshal_item({"PK": "AFFECTION#PROFILE", "SK": owner_key}),
            ConsistentRead=True,
            ProjectionExpression="record_type, display_name",
        ).get("Item")
        identity: DynamoItem = {"owner_key": owner_key}
        if profile is not None:
            candidate = unmarshal_item(profile)
            name = candidate.get("display_name")
            if (
                candidate.get("record_type") == "affection_profile"
                and isinstance(name, str)
                and 1 <= len(name.strip()) <= 100
                and name.isprintable()
            ):
                identity["collector_display_name"] = name.strip()
        return identity

    def _existing(self, expected: DynamoItem) -> DynamoItem | None:
        result = self._dynamodb.get_item(
            TableName=self._statistics,
            Key=marshal_item({"PK": expected["PK"], "SK": expected["SK"]}),
            ConsistentRead=True,
        ).get("Item")
        if result is None:
            return None
        item = unmarshal_item(result)
        if any(
            item.get(key) != value
            for key, value in expected.items()
            if key not in {"school_name", "owner_key", "collector_display_name"}
        ) or item.get("rarity") != expected.get("rarity"):
            raise ValueError("gallery_projection_conflict")
        return item

    def _put_new(self, metadata: DynamoItem) -> bool:
        try:
            self._dynamodb.put_item(
                TableName=self._statistics,
                Item=marshal_item(metadata),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except self._dynamodb.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def _put_thumbnail(self, metadata: DynamoItem, thumbnail: DynamoItem) -> bool:
        try:
            self._dynamodb.put_item(
                TableName=self._statistics,
                Item=marshal_item({**metadata, "thumbnail": thumbnail}),
                ConditionExpression="#stored = :stored AND attribute_not_exists(thumbnail)",
                ExpressionAttributeNames={"#stored": "stored"},
                ExpressionAttributeValues=marshal_item({":stored": metadata["stored"]}),
            )
        except self._dynamodb.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def _read_original(self, stored: StoredGachaImage) -> bytes:
        response = self._s3.get_object(
            Bucket=self._bucket,
            Key=stored.key,
            VersionId=stored.version_id,
        )
        content = _read_bounded(response, maximum=RESULT_MAX_BYTES)
        if (
            response.get("VersionId") != stored.version_id
            or response.get("ContentType") != "image/png"
            or hashlib.sha256(content).hexdigest() != stored.sha256
        ):
            raise ValueError("gallery_source_image_invalid")
        return content

    def _store_thumbnail(self, offer_id: str, source_sha256: str, body: bytes) -> DynamoItem:
        if not 1 <= len(body) <= _THUMBNAIL_MAX_BYTES:
            raise ValueError("gallery_thumbnail_size_invalid")
        key = f"{GALLERY_THUMBNAIL_PREFIX}{offer_id}/{source_sha256}.webp"
        digest = hashlib.sha256(body).digest()
        checksum = base64.b64encode(digest).decode("ascii")
        metadata = {"source-sha256": source_sha256}
        try:
            response = self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="image/webp",
                CacheControl="private, no-store",
                ChecksumSHA256=checksum,
                IfNoneMatch="*",
                Metadata=metadata,
            )
            version = response.get("VersionId")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            existing = self._s3.head_object(Bucket=self._bucket, Key=key, ChecksumMode="ENABLED")
            if (
                existing.get("ContentType") != "image/webp"
                or existing.get("Metadata") != metadata
                or not 1 <= existing.get("ContentLength", 0) <= _THUMBNAIL_MAX_BYTES
            ):
                raise ValueError("gallery_thumbnail_conflict") from None
            try:
                digest = base64.b64decode(existing.get("ChecksumSHA256", ""), validate=True)
            except binascii.Error:
                raise ValueError("gallery_thumbnail_conflict") from None
            if len(digest) != hashlib.sha256().digest_size:
                raise ValueError("gallery_thumbnail_conflict") from None
            version = existing.get("VersionId")
        if not isinstance(version, str) or not version or version == "null":
            raise ValueError("gallery_thumbnail_version_invalid")
        return {
            "key": key,
            "version_id": version,
            "sha256": digest.hex(),
            "width": GALLERY_THUMBNAIL_SIZE[0],
            "height": GALLERY_THUMBNAIL_SIZE[1],
        }


def _parse_result_key(key: str) -> tuple[str, str]:
    match = GALLERY_RESULT_KEY.fullmatch(key)
    if match is None:
        raise ValueError("gallery_source_key_invalid")
    posted_at, offer_id = match.groups()
    try:
        datetime.fromisoformat(posted_at)
    except ValueError:
        raise ValueError("gallery_source_key_invalid") from None
    return posted_at, offer_id


def _text(item: DynamoItem, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("gallery_source_text_invalid")
    return value


def _map(item: DynamoItem, key: str) -> DynamoItem:
    value = item.get(key)
    if not isinstance(value, dict):
        raise ValueError("gallery_source_map_invalid")
    return value


def _stored_image(item: DynamoItem) -> StoredGachaImage:
    return StoredGachaImage(_text(item, "key"), _text(item, "version_id"), _text(item, "sha256"))


def _render_thumbnail(content: bytes) -> bytes:
    with Image.open(io.BytesIO(content), formats=["PNG"]) as original:
        if original.size != RESULT_SIZE or getattr(original, "n_frames", 1) != 1:
            raise ValueError("gallery_source_dimensions_invalid")
        with (
            original.resize(GALLERY_THUMBNAIL_SIZE, Image.Resampling.LANCZOS) as thumbnail,
            io.BytesIO() as output,
        ):
            thumbnail.save(output, format="WEBP", quality=82, method=4)
            return output.getvalue()


def _read_bounded(response: GetObjectOutputTypeDef, *, maximum: int) -> bytes:
    with response["Body"] as body:
        size = response.get("ContentLength", 0)
        if not 1 <= size <= maximum:
            raise ValueError("gallery_source_length_invalid")
        content: bytes = body.read(maximum + 1)
    if len(content) != size:
        raise ValueError("gallery_source_length_invalid")
    return content
