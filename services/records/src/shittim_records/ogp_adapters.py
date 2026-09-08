"""Limited AWS reads/cache writes for public record previews."""

from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import Future
from datetime import datetime
from threading import Thread
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from shittim_records.ogp import RECORD_ID, RecordPreview

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_s3.client import S3Client

META_ATTRIBUTES = (
    "PK",
    "SK",
    "schema_version",
    "record_type",
    "record_id",
    "question",
    "requester_key",
    "requester_display_name",
    "completed_at",
)
PROFILE_ATTRIBUTES = (
    "PK",
    "SK",
    "schema_version",
    "record_type",
    "display_name",
    "avatar_asset_key",
    "expiresAt",
    "updated_at",
)
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class AwsPreviewStore:
    def __init__(
        self, archive: Table, profiles: Table, s3: S3Client, *, media_bucket: str, web_bucket: str
    ) -> None:
        self.archive = archive
        self.profiles = profiles
        self.s3 = s3
        self.media_bucket = media_bucket
        self.web_bucket = web_bucket

    @staticmethod
    def _item(
        table: Table, key: dict[str, str], attributes: tuple[str, ...]
    ) -> dict[str, Any] | None:
        names = {f"#a{index}": name for index, name in enumerate(attributes)}
        return table.get_item(
            Key=key,
            ConsistentRead=True,
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        ).get("Item")

    def preview(self, record_id: str) -> RecordPreview | None:
        if re.fullmatch(RECORD_ID, record_id) is None:
            raise ValueError("invalid preview record")
        meta = self._item(
            self.archive, {"PK": f"RECORD#{record_id}", "SK": "META"}, META_ATTRIBUTES
        )
        if meta is None:
            return None
        if (
            meta.get("record_type") != "archive_meta"
            or meta.get("schema_version") not in {1, 2, 3}
            or meta.get("record_id") != record_id
        ):
            raise ValueError("invalid preview metadata")
        requester_key = _text(meta, "requester_key")
        if re.fullmatch(RECORD_ID, requester_key) is None:
            raise ValueError("invalid preview profile")
        profile = self._item(
            self.profiles, {"PK": "PROFILE#REQUESTER", "SK": requester_key}, PROFILE_ATTRIBUTES
        )
        if profile is not None and (
            profile.get("schema_version") != 1 or profile.get("record_type") != "requester_profile"
        ):
            raise ValueError("invalid preview profile")
        avatar_key = profile.get("avatar_asset_key") if profile else None
        profile_version = profile.get("updated_at") if profile else None
        if profile_version is not None and not isinstance(profile_version, str):
            raise ValueError("invalid preview profile")
        if avatar_key is not None and not isinstance(avatar_key, str):
            raise ValueError("invalid preview avatar")
        return RecordPreview(
            record_id=record_id,
            question=_text(meta, "question"),
            requester_name=_text(profile, "display_name")
            if profile
            else _text(meta, "requester_display_name"),
            completed_at=datetime.fromisoformat(_text(meta, "completed_at")),
            avatar_key=avatar_key,
            profile_version=profile_version,
        )

    def _read(self, bucket: str, key: str, limit: int) -> bytes | None:
        try:
            response = self.s3.get_object(Bucket=bucket, Key=key)
        except ClientError as error:
            # Without ListBucket, S3 reports missing private objects as AccessDenied.
            # Optional reads become a cache miss; PutObject still must succeed before posting.
            if error.response["Error"]["Code"] in {"NoSuchKey", "404", "AccessDenied", "403"}:
                return None
            raise
        with response["Body"] as body:
            if response.get("ContentLength", 0) > limit:
                raise ValueError("preview object too large")
            content = body.read(limit + 1)
            if len(content) > limit:
                raise ValueError("preview object too large")
            return content

    def index_html(self) -> str:
        content = self._read(self.web_bucket, "index.html", 512 * 1024)
        if content is None:
            raise ValueError("preview template unavailable")
        return content.decode("utf-8")

    def cached_image(self, key: str) -> bytes | None:
        return self._read(self.media_bucket, key, MAX_IMAGE_BYTES)

    def save_image(self, key: str, image: bytes) -> bytes:
        if len(image) > MAX_IMAGE_BYTES:
            raise ValueError("preview image too large")
        # First complete object wins, including concurrent avatar fallback outcomes.
        try:
            self.s3.put_object(
                Bucket=self.media_bucket,
                Key=key,
                Body=image,
                ContentType="image/png",
                CacheControl="public, max-age=31536000, immutable",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
            return image
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                raise
            winner = self.cached_image(key)
            if winner is None:
                raise ValueError("preview cache unavailable") from None
            return winner

    def avatar(self, key: str) -> bytes | None:
        try:
            return self._read(self.media_bucket, key, 512 * 1024)
        except Exception:
            return None  # Missing/oversized/unavailable avatars use the neutral icon.


class LambdaPreviewPreparer:
    """At most five seconds of caller waiting, without asyncio executor shutdown waits."""

    def __init__(self, function_name: str) -> None:
        self.function_name = function_name
        self.client = boto3.client(
            "lambda",
            config=Config(
                connect_timeout=1,
                read_timeout=4,
                retries={"total_max_attempts": 1},
            ),
        )

    async def prepare(self, record_id: str) -> None:
        result: Future[None] = Future()

        def invoke() -> None:
            if not result.set_running_or_notify_cancel():
                return
            try:
                response = self.client.invoke(
                    FunctionName=self.function_name,
                    InvocationType="RequestResponse",
                    Payload=json.dumps({"recordId": record_id}).encode(),
                )
                with response["Payload"] as body:
                    prepared = json.loads(body.read(1024))
                if response.get("FunctionError") or prepared != {"prepared": True}:
                    raise ValueError("preview preparation unavailable")
                result.set_result(None)
            except Exception:
                result.set_exception(ValueError("preview preparation unavailable"))

        Thread(target=invoke, daemon=True).start()
        await asyncio.wait_for(asyncio.wrap_future(result), timeout=5)


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid preview metadata")
    return value
