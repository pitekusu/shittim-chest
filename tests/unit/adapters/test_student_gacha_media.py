from __future__ import annotations

import base64
import hashlib
import io
import json
from unittest.mock import AsyncMock, Mock, call

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from PIL import Image

from shittim_chest.adapters.assets.student_gacha import (
    _ASSET_DIGESTS,
    RESULT_SIZE,
    SCHOOL_LOGOS_KEY,
    StudentGachaMedia,
    _parse_roster,
)
from shittim_chest.application.student_gacha_models import (
    GACHA_APNG_FRAME_DURATIONS,
    GACHA_GIF_FRAME_DURATIONS,
    GACHA_LEGACY_APNG_FRAME_DURATIONS,
    GachaGenerationFailure,
    GachaImageFormat,
    SchoolBranding,
    StoredGachaImage,
    Student,
    normalize_teacher_name,
    student_gacha_image_key,
)

_DRAW_ID = "0" * 32


def _animation(
    image_format: GachaImageFormat,
    *,
    durations: tuple[int, ...] | None = None,
    loop: int = 0,
    default_image: bool = False,
) -> bytes:
    if durations is None:
        durations = image_format.frame_durations
    frames = [
        Image.new("RGB", image_format.size, (18 * frame, 54, 88))
        for frame in range(len(durations) + default_image)
    ]
    options = {"default_image": True} if default_image else {}
    with io.BytesIO() as output:
        frames[0].save(
            output,
            image_format.pillow_format,
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=loop,
            **options,
        )
        return output.getvalue()


def _missing_object() -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "404"},
            "ResponseMetadata": {
                "RequestId": "test",
                "HostId": "test",
                "HTTPStatusCode": 404,
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        "HeadObject",
    )


@pytest.mark.asyncio
async def test_save_reconciles_lost_put_response_and_loads_verified_version() -> None:
    apng = _animation(GachaImageFormat.APNG)
    checksum = base64.b64encode(hashlib.sha256(apng).digest()).decode("ascii")
    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    apng_metadata = {
        "ContentType": "image/apng",
        "ContentLength": len(apng),
        "ChecksumSHA256": checksum,
        "VersionId": "original-version",
    }
    client.head_object.side_effect = [*(_missing_object() for _ in range(4)), apng_metadata]
    client.get_object.return_value = {**apng_metadata, "Body": io.BytesIO(apng)}
    media = StudentGachaMedia(client, bucket_name="private-media")

    stored = await media.save(_DRAW_ID, apng)

    assert stored.sha256 == hashlib.sha256(apng).hexdigest()
    assert stored.key == student_gacha_image_key(_DRAW_ID, GachaImageFormat.APNG)
    assert client.put_object.call_args.kwargs["Body"] == apng
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert client.put_object.call_args.kwargs["ChecksumSHA256"] == checksum
    assert client.put_object.call_args.kwargs["ContentType"] == "image/apng"
    assert await media.load(stored) == apng
    assert client.get_object.call_args.kwargs["VersionId"] == "original-version"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_format", (GachaImageFormat.PNG, GachaImageFormat.GIF))
