from __future__ import annotations

import base64
import hashlib
import io
import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from shittim_chest.adapters.assets.student_gacha import (
    RESULT_SIZE,
    StudentGachaMedia,
    _parse_roster,
)
from shittim_chest.application.student_gacha_models import (
    GachaGenerationFailure,
    normalize_teacher_name,
    student_gacha_image_key,
)

_DRAW_ID = "0" * 32


@pytest.mark.asyncio
async def test_save_reconciles_lost_put_response_and_loads_verified_version() -> None:
    raw = io.BytesIO()
    Image.new("RGB", RESULT_SIZE, "lavender").save(raw, "PNG")
    png = raw.getvalue()
    checksum = base64.b64encode(hashlib.sha256(png).digest()).decode("ascii")
    client = Mock()
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    client.head_object.return_value = {
        "ContentType": "image/png",
        "ContentLength": len(png),
        "ChecksumSHA256": checksum,
        "VersionId": "original-version",
    }
    client.get_object.return_value = {"ContentLength": len(png), "Body": io.BytesIO(png)}
    media = StudentGachaMedia(client, bucket_name="private-media")

    stored = await media.save(_DRAW_ID, png)

    assert stored.sha256 == hashlib.sha256(png).hexdigest()
    assert stored.key == student_gacha_image_key(_DRAW_ID)
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert client.put_object.call_args.kwargs["ChecksumSHA256"] == checksum
    assert await media.load(stored) == png
    assert client.get_object.call_args.kwargs["VersionId"] == "original-version"


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
    client.list_objects_v2.assert_called_once_with(
        Bucket="private-media", Prefix=student_gacha_image_key(_DRAW_ID), MaxKeys=1
    )

    client.list_objects_v2.return_value = {"Contents": [{"Key": student_gacha_image_key(_DRAW_ID)}]}
    with pytest.raises(GachaGenerationFailure, match="gacha_result_lookup_failed"):
        await media.find(_DRAW_ID)


def _roster_rows() -> list[dict[str, object]]:
    names = ("リン", "アコ", "アコ\uff08ドレス\uff09", "キキョウ", "ヒナ")
    return [
        {"name": name, "school": "ゲヘナ", "rarity": rarity} for rarity, name in enumerate(names)
    ]


def test_roster_keeps_npcs_costume_variants_and_rarity() -> None:
    rows = _roster_rows()
    entries = _parse_roster(json.dumps(rows).encode())

    assert [(entry.name, entry.rarity) for entry in entries] == [
        (row["name"], row["rarity"]) for row in rows
    ]


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
