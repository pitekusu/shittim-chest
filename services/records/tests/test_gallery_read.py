"""Exercise private gallery routes with the production read service and SDK adapter."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode
from zipfile import ZipFile

import pytest
from PIL import Image
from shittim_chest.adapters.assets.student_gacha import _ASSET_DIGESTS, SCHOOL_LOGOS_KEY
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from tests.test_http_api import NOW, SESSION_KEY, FakeSessionStore, event, session

from shittim_records.auth import SESSION_COOKIE_NAME, AuthFailure, AuthStore, validate_return_to
from shittim_records.gallery_read import GalleryReadService
from shittim_records.gallery_read_adapters import DynamoGalleryStore
from shittim_records.http_api import ReadHttpController
from shittim_records.read_api import RecordsReadService, RequesterProfile

ROOT = "GET /api/v1/gallery"
GUILD = "1" * 18
PARTITION = f"GACHA#GALLERY#{GUILD}"
POSTED = "2026-08-17T11:00:00.000000Z"
OFFER = "a" * 32
SHA256 = "b" * 64
COOKIES = [f"{SESSION_COOKIE_NAME}=session-token"]
VIEWER = replace(
    session(), requester_key="r" * 43, expires_at=int((NOW + timedelta(days=1)).timestamp())
)


def card_id(offer: str = OFFER) -> str:
    return base64.urlsafe_b64encode(f"{POSTED}#{offer}".encode()).decode()


def item(offer: str = OFFER, *, ready: bool = True, rarity: int | None = 4) -> dict[str, Any]:
    row = {
        "PK": PARTITION,
        "SK": f"RESULT#{POSTED}#{offer}",
        "schema_version": 1,
        "record_type": "student_gacha_gallery",
        "posted_at": POSTED,
        "offer_id": offer,
        "performer": "participant-c",
        "student_name": "ハルカ",
        "school_name": "ゲヘナ学園",
        "owner_key": VIEWER.requester_key if offer == OFFER else "k" * 43,
        "collector_display_name": "保存時の名前",
        "stored": {
            "key": f"student-gacha/results/{offer}.png",
            "version_id": "original-version",
            "sha256": SHA256,
        },
    }
    if rarity is not None:
        row["rarity"] = rarity
    if ready:
        row["thumbnail"] = {
            "key": f"student-gacha/thumbnails/v1/{offer}/{SHA256}.webp",
            "version_id": "thumbnail-version",
            "sha256": "c" * 64,
            "width": 768,
            "height": 355,
        }
    return row


class Dynamo:
    def __init__(self) -> None:
        self.first = item()
        self.second = item("d" * 32, ready=False, rarity=None)
        self.requests: list[dict[str, Any]] = []
        self.batch_attempts = 0

    def query(self, **arguments: Any) -> dict[str, Any]:
        self.requests.append(arguments)
        if arguments.get("ExclusiveStartKey") is not None:
            assert unmarshal_item(arguments["ExclusiveStartKey"]) == {
                "PK": PARTITION,
                "SK": self.first["SK"],
            }
            return {"Items": [marshal_item(self.second)]}
        return {
            "Items": [marshal_item(self.first)],
            "LastEvaluatedKey": marshal_item({"PK": PARTITION, "SK": self.first["SK"]}),
        }

    def get_item(self, **arguments: Any) -> dict[str, Any]:
        self.requests.append(arguments)
        key = unmarshal_item(arguments["Key"])
        assert key["PK"] == PARTITION
        if key["SK"] == self.first["SK"]:
            return {"Item": marshal_item(self.first)}
        return {}

    def batch_get_item(self, **arguments: Any) -> dict[str, Any]:
        self.requests.append(arguments)
        self.batch_attempts += 1
        keys = arguments["RequestItems"]["stats"]["Keys"]
        assert all(unmarshal_item(key)["PK"] == PARTITION for key in keys)
        if self.batch_attempts == 1:
            return {
                "Responses": {"stats": [marshal_item(self.second)]},
                "UnprocessedKeys": {"stats": {"Keys": keys[:1]}},
            }
        return {"Responses": {"stats": [marshal_item(self.first)]}}


class Profiles:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def load_profiles(self, *, requester_keys: tuple[str, ...]) -> dict[str, RequesterProfile]:
        return {
            key: RequesterProfile("現在の名前", f"requesters/{key}/avatar.webp")
            for key in requester_keys
        }

    def avatar_url(self, *, asset_key: str) -> str:
        self.requests.append(asset_key)
        return "https://media.example.invalid/avatar"


class Signer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.catalog: bytes = b""
        self.catalog_reads = 0

    def generate_presigned_url(self, operation: str, **arguments: Any) -> str:
        assert operation == "get_object"
        self.requests.append(arguments)
        return "https://media.example.invalid/signed"

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == "media" and Key == SCHOOL_LOGOS_KEY
        self.catalog_reads += 1
        return {"Body": io.BytesIO(self.catalog), "ContentLength": len(self.catalog)}


@pytest.fixture
def setup() -> tuple[ReadHttpController, Dynamo, Signer, GalleryReadService]:
    dynamo = Dynamo()
    signer = Signer()
    store = DynamoGalleryStore(cast(Any, dynamo), cast(Any, signer), table="stats", bucket="media")
    gallery = GalleryReadService(store, Profiles(), guild_id=GUILD, session_key=SESSION_KEY)
    controller = ReadHttpController(
        store=cast(AuthStore, FakeSessionStore(VIEWER)),
        session_key=SESSION_KEY,
        records=cast(RecordsReadService, SimpleNamespace()),
        gallery=lambda: gallery,
    )
    return controller, dynamo, signer, gallery


def test_gallery_metadata_is_private_paginated_and_contains_no_signed_urls(setup: Any) -> None:
    controller, dynamo, signer, gallery = setup
    assert validate_return_to("/gallery") == "/gallery"
    assert validate_return_to(f"/gallery/{card_id()}") == f"/gallery/{card_id()}"
    with pytest.raises(AuthFailure, match="return_to_invalid"):
        validate_return_to(f"/gallery/{card_id()}?redirect=outside")
    assert controller.handle(event(ROOT), now=NOW)["statusCode"] == 401
    assert (
        controller.handle(event(ROOT, headers={"authorization": f"Bearer {'t' * 43}"}), now=NOW)[
            "statusCode"
        ]
        == 401
    )
    assert (
        controller.handle(event(ROOT, query="guild=other", cookies=COOKIES), now=NOW)["statusCode"]
        == 400
    )
    assert not dynamo.requests

    first = controller.handle(event(ROOT, cookies=COOKIES), now=NOW)
    assert first["headers"]["Cache-Control"] == "private, no-store"
    data = json.loads(first["body"])
    assert data["schemaVersion"] == 1
    card = data["items"][0]
    assert card["cardId"] == card_id()
    assert card["performer"] == "participant-c"
    assert card["schoolName"] == "ゲヘナ学園"
    assert card["isMine"] is True and card["thumbnailReady"] is True
    assert card["collector"]["displayName"] == VIEWER.display_name
    assert card["collector"]["id"] != VIEWER.requester_key
    assert "avatar" not in card["collector"]
    assert "url" not in card and "ownerKey" not in card
    assert dynamo.requests[0]["ScanIndexForward"] is False
    assert unmarshal_item(dynamo.requests[0]["ExpressionAttributeValues"])[":pk"] == PARTITION
    cursor = data["nextCursor"]

    second = controller.handle(
        event(ROOT, query=urlencode({"cursor": cursor}), cookies=COOKIES), now=NOW
    )
    second_card = json.loads(second["body"])["items"][0]
    assert second_card["rarity"] is None and second_card["thumbnailReady"] is False
    assert second_card["collector"]["displayName"] == "現在の名前"
    assert "avatar" not in second_card["collector"]
    assert second_card["isMine"] is False
    assert not signer.requests
    expired = controller.handle(
        event(ROOT, query=urlencode({"cursor": cursor}), cookies=COOKIES),
        now=NOW + timedelta(hours=1),
    )
    assert expired["statusCode"] == 400
    other_guild = GalleryReadService(
        gallery._store, Profiles(), guild_id="2" * 18, session_key=SESSION_KEY
    )
    with pytest.raises(RuntimeError, match="CURSOR_INVALID"):
        other_guild.list_cards(cursor=cursor, viewer=session(), now=NOW)


def test_gallery_avatar_contract_keeps_existing_client_shape_and_signs_only_owned_icons(
    setup: Any,
) -> None:
    controller, dynamo, _, gallery = setup
    profiles = cast(Profiles, gallery._profiles)
    query = "contract=gallery-avatars-v1"
    legacy = controller.handle(event(ROOT, cookies=COOKIES), now=NOW)
    assert "avatar" not in json.loads(legacy["body"])["items"][0]["collector"]
    assert not profiles.requests

    detail = controller.handle(
        event(f"{ROOT}/{{cardId}}", query=query, path={"cardId": card_id()}, cookies=COOKIES),
        now=NOW,
    )
    assert json.loads(detail["body"])["card"]["collector"]["avatar"]["kind"] == "placeholder"
    assert not profiles.requests

    cursor = json.loads(legacy["body"])["nextCursor"]
    second = controller.handle(
        event(ROOT, query=f"{query}&cursor={cursor}", cookies=COOKIES), now=NOW
    )
    collector = json.loads(second["body"])["items"][0]["collector"]
    assert collector["avatar"]["kind"] == "image"
    assert collector["avatar"]["url"] == "https://media.example.invalid/avatar"
    assert profiles.requests == [f"requesters/{dynamo.second['owner_key']}/avatar.webp"]
    assert (
        controller.handle(event(ROOT, query="contract=unknown", cookies=COOKIES), now=NOW)[
            "statusCode"
        ]
        == 400
    )


def test_school_logos_require_browser_auth_and_return_only_validated_catalog_icons(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _, signer, _ = setup
    route = f"{ROOT}/school-logos"
    with Image.new("RGBA", (180, 90), "red") as original, io.BytesIO() as output:
        original.save(output, format="PNG")
        png = output.getvalue()
    with io.BytesIO() as output:
        with ZipFile(output, "w") as archive:
            archive.writestr(
                "blue_archive_school_logos/schools.json",
                json.dumps(
                    [
                        {
                            "id": "gehenna",
                            "name": "ゲヘナ学園",
                            "kind": "school",
                            "logo_path": "logos/gehenna.png",
                            "width": 180,
                            "height": 90,
                            "format": "PNG",
                            "sha256": hashlib.sha256(png).hexdigest(),
                        },
                        {
                            "id": "no-logo",
                            "name": "ロゴ未登録",
                            "kind": "school",
                            "logo_path": None,
                        },
                    ]
                ),
            )
            archive.writestr("blue_archive_school_logos/logos/gehenna.png", png)
        signer.catalog = output.getvalue()
    assert controller.handle(event(route), now=NOW)["statusCode"] == 401
    assert signer.catalog_reads == 0
    assert controller.handle(event(route, cookies=COOKIES), now=NOW)["statusCode"] == 503
    monkeypatch.setitem(
        _ASSET_DIGESTS, SCHOOL_LOGOS_KEY, hashlib.sha256(signer.catalog).hexdigest()
    )

    response = controller.handle(event(route, cookies=COOKIES), now=NOW)
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    payload = json.loads(response["body"])
    assert set(payload["logos"]) == {"ゲヘナ学園"}
    prefix, data = payload["logos"]["ゲヘナ学園"].split(",", 1)
    assert prefix == "data:image/webp;base64"
    with Image.open(io.BytesIO(base64.b64decode(data)), formats=["WEBP"]) as logo:
        assert logo.size == (64, 32)
    assert controller.handle(event(route, cookies=COOKIES), now=NOW) == response
    assert signer.catalog_reads == 2


def test_gallery_signs_only_requested_ready_thumbnails_or_explicit_original(setup: Any) -> None:
    controller, dynamo, signer, _ = setup
    detail = controller.handle(
        event(f"{ROOT}/{{cardId}}", path={"cardId": card_id()}, cookies=COOKIES), now=NOW
    )
    data = json.loads(detail["body"])
    assert (data["width"], data["height"]) == (1792, 828)
    assert not signer.requests

    thumbnails = controller.handle(
        event(
            f"{ROOT}/thumbnails",
            query=urlencode({"ids": f"{card_id()},{card_id('d' * 32)}"}),
            cookies=COOKIES,
        ),
        now=NOW,
    )
    assert dynamo.batch_attempts == 2
    assert json.loads(thumbnails["body"])["items"] == [
        {
            "cardId": card_id(),
            "url": "https://media.example.invalid/signed",
            "expiresAt": "2026-08-17T12:05:00Z",
        }
    ]
    assert signer.requests[-1] == {
        "Params": {
            "Bucket": "media",
            "Key": dynamo.first["thumbnail"]["key"],
            "VersionId": "thumbnail-version",
            "ResponseContentType": "image/webp",
            "ResponseCacheControl": "private, no-store",
        },
        "ExpiresIn": 300,
    }
    original = controller.handle(
        event(f"{ROOT}/{{cardId}}/original", path={"cardId": card_id()}, cookies=COOKIES), now=NOW
    )
    assert original["statusCode"] == 200
    assert signer.requests[-1]["Params"]["VersionId"] == "original-version"
    assert signer.requests[-1]["Params"]["ResponseContentType"] == "image/png"

    missing = controller.handle(
        event(f"{ROOT}/{{cardId}}/original", path={"cardId": card_id("e" * 32)}, cookies=COOKIES),
        now=NOW,
    )
    assert missing["statusCode"] == 404
    assert len(signer.requests) == 2


@pytest.mark.parametrize("field,value", [("performer", "other"), ("rarity", True), ("PK", "other")])
def test_invalid_projection_cannot_be_used_to_sign_an_image(
    setup: Any, field: str, value: Any
) -> None:
    controller, dynamo, signer, _ = setup
    dynamo.first[field] = value
    response = controller.handle(
        event(f"{ROOT}/{{cardId}}/original", path={"cardId": card_id()}, cookies=COOKIES), now=NOW
    )
    assert response["statusCode"] == 503
    assert not signer.requests
