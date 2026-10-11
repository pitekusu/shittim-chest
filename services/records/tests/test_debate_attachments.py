"""Private images remain immutable across replay, expiry, and Archive publication."""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from pydantic import ValidationError
from shittim_chest.adapters.assets.images import (
    DebateImageFailure,
    S3DebateImageStore,
    validate_image,
)
from shittim_chest.adapters.dynamodb.codec import unmarshal_item
from shittim_chest.domain.attachments import DebateImage
from tests.factories import NOW, completed_snapshot, presentation
from tests.test_read_api import HMAC_KEY, SESSION_KEY, FakeReader

from shittim_records.archive import project_completed_debate
from shittim_records.contracts import DebateAttachmentUploadRequest, DebateStartRequest
from shittim_records.debate_attachments import (
    AttachmentFailure,
    DebateAttachmentService,
    DynamoDebateAttachmentRepository,
)
from shittim_records.read_api import CursorCodec, RecordsReadService

ID = "00000000-0000-4000-8000-00000000000a"
REQUEST = "00000000-0000-4000-8000-00000000000b"


def png(*, width=12, height=8, mode="RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, (width, height), (25, 100, 200, 90) if mode == "RGBA" else (25, 100, 200)).save(
        out, format="PNG"
    )
    return out.getvalue()


def image(attachment_id=ID) -> DebateImage:
    content = png()
    return DebateImage(
        attachment_id,
        "image/png",
        len(content),
        hashlib.sha256(content).hexdigest(),
        12,
        8,
        f"debate-images/{attachment_id}/original.png",
        f"debate-images/{attachment_id}/vision.webp",
        "original-v1",
        "vision-v1",
    )


class S3:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.ticket: dict[str, Any] | None = None

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        value = {
            "content": kwargs["Body"],
            "ContentLength": len(kwargs["Body"]),
            "ContentType": kwargs["ContentType"],
            "ChecksumSHA256": kwargs["ChecksumSHA256"],
            "VersionId": "v1",
            "Metadata": kwargs.get("Metadata", {}),
        }
        self.objects[key] = value
        return {"VersionId": "v1"}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return self.objects[kwargs["Key"]]

    def get_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[kwargs["Key"]]
        return {**data, "Body": io.BytesIO(data["content"])}

    def delete_object(self, **kwargs):
        self.deleted.append(kwargs)
        self.objects.pop(kwargs["Key"], None)

    def generate_presigned_post(self, **kwargs):
        self.ticket = kwargs
        return {"url": "https://upload.example.invalid", "fields": kwargs["Fields"]}


def test_original_bytes_and_alpha_survive_vision_derivation() -> None:
    original = png(width=3000, height=4)
    validated = validate_image(original, content_type="image/png")
    assert validated.original == original
    assert (validated.width, validated.height) == (3000, 4)
    with Image.open(io.BytesIO(validated.vision)) as derived:
        assert derived.format == "WEBP"
        assert derived.width == 2048
        assert "A" in derived.getbands()
    assert original.hex() not in repr(validated)


def test_rotated_jpeg_metadata_uses_original_encoded_bounds() -> None:
    out = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (12, 8), "red").save(out, format="JPEG", exif=exif)
    validated = validate_image(out.getvalue(), content_type="image/jpeg")
    assert (validated.width, validated.height) == (12, 8)
    assert validated.original == out.getvalue()
    with Image.open(io.BytesIO(validated.vision)) as derived:
        assert derived.size == (8, 12)
        assert not derived.getexif()


@pytest.mark.parametrize("content,type_", [(b"not an image", "image/png"), (png(), "image/jpeg")])
def test_invalid_content_and_mime_spoof_are_rejected(content, type_) -> None:
    with pytest.raises(DebateImageFailure, match="ATTACHMENT_INVALID"):
        validate_image(content, content_type=type_)


def test_multiframe_and_checksum_mismatch_are_rejected() -> None:
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        out,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
    )
    with pytest.raises(DebateImageFailure):
        validate_image(out.getvalue(), content_type="image/webp")
    with pytest.raises(DebateImageFailure):
        validate_image(png(), content_type="image/png", sha256="a" * 64)


def test_store_replays_identical_objects_and_rejects_rebinding() -> None:
    s3 = S3()
    store = S3DebateImageStore(cast(Any, s3), media_bucket_name="private-media")
    value = validate_image(png(), content_type="image/png")
    first = store.persist(ID, value)
    assert store.persist(ID, value) == first
    assert asyncio.run(store.load_original(first)) == value.original
    assert asyncio.run(store.load_vision_images((first,)))[0].content == value.vision
    with pytest.raises(DebateImageFailure, match="ATTACHMENT_CONFLICT"):
        store.persist(ID, validate_image(png(width=13), content_type="image/png"))