async def test_finds_legacy_result_without_overwriting_it_with_apng(
    legacy_format: GachaImageFormat,
) -> None:
    if legacy_format is GachaImageFormat.PNG:
        with io.BytesIO() as output:
            Image.new("RGB", RESULT_SIZE, "lavender").save(output, "PNG")
            legacy_bytes = output.getvalue()
    else:
        legacy_bytes = _animation(legacy_format)
    legacy_key = student_gacha_image_key(_DRAW_ID, legacy_format)
    legacy_digest = hashlib.sha256(legacy_bytes)
    legacy_metadata = {
        "ContentLength": len(legacy_bytes),
        "ContentType": legacy_format.content_type,
        "ChecksumSHA256": base64.b64encode(legacy_digest.digest()).decode("ascii"),
        "VersionId": "old-version",
    }
    objects = {legacy_key: legacy_metadata}

    def head_object(*, Key: str, **_kwargs: object) -> dict[str, str | int]:
        if Key in objects:
            return objects[Key]
        raise _missing_object()

    client = Mock()
    client.head_object.side_effect = head_object
    client.get_object.return_value = {**legacy_metadata, "Body": io.BytesIO(legacy_bytes)}
    media = StudentGachaMedia(client, bucket_name="private-media")

    stored = await media.find(_DRAW_ID)
    assert stored == StoredGachaImage(legacy_key, "old-version", legacy_digest.hexdigest())
    assert stored is not None
    assert await media.load(stored) == legacy_bytes

    apng = _animation(GachaImageFormat.APNG)
    with pytest.raises(GachaGenerationFailure, match="gacha_result_conflict") as failure:
        await media.save(_DRAW_ID, apng)
    assert failure.value.ambiguous
    client.put_object.assert_not_called()

    objects[student_gacha_image_key(_DRAW_ID, GachaImageFormat.APNG)] = {
        "ContentLength": len(apng),
        "ContentType": "image/apng",
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(apng).digest()).decode("ascii"),
        "VersionId": "conflicting-version",
    }
    with pytest.raises(GachaGenerationFailure, match="gacha_result_conflict") as failure:
        await media.find(_DRAW_ID)
    assert failure.value.ambiguous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("durations", "loop", "default_image"),
    (
        ((170,), 0, False),
        (GACHA_GIF_FRAME_DURATIONS, 0, False),
        (
            GACHA_APNG_FRAME_DURATIONS[:-1] + GACHA_LEGACY_APNG_FRAME_DURATIONS[-1:],
            0,
            False,
        ),
        (GACHA_APNG_FRAME_DURATIONS, 1, False),
        (GACHA_APNG_FRAME_DURATIONS[:-1], 0, True),
    ),
)
async def test_rejects_invalid_apng_before_saving(
    durations: tuple[int, ...], loop: int, default_image: bool
) -> None:
    apng = _animation(
        GachaImageFormat.APNG, durations=durations, loop=loop, default_image=default_image
    )
    client = Mock()
    media = StudentGachaMedia(client, bucket_name="private-media")

    with pytest.raises(GachaGenerationFailure, match="gacha_media_invalid"):
        await media.save(_DRAW_ID, apng)
    client.put_object.assert_not_called()


