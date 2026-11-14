"""Projection and retry coverage for published student recruitment cards."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from shittim_chest.adapters.assets.student_gacha import (
    _ASSET_DIGESTS,
    RESULT_SIZE,
    SCHOOL_LOGOS_KEY,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application.student_gacha_models import student_gacha_image_key

from shittim_records import lambda_handlers
from shittim_records.gallery_projection import GalleryProjector

if TYPE_CHECKING:
    from shittim_chest.adapters.dynamodb.serializer import DynamoItem

_GUILD = "1" * 17
_OFFER = "a" * 32
_OWNER = "o" * 43
_POSTED = "2026-11-14T12:34:56.123456Z"
_SK = f"RESULT#{_POSTED}#{_OFFER}"
_ORIGINAL_KEY = student_gacha_image_key(_OFFER)


class ConditionalCheckFailed(ClientError):
    pass


@pytest.fixture
def source() -> tuple[DynamoItem, bytes]:
    with Image.new("RGB", RESULT_SIZE, "navy") as original, io.BytesIO() as output:
        original.save(output, format="PNG")
        png = output.getvalue()
    return {
        "PK": f"GACHA#GUILD#{_GUILD}",
        "SK": _SK,
        "posted_at": _POSTED,
        "offer_id": _OFFER,
        "performer": "participant-a",
        "student": {"name": "ハルカ", "school": "ゲヘナ", "rarity": 0},
        "stored": {
            "key": _ORIGINAL_KEY,
            "version_id": "original-version",
            "sha256": hashlib.sha256(png).hexdigest(),
        },
    }, png


@pytest.fixture
def aws(
    source: tuple[DynamoItem, bytes], monkeypatch: pytest.MonkeyPatch
) -> tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]]:
    source_row, png = source
    with io.BytesIO() as output:
        with ZipFile(output, "w") as catalog:
            catalog.writestr(
                "blue_archive_school_logos/schools.json",
                json.dumps(
                    [
                        {
                            "id": "gehenna",
                            "name": "ゲヘナ学園",
                            "kind": "school",
                            "logo_path": None,
                        }
                    ]
                ),
            )
        catalog_bytes = output.getvalue()
    monkeypatch.setitem(_ASSET_DIGESTS, SCHOOL_LOGOS_KEY, hashlib.sha256(catalog_bytes).hexdigest())

    db = Mock()
    db.exceptions.ConditionalCheckFailedException = ConditionalCheckFailed
    table: dict[str, DynamoItem] = {}

    def get_item(**kwargs: Any) -> dict[str, Any]:
        key = unmarshal_item(kwargs["Key"])
        if key["PK"] == f"GACHA#OFFER#{_OFFER}":
            assert kwargs["ProjectionExpression"] == "PK, SK, guild_id, #state, owner_key"
            return {
                "Item": marshal_item(
                    {**key, "guild_id": _GUILD, "state": "redeemed", "owner_key": _OWNER}
                )
            }
        if key["PK"] == "AFFECTION#PROFILE":
            return {
                "Item": marshal_item({"record_type": "affection_profile", "display_name": "先生"})
            }
        assert key["PK"] == f"GACHA#GALLERY#{_GUILD}"
        item = table.get(cast(str, key["SK"]))
        return {"Item": marshal_item(item)} if item is not None else {}

    def put_item(**kwargs: Any) -> None:
        item = unmarshal_item(kwargs["Item"])
        key = cast(str, item["SK"])
        existing = table.get(key)
        if "thumbnail" in item:
            assert (
                kwargs["ConditionExpression"]
                == "#stored = :stored AND attribute_not_exists(thumbnail)"
            )
            assert kwargs["ExpressionAttributeNames"] == {"#stored": "stored"}
        if ("thumbnail" not in item and existing is not None) or (
            "thumbnail" in item and (existing is None or "thumbnail" in existing)
        ):
            raise ConditionalCheckFailed(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
            )
        table[key] = item

    db.get_item.side_effect = get_item
    db.put_item.side_effect = put_item
    db.query.return_value = {
        "Items": [marshal_item(source_row)],
        "LastEvaluatedKey": marshal_item({"PK": source_row["PK"], "SK": _SK}),
    }

    s3 = Mock()
    objects: dict[str, Any] = {}

    def get_object(**kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] == SCHOOL_LOGOS_KEY:
            return {"Body": io.BytesIO(catalog_bytes), "ContentLength": len(catalog_bytes)}
        assert kwargs["Key"] == _ORIGINAL_KEY
        assert kwargs["VersionId"] == "original-version"
        return {
            "Body": io.BytesIO(png),
            "ContentLength": len(png),
            "VersionId": "original-version",
            "ContentType": "image/png",
        }

    def put_object(**kwargs: Any) -> dict[str, str]:
        assert kwargs["IfNoneMatch"] == "*"
        if kwargs["Key"] in objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        objects[kwargs["Key"]] = kwargs
        return {"VersionId": "thumbnail-version"}

    def head_object(**kwargs: Any) -> dict[str, Any]:
        item = objects[kwargs["Key"]]
        assert kwargs["ChecksumMode"] == "ENABLED"
        return {
            "ContentType": item["ContentType"],
            "ContentLength": len(item["Body"]),
            "Metadata": item["Metadata"],
            "VersionId": "thumbnail-version",
            "ChecksumSHA256": item["ChecksumSHA256"],
        }

    s3.get_object.side_effect = get_object
    s3.put_object.side_effect = put_object
    s3.head_object.side_effect = head_object
    return db, s3, table, objects


def _projector(dynamodb: Mock, s3: Mock) -> GalleryProjector:
    return GalleryProjector(
        cast(Any, dynamodb),
        cast(Any, s3),
        source_table_name="source",
        statistics_table_name="statistics",
        media_bucket_name="private-media",
        guild_id=_GUILD,
    )


def test_projector_creates_versioned_webp_and_preserves_original(
    source: tuple[DynamoItem, bytes],
    aws: tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]],
) -> None:
    dynamodb, s3, table, objects = aws
    result, png = source
    projector = _projector(dynamodb, s3)
    assert projector.project_result(result).created
    row = table[_SK]
    assert row["rarity"] == 0
    assert row["school_name"] == "ゲヘナ学園"
    assert row["owner_key"] == _OWNER
    assert row["collector_display_name"] == "先生"
    assert row["stored"] == result["stored"]
    digest = hashlib.sha256(png).hexdigest()
    key = f"student-gacha/thumbnails/v1/{_OFFER}/{digest}.webp"
    webp = objects[key]["Body"]
    assert row["thumbnail"] == {
        "key": key,
        "version_id": "thumbnail-version",
        "sha256": hashlib.sha256(webp).hexdigest(),
        "width": 768,
        "height": 355,
    }
    with Image.open(io.BytesIO(webp)) as image:
        assert image.format == "WEBP"
        assert image.size == (768, 355)
    assert not projector.project_result(result).created
    assert s3.put_object.call_count == 1
    assert dynamodb.put_item.call_count == 2


def test_unverified_original_is_rejected_and_dry_run_rechecks_completed_row(
    source: tuple[DynamoItem, bytes],
    aws: tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]],
) -> None:
    dynamodb, s3, table, objects = aws
    result, _ = source
    projector = _projector(dynamodb, s3)
    get_object = s3.get_object.side_effect

    def corrupt_original(**kwargs: Any) -> dict[str, Any]:
        response = get_object(**kwargs)
        if kwargs["Key"] == _ORIGINAL_KEY:
            response["Body"].close()
            response["Body"] = io.BytesIO(b"bad")
            response["ContentLength"] = 3
        return response

    s3.get_object.side_effect = corrupt_original
    with pytest.raises(ValueError, match="gallery_source_image_invalid"):
        projector.project_result(result)
    assert not table
    assert not objects
    s3.get_object.side_effect = get_object
    assert projector.project_result(result).created
    s3.get_object.side_effect = corrupt_original
    with pytest.raises(ValueError, match="gallery_source_image_invalid"):
        projector.project_result(result, apply=False)
    assert len(objects) == 1


def test_thumbnail_failure_preserves_verified_metadata_for_retry(
    source: tuple[DynamoItem, bytes],
    aws: tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]],
) -> None:
    dynamodb, s3, table, objects = aws
    result, _ = source
    projector = _projector(dynamodb, s3)
    put_object = s3.put_object.side_effect
    s3.put_object.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "PutObject")
    with pytest.raises(ClientError):
        projector.project_result(result)
    assert table[_SK]["stored"] == result["stored"]
    assert "thumbnail" not in table[_SK]
    s3.put_object.side_effect = put_object
    assert projector.project_result(result).created
    assert "thumbnail" in table[_SK]
    assert len(objects) == 1


def test_concurrent_s3_write_is_recovered_and_backfill_is_bounded(
    source: tuple[DynamoItem, bytes],
    aws: tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]],
) -> None:
    dynamodb, s3, table, objects = aws
    result, _ = source
    student = cast("DynamoItem", result["student"])
    del student["rarity"]
    dynamodb.query.return_value["Items"] = [marshal_item(result)]
    projector = _projector(dynamodb, s3)
    deadline = datetime.now(UTC) + timedelta(minutes=15)
    assert projector.backfill_page(apply=False, limit=10, cursor=None, deadline=deadline) == {
        "candidates": 1,
        "validated": 1,
        "projected": 0,
        "skipped": 0,
        "failed": 0,
        "complete": False,
        "next_cursor": _SK,
    }
    assert not table and not objects

    put = s3.put_object.side_effect

    def race(**kwargs: Any) -> dict[str, str]:
        put(**kwargs)
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

    s3.put_object.side_effect = race
    assert (
        projector.backfill_page(apply=True, limit=10, cursor=_SK, deadline=deadline)["projected"]
        == 1
    )
    assert dynamodb.query.call_args.kwargs["ExclusiveStartKey"] == marshal_item(
        {"PK": f"GACHA#GUILD#{_GUILD}", "SK": _SK}
    )
    assert "rarity" not in table[_SK]
    assert "thumbnail" in table[_SK]
    assert len(objects) == 1
    s3.head_object.assert_called_once()
    assert not projector.project_result({**result, "PK": f"GACHA#GUILD#{'2' * 17}"}).created
    assert len(table) == 1


def test_backfill_handler_returns_cursor_without_logging_it(
    aws: tuple[Mock, Mock, dict[str, DynamoItem], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dynamodb, s3, _, _ = aws
    monkeypatch.setattr(lambda_handlers, "_GALLERY_PROJECTOR", _projector(dynamodb, s3))
    response = lambda_handlers.backfill_handler(
        {"target": "student-gacha"},
        SimpleNamespace(get_remaining_time_in_millis=lambda: 15 * 60 * 1_000),
    )
    assert response["next_cursor"] == _SK
    assert "records_gallery_backfill_page" in caplog.text
    assert _SK not in caplog.text
    assert dynamodb.query.call_args.kwargs["Limit"] == 25

    response = lambda_handlers.backfill_handler(
        {"target": "student-gacha", "mode": "apply", "cursor": _SK},
        SimpleNamespace(get_remaining_time_in_millis=lambda: 60 * 1_000),
    )
    assert response["next_cursor"] == _SK
    assert response["complete"] is False
    assert response["candidates"] == 0
    dynamodb.put_item.assert_not_called()


def test_stream_routes_only_confirmed_result_inserts(
    source: tuple[DynamoItem, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = source
    projector = Mock()
    monkeypatch.setattr(lambda_handlers, "_GALLERY_PROJECTOR", projector)
    image = marshal_item(result)
    dynamodb = {"Keys": {key: image[key] for key in ("PK", "SK")}, "NewImage": image}
    assert lambda_handlers.projector_handler(
        {
            "Records": [
                {"eventName": "INSERT", "dynamodb": {**dynamodb, "SequenceNumber": "1"}},
                {"eventName": "MODIFY", "dynamodb": {**dynamodb, "SequenceNumber": "2"}},
            ]
        },
        object(),
    ) == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    projector.project_result.assert_called_once_with(result)