def test_ticket_fences_exact_key_checksum_type_and_length() -> None:
    s3 = S3()
    store = S3DebateImageStore(
        cast(Any, s3), media_bucket_name="media", upload_bucket_name="staging"
    )
    store.upload_ticket(
        key="debate-uploads/opaque.bin", content_type="image/png", sha256="a" * 64, size_bytes=500
    )
    assert s3.ticket is not None
    assert ["content-length-range", 500, 500] in s3.ticket["Conditions"]
    assert s3.ticket["ExpiresIn"] == 600
    assert s3.ticket["Fields"]["key"] == "debate-uploads/opaque.bin"
    assert "x-amz-checksum-sha256" in s3.ticket["Fields"]


def test_partial_promotion_cleanup_deletes_exact_version_only() -> None:
    s3 = S3()
    store = S3DebateImageStore(cast(Any, s3), media_bucket_name="media")
    original = validate_image(png(), content_type="image/png")
    store._put(
        f"debate-images/{ID}/original.png",
        original.original,
        "image/png",
        source_sha256=original.sha256,
    )
    store.delete_orphan(ID, "image/png", original.sha256)
    assert s3.deleted == [
        {"Bucket": "media", "Key": f"debate-images/{ID}/original.png", "VersionId": "v1"}
    ]


def test_archive_v4_preserves_twelve_rows_and_order_sensitive_fingerprint() -> None:
    snapshot = completed_snapshot()

    def project(images):
        return project_completed_debate(
            replace(snapshot, attachments=images),
            identity_hmac_key=HMAC_KEY,
            presentation=presentation(),
            projected_at=NOW,
        )

    baseline = project(())
    one = project((image(),))
    second = image("00000000-0000-4000-8000-00000000000c")
    assert baseline.schema_version == 1
    assert one.schema_version == 4 and len(one.items) == 12
    assert one.items[0]["attachments"][0]["sha256"] == image().sha256
    assert (
        project((image(), second)).source_fingerprint
        != project((second, image())).source_fingerprint
    )
    assert project(()).source_fingerprint == baseline.source_fingerprint
    assert not any("url" in descriptor for descriptor in one.items[0]["attachments"])


def test_read_v4_exposes_private_original_and_thumbnail_with_expiry() -> None:
    reader = FakeReader()
    projection = project_completed_debate(
        replace(completed_snapshot(), attachments=(image(),)),
        identity_hmac_key=HMAC_KEY,
        presentation=presentation(),
        projected_at=NOW,
    )
    reader.items, reader.record_id = projection.items, projection.record_id
    calls = []

    def url(value, *, thumbnail=False):
        calls.append(thumbnail)
        key = value.vision_asset_key if thumbnail else value.asset_key
        return f"https://shittim-chest-production-records-media-000000000000.s3.ap-northeast-1.amazonaws.com/{key}?signed=fixture"

    cast(Any, reader).attachment_url = url
    detail = RecordsReadService(reader=reader, cursor_codec=CursorCodec(SESSION_KEY)).get_record(
        record_id=reader.record_id, now=NOW
    )
    assert detail.attachments is not None
    assert detail.attachments[0].attachment_id == ID
    assert detail.attachments[0].url_expires_at == NOW + timedelta(seconds=300)
    assert calls == [False, True]