@pytest.mark.asyncio
async def test_missing_result_is_distinguished_from_access_denied() -> None:
    client = Mock()
    client.head_object.side_effect = ClientError(
        {
            "Error": {"Code": "403"},
            "ResponseMetadata": {
                "RequestId": "test",
                "HostId": "test",
                "HTTPStatusCode": 403,
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        "HeadObject",
    )
    client.list_objects_v2.return_value = {}
    media = StudentGachaMedia(client, bucket_name="private-media")

    assert await media.find(_DRAW_ID) is None
    assert client.list_objects_v2.call_args_list == [
        call(
            Bucket="private-media",
            Prefix=student_gacha_image_key(_DRAW_ID, image_format),
            MaxKeys=1,
        )
        for image_format in GachaImageFormat
    ]

    client.list_objects_v2.return_value = {"Contents": [{"Key": student_gacha_image_key(_DRAW_ID)}]}
    with pytest.raises(GachaGenerationFailure, match="gacha_result_lookup_failed"):
        await media.find(_DRAW_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "student",
    [
        Student("コハル\uff08水着\uff09", "トリニティ", rarity=3),
        Student("ナグサ", "百鬼夜行連合学院", rarity=0),
        Student("アロナ", "別の所属", rarity=4),
    ],
)
async def test_costume_reference_passes_the_exact_draw_to_the_wiki_resolver(
    monkeypatch: pytest.MonkeyPatch,
    student: Student,
) -> None:
    load_wiki_costume = AsyncMock(return_value=b"costume WebP")
    monkeypatch.setattr(
        "shittim_chest.adapters.assets.student_gacha.load_wiki_costume", load_wiki_costume
    )
    client = Mock()
    media = StudentGachaMedia(client, bucket_name="private-media")

    assert await media.costume_reference(student) == b"costume WebP"
    load_wiki_costume.assert_awaited_once_with(student)
    client.get_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("student", "participant", "file_format"),
    [
        (Student("アロナ", "シッテムの箱", 4), "participant-a", "PNG"),
        (Student("プラナ", "シッテムの箱", 4), "participant-b", "WEBP"),
        (Student("安倍晋三", "自民党", 4), "participant-c", "WEBP"),
        (Student("プラナ", "シッテムの箱"), "participant-b", "WEBP"),
    ],
)
async def test_special_costume_uses_the_matching_private_portrait_as_webp(
    monkeypatch: pytest.MonkeyPatch, student: Student, participant: str, file_format: str
) -> None:
    with io.BytesIO() as output:
        Image.new("RGB", (16, 24), "lavender").save(output, file_format)
        portrait = output.getvalue()
    load_wiki_costume = AsyncMock()
    monkeypatch.setattr(
        "shittim_chest.adapters.assets.student_gacha.load_wiki_costume", load_wiki_costume
    )
    buffer = io.BytesIO(portrait)
    client = Mock()
    client.get_object.return_value = {"ContentLength": len(portrait), "Body": buffer}
    media = StudentGachaMedia(client, bucket_name="private-media")

    content = await media.costume_reference(student)

    with Image.open(io.BytesIO(content)) as image:
        assert image.format == "WEBP"
        assert image.size == (16, 24)
    client.get_object.assert_called_once_with(
        Bucket="private-media",
        Key=f"participants/{participant}/memorial-reference.{file_format.lower()}",
    )
    load_wiki_costume.assert_not_awaited()
    assert buffer.closed


@pytest.mark.asyncio
async def test_invalid_special_costume_has_no_usable_reference() -> None:
    content = b"not an image"
    buffer = io.BytesIO(content)
    client = Mock()
    client.get_object.return_value = {"ContentLength": len(content), "Body": buffer}
    media = StudentGachaMedia(client, bucket_name="private-media")

    with pytest.raises(GachaGenerationFailure, match="gacha_costume_reference_invalid"):
        await media.costume_reference(Student("安倍晋三", "自民党", 4))
    assert buffer.closed


@pytest.mark.asyncio
async def test_reads_only_the_verified_private_school_logo_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = b"verified school logo bundle"
    buffer = io.BytesIO(catalog)
    branding = SchoolBranding("ゲヘナ学園")
    lookup = Mock(return_value=branding)
    monkeypatch.setitem(_ASSET_DIGESTS, SCHOOL_LOGOS_KEY, hashlib.sha256(catalog).hexdigest())
    monkeypatch.setattr("shittim_chest.adapters.assets.student_gacha.school_branding", lookup)
    client = Mock()
    client.get_object.return_value = {"ContentLength": len(catalog), "Body": buffer}

    result = await StudentGachaMedia(client, bucket_name="private-media").school_branding("ゲヘナ")

    assert result == branding
    client.get_object.assert_called_once_with(
        Bucket="private-media", Key="student-gacha/assets/blue_archive_school_logos.zip"
    )
    lookup.assert_called_once_with(catalog, "ゲヘナ")
    assert buffer.closed


@pytest.mark.asyncio
async def test_school_logo_bundle_checksum_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = b"different school logo bundle"
    buffer = io.BytesIO(catalog)
    lookup = Mock()
    monkeypatch.setattr("shittim_chest.adapters.assets.student_gacha.school_branding", lookup)
    client = Mock()
    client.get_object.return_value = {"ContentLength": len(catalog), "Body": buffer}

    with pytest.raises(GachaGenerationFailure, match="gacha_asset_checksum_mismatch"):
        await StudentGachaMedia(client, bucket_name="private-media").school_branding("ゲヘナ")

    lookup.assert_not_called()
    assert buffer.closed


def _roster_rows() -> list[dict[str, object]]:
    names = ("リン", "アコ", "アコ\uff08ドレス\uff09", "キキョウ", "ヒナ")
    return [
        {"name": name, "school": "ゲヘナ", "rarity": rarity} for rarity, name in enumerate(names)
    ]


@pytest.mark.asyncio
async def test_reads_the_versioned_published_roster_with_s3_checksum_validation() -> None:
    client = boto3.client(
        "s3", region_name="ap-northeast-1", config=Config(signature_version=UNSIGNED)
    )
    raw = json.dumps(_roster_rows()).encode()
    buffer = io.BytesIO(raw)
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {
                "ContentLength": len(raw),
                "ContentType": "application/json",
                "VersionId": "published-version",
                "ChecksumType": "FULL_OBJECT",
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii"),
                "Body": StreamingBody(buffer, len(raw)),
            },
            {
                "Bucket": "private-media",
                "Key": "student-gacha/roster/blue_archive_characters_with_npcs.json",
                "ChecksumMode": "ENABLED",
            },
        )
        entries = await StudentGachaMedia(client, bucket_name="private-media").roster()
        stubber.assert_no_pending_responses()

    assert list(entry.rarity for entry in entries) == list(range(5))
    assert buffer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        ({"ChecksumSHA256": None}, "gacha_asset_checksum_mismatch"),
        ({"ChecksumSHA256": "unexpected"}, "gacha_asset_checksum_mismatch"),
        ({"ChecksumType": "COMPOSITE"}, "gacha_asset_checksum_mismatch"),
        ({"VersionId": "null"}, "gacha_roster_invalid"),
        ({"ContentType": "text/html"}, "gacha_roster_invalid"),
        ({"ContentLength": 256 * 1024 + 1}, "gacha_media_invalid"),
    ],
)
async def test_rejects_the_unverified_published_roster(
    metadata: dict[str, object], code: str
) -> None:
    raw = json.dumps(_roster_rows()).encode()
    buffer = io.BytesIO(raw)
    client = Mock()
    client.get_object.return_value = {
        "ContentLength": len(raw),
        "ContentType": "application/json",
        "VersionId": "published-version",
        "ChecksumType": "FULL_OBJECT",
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii"),
        "Body": buffer,
        **metadata,
    }

    with pytest.raises(GachaGenerationFailure, match=code):
        await StudentGachaMedia(client, bucket_name="private-media").roster()

    assert buffer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("service_code", ["NoSuchKey", "AccessDenied"])
async def test_unavailable_publication_does_not_read_another_object(service_code: str) -> None:
    client = Mock()
    client.get_object.side_effect = ClientError({"Error": {"Code": service_code}}, "GetObject")

    with pytest.raises(GachaGenerationFailure, match="gacha_media_unavailable"):
        await StudentGachaMedia(client, bucket_name="private-media").roster()

    assert client.get_object.call_count == 1


def test_roster_keeps_npcs_costume_variants_and_rarity() -> None:
    rows = _roster_rows()
    entries = _parse_roster(json.dumps(rows).encode())

    assert [(entry.name, entry.rarity) for entry in entries] == [
        (row["name"], row["rarity"]) for row in rows
    ]


@pytest.mark.parametrize("name", ["アロナ", "プラナ"])
def test_roster_excludes_arona_and_plana_from_rarity_zero(name: str) -> None:
    rows = _roster_rows()
    rows[0]["name"] = name
    rows[0]["school"] = "シッテムの箱"
    with pytest.raises(GachaGenerationFailure, match="gacha_roster_invalid"):
        _parse_roster(json.dumps(rows).encode())

    rows[0]["name"] = "リン"
    rows[4]["name"] = name
    rows[4]["school"] = "シッテムの箱"
    assert _parse_roster(json.dumps(rows).encode())[-1].rarity == 4


@pytest.mark.parametrize("invalid_rarity", [-1, 5, 0.0, True, "0", None])
def test_roster_rejects_invalid_rarity(invalid_rarity: object) -> None:
    rows = _roster_rows()
    rows[0]["rarity"] = invalid_rarity
    with pytest.raises(GachaGenerationFailure, match="gacha_roster_invalid"):
        _parse_roster(json.dumps(rows).encode())


def test_roster_rejects_missing_rank_duplicate_name_and_unexpected_fields() -> None:
    rows = _roster_rows()
    for malformed in (
        rows[1:],
        [*rows, {**rows[0], "school": "トリニティ"}],
        [{**rows[0], "description": "unexpected"}, *rows[1:]],
        [{"name": "リン", "school": "ゲヘナ"}, *rows[1:]],
    ):
        with pytest.raises(GachaGenerationFailure, match="gacha_roster_invalid"):
            _parse_roster(json.dumps(malformed).encode())
    duplicate_key = json.dumps(rows).replace('"rarity": 0', '"rarity": 0, "rarity": 0')
    with pytest.raises(GachaGenerationFailure, match="gacha_roster_invalid"):
        _parse_roster(duplicate_key.encode())


def test_teacher_name_is_normalized_and_rejects_control_characters() -> None:
    assert normalize_teacher_name("  カ\u3099  ") == "ガ"
    for value in ("", "\n先生", "先生\n別名", "先生\u202e", "先生" * 13):
        with pytest.raises(ValueError):
            normalize_teacher_name(value)