def test_upload_request_requires_uuid4_and_start_rejects_duplicates() -> None:
    content = png()
    with pytest.raises(ValidationError):
        DebateAttachmentUploadRequest(
            request_id=REQUEST,
            attachment_id="00000000-0000-5000-8000-00000000000a",
            content_type="image/png",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    with pytest.raises(ValidationError):
        DebateStartRequest(
            request_id=REQUEST, question="Fictional question", attachment_ids=[ID, ID]
        )
    accepted = DebateStartRequest.model_validate(
        {"requestId": REQUEST, "question": "Fictional question", "attachmentIds": [ID]}, strict=True
    )
    assert accepted.attachment_ids == [ID]


class Repository:
    def __init__(self, *, state="reserved"):
        self.item = {
            "request_id": REQUEST,
            "attachment_id": ID,
            "state": state,
            "expires_at": int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
            "upload_key": "debate-uploads/opaque.bin",
            "content_type": "image/png",
            "size_bytes": len(png()),
            "sha256": hashlib.sha256(png()).hexdigest(),
        }
        self.claims = 0
        self.releases = 0
        self.cleaned_items = []

    def get(self, owner, attachment_id):
        return self.item if owner == "owner" and attachment_id == ID else None

    def claim(self, item, now):
        self.claims += 1
        return "claim"

    def release(self, item, token):
        self.releases += 1

    def complete(self, item, token, value, now):
        from shittim_records.debate_attachments import image_payload

        item.update(state="ready", image=image_payload(value))

    def expire(self, now):
        return () if self.item["state"] == "consumed" else (self.item,)

    def cleaned(self, item):
        self.cleaned_items.append(item)


def test_missing_upload_can_be_resumed_and_ready_complete_is_replayed() -> None:
    repository, s3 = Repository(), S3()
    store = S3DebateImageStore(
        cast(Any, s3), media_bucket_name="media", upload_bucket_name="staging"
    )
    service = DebateAttachmentService(cast(Any, repository), store)
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_NOT_READY"):
        service.complete("owner", ID, datetime.now(UTC))
    assert repository.releases == 1
    s3.objects[repository.item["upload_key"]] = {"content": png(), "ContentLength": len(png())}
    result = service.complete("owner", ID, datetime.now(UTC))
    assert result["status"] == "ready"
    assert service.complete("owner", ID, datetime.now(UTC)) == result
    assert repository.claims == 2
    with pytest.raises(AttachmentFailure, match="ATTACHMENT_NOT_FOUND"):
        service.status("other-owner", ID, datetime.now(UTC))


def test_cleanup_never_touches_consumed_image() -> None:
    repository, s3 = Repository(state="consumed"), S3()
    service = DebateAttachmentService(
        cast(Any, repository), S3DebateImageStore(cast(Any, s3), media_bucket_name="media")
    )
    assert service.cleanup(datetime.now(UTC)) == 0
    assert not s3.deleted and not repository.cleaned_items


def test_atomic_consume_requires_same_ready_content_request_and_live_expiry() -> None:
    repository = DynamoDebateAttachmentRepository(cast(Any, object()), "source")
    action = repository.consume_actions("owner", REQUEST, (image(),), NOW)[0]["Update"]
    values = unmarshal_item(cast(Any, action["ExpressionAttributeValues"]))
    assert values[":request"] == REQUEST and values[":ready"] == "ready"
    assert isinstance(values[":image"], dict)
    assert values[":image"]["sha256"] == image().sha256
    assert "expires_at > :now" in action["ConditionExpression"]
    assert action["UpdateExpression"].startswith("SET #state = :consumed")


def test_discord_import_recovers_partial_private_original_without_cdn(monkeypatch) -> None:
    from shittim_chest.adapters.assets import images as module
    from shittim_chest.application.models import PendingDiscordImage

    s3 = S3()
    store = S3DebateImageStore(cast(Any, s3), media_bucket_name="media")
    original = validate_image(png(), content_type="image/png")
    store._put(
        f"debate-images/{ID}/original.png",
        original.original,
        "image/png",
        source_sha256=original.sha256,
    )
    monkeypatch.setattr(
        module.httpx2,
        "AsyncClient",
        lambda **kwargs: pytest.fail("CDN must not be fetched after private original is durable"),
    )
    pending = PendingDiscordImage(
        ID,
        "123",
        "image/png",
        len(original.original),
        "https://cdn.discordapp.com/attachments/456/123/image.png?ex=expired",
    )
    recovered = asyncio.run(store.import_discord_attachment(pending))
    assert recovered.sha256 == original.sha256
    assert f"debate-images/{ID}/vision.webp" in s3.objects


def test_orphan_cleanup_refuses_objects_with_another_source_checksum() -> None:
    s3 = S3()
    store = S3DebateImageStore(cast(Any, s3), media_bucket_name="media")
    validated = validate_image(png(), content_type="image/png")
    store.persist(ID, validated)
    with pytest.raises(DebateImageFailure, match="ATTACHMENT_CONFLICT"):
        store.delete_orphan(ID, "image/png", "a" * 64)
    assert not s3.deleted


def test_shared_core_store_serializes_image_decoding(monkeypatch) -> None:
    import threading
    import time

    from shittim_chest.adapters.assets import images as module
    from shittim_chest.application.models import PendingDiscordImage

    s3 = S3()
    store = S3DebateImageStore(cast(Any, s3), media_bucket_name="media")
    validated = validate_image(png(), content_type="image/png")
    ids = [ID, "00000000-0000-4000-8000-00000000000c"]
    for attachment_id in ids:
        store._put(
            f"debate-images/{attachment_id}/original.png",
            validated.original,
            "image/png",
            source_sha256=validated.sha256,
        )
    active = 0
    maximum = 0
    lock = threading.Lock()
    real_validate = module.validate_image

    def counted(content, *, content_type):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.02)
            return real_validate(content, content_type=content_type)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(module, "validate_image", counted)

    async def run():
        return await asyncio.gather(
            *(
                store.import_discord_attachment(
                    PendingDiscordImage(
                        attachment_id,
                        "123",
                        "image/png",
                        len(validated.original),
                        "https://cdn.discordapp.com/attachments/456/123/image.png",
                    )
                )
                for attachment_id in ids
            )
        )

    result = asyncio.run(run())
    assert len(result) == 2 and maximum == 1
