"""Exercise private gallery routes with the production read service and SDK adapter."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlencode
from zipfile import ZipFile

import pytest
from PIL import Image
from shittim_chest.adapters.assets.student_gacha import _ASSET_DIGESTS, ROSTER_KEY, SCHOOL_LOGOS_KEY
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application.student_gacha_models import GachaImageFormat, student_gacha_image_key
from tests.test_http_api import NOW, SESSION_KEY, FakeSessionStore, event, session
from tests.test_mobile_auth_adapters import mobile_session
from tests.test_mobile_read import BEARER, DIGEST
from tests.test_mobile_session import SessionStore

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


def item(
    offer: str = OFFER,
    *,
    ready: bool = True,
    rarity: int | None = 4,
    image_format: GachaImageFormat = GachaImageFormat.PNG,
) -> dict[str, Any]:
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
            "key": student_gacha_image_key(offer, image_format),
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
        self.roster: bytes = b""
        self.roster_reads = 0
        self.roster_version = "roster-version"

    def generate_presigned_url(self, operation: str, **arguments: Any) -> str:
        assert operation == "get_object"
        self.requests.append(arguments)
        return "https://media.example.invalid/signed"

    def get_object(
        self, *, Bucket: str, Key: str, ChecksumMode: str | None = None
    ) -> dict[str, Any]:
        assert Bucket == "media"
        if Key == ROSTER_KEY:
            assert ChecksumMode == "ENABLED"
            self.roster_reads += 1
            return {
                "Body": io.BytesIO(self.roster),
                "ContentLength": len(self.roster),
                "ContentType": "application/json",
                "VersionId": self.roster_version,
                "ChecksumType": "FULL_OBJECT",
                "ChecksumSHA256": base64.b64encode(hashlib.sha256(self.roster).digest()).decode(),
            }
        assert Key == SCHOOL_LOGOS_KEY
        self.catalog_reads += 1
        return {"Body": io.BytesIO(self.catalog), "ContentLength": len(self.catalog)}


@pytest.fixture
def mobile_store() -> SessionStore:
    store = SessionStore()
    store.sessions[DIGEST] = mobile_session(now_epoch=int(NOW.timestamp()))
    return store


@pytest.fixture
def setup(
    mobile_store: SessionStore,
) -> tuple[ReadHttpController, Dynamo, Signer, GalleryReadService]:
    dynamo = Dynamo()
    signer = Signer()
    store = DynamoGalleryStore(cast(Any, dynamo), cast(Any, signer), table="stats", bucket="media")
    gallery = GalleryReadService(store, Profiles(), guild_id=GUILD, session_key=SESSION_KEY)
    controller = ReadHttpController(
        store=cast(AuthStore, FakeSessionStore(VIEWER)),
        session_key=SESSION_KEY,
        records=cast(RecordsReadService, SimpleNamespace()),
        gallery=lambda: gallery,
        mobile_store=mobile_store,
        clock=lambda: NOW,
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
        controller.handle(event(ROOT, headers={"authorization": f"Bearer {'u' * 43}"}), now=NOW)[
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
    assert "contract" not in data and "studentId" not in card
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


def test_gallery_mobile_session_reads_shared_collection_and_signs_images(
    setup: Any, mobile_store: SessionStore
) -> None:
    controller, _, signer, _ = setup
    signer.roster = json.dumps(
        [
            {"name": name, "school": "ゲヘナ", "rarity": rarity}
            for rarity, name in enumerate(("名無しの生徒", "カヨコ", "ムツキ", "アル", "ハルカ"))
        ]
    ).encode()
    path = {"cardId": card_id()}
    query = "contract=gallery-collection-v1"
    roster = controller.handle(event(f"{ROOT}/roster", headers=BEARER), now=NOW)
    assert roster["statusCode"] == 200
    assert roster == controller.handle(event(f"{ROOT}/roster", cookies=COOKIES), now=NOW)
    slot = next(entry for entry in json.loads(roster["body"])["items"] if entry["rarity"] == 4)
    assert slot["studentName"] is None and slot["schoolName"] is None

    collection = controller.handle(event(ROOT, query=query, headers=BEARER), now=NOW)
    browser = controller.handle(event(ROOT, query=query, cookies=COOKIES), now=NOW)
    assert collection["statusCode"] == browser["statusCode"] == 200
    assert collection["headers"]["Cache-Control"] == "private, no-store"
    assert "cookies" not in collection
    card = json.loads(collection["body"])["items"][0]
    browser_card = json.loads(browser["body"])["items"][0]
    assert card["studentId"] == browser_card["studentId"] == slot["studentId"]
    assert card["collector"]["id"] == browser_card["collector"]["id"]
    assert card["collector"]["displayName"] == mobile_store.sessions[DIGEST].display_name
    assert browser_card["collector"]["displayName"] == VIEWER.display_name
    assert card["isMine"] is True and browser_card["isMine"] is True
    assert VIEWER.requester_key not in collection["body"]

    detail = controller.handle(event(f"{ROOT}/{{cardId}}", path=path, headers=BEARER), now=NOW)
    assert detail["statusCode"] == 200
    assert json.loads(detail["body"])["card"]["isMine"] is True
    thumbnails = controller.handle(
        event(f"{ROOT}/thumbnails", query=f"ids={card_id()}", headers=BEARER), now=NOW
    )
    assert thumbnails["statusCode"] == 200
    assert json.loads(thumbnails["body"])["items"][0]["cardId"] == card_id()
    original = controller.handle(
        event(f"{ROOT}/{{cardId}}/original", path=path, headers=BEARER), now=NOW
    )
    assert original["statusCode"] == 200
    assert len(signer.requests) == 2


def test_gallery_rejects_mixed_expired_and_revoked_sessions_before_signing(
    setup: Any, mobile_store: SessionStore
) -> None:
    controller, dynamo, signer, _ = setup
    route = f"{ROOT}/{{cardId}}/original"
    path = {"cardId": card_id()}
    mixed = controller.handle(event(route, path=path, headers=BEARER, cookies=COOKIES), now=NOW)
    assert mixed["statusCode"] == 400
    assert not mobile_store.reads
    original = event(route, path=path, headers=BEARER)
    controller._clock = lambda: datetime.fromtimestamp(
        mobile_store.sessions[DIGEST].expires_at, UTC
    )
    expired = controller.handle(original, now=NOW)
    assert expired["statusCode"] == 401
    assert expired["headers"]["WWW-Authenticate"] == "Bearer"
    controller._clock = lambda: NOW
    mobile_store.delete_session(session_hash=DIGEST)
    assert controller.handle(original, now=NOW)["statusCode"] == 401
    assert not dynamo.requests and not signer.requests


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
    avatar_list = json.loads(second["body"])
    assert "contract" not in avatar_list and "studentId" not in avatar_list["items"][0]
    collector = avatar_list["items"][0]["collector"]
    assert collector["avatar"]["kind"] == "image"
    assert collector["avatar"]["url"] == "https://media.example.invalid/avatar"
    assert profiles.requests == [f"requesters/{dynamo.second['owner_key']}/avatar.webp"]
    assert (
        controller.handle(event(ROOT, query="contract=unknown", cookies=COOKIES), now=NOW)[
            "statusCode"
        ]
        == 400
    )


def test_gallery_roster_masks_secrets_and_joins_confirmed_cards_on_every_page(setup: Any) -> None:
    controller, dynamo, signer, gallery = setup
    route = f"{ROOT}/roster"
    new_year = "ハルカ\uff08正月\uff09"
    uniform = "ハルカ\uff08制服\uff09"
    rows = [
        {"name": name, "school": school, "rarity": rarity}
        for name, school, rarity in [
            ("名無しの生徒", "シッテムの箱", 0),
            (new_year, "ゲヘナ", 1),
            ("カヨコ", "ゲヘナ学園", 2),
            ("アル", "未登録の学校", 3),
            ("ハルカ", "シッテムの箱", 4),
        ]
    ]
    signer.roster = json.dumps(rows).encode()
    assert controller.handle(event(route), now=NOW)["statusCode"] == 401
    assert (
        controller.handle(event(route, query="guild=other", cookies=COOKIES), now=NOW)["statusCode"]
        == 400
    )
    assert signer.roster_reads == 0

    response = controller.handle(event(route, cookies=COOKIES), now=NOW)
    assert response["statusCode"] == 200
    assert response["headers"]["Cache-Control"] == "private, no-store"
    roster = json.loads(response["body"])
    assert roster["schemaVersion"] == 1
    slots = {entry["rarity"]: entry for entry in roster["items"]}
    assert {rarity: entry["studentName"] for rarity, entry in slots.items()} == {
        0: None,
        1: new_year,
        2: "カヨコ",
        3: "アル",
        4: None,
    }
    assert {rarity: entry["schoolName"] for rarity, entry in slots.items()} == {
        0: None,
        1: "ゲヘナ学園",
        2: "ゲヘナ学園",
        3: "未登録の学校",
        4: None,
    }
    assert len({entry["studentId"] for entry in slots.values()}) == 5
    fields = {"studentId", "studentName", "schoolName", "rarity"}
    assert all(set(entry) == fields for entry in slots.values())
    assert not dynamo.requests

    other_guild = GalleryReadService(
        gallery._store, Profiles(), guild_id="2" * 18, session_key=SESSION_KEY
    )
    assert {entry.student_id for entry in other_guild.get_roster().items}.isdisjoint(
        {entry["studentId"] for entry in slots.values()}
    )

    # Existing results can have a different owner and no recorded rarity.
    query = "contract=gallery-collection-v1"
    first = json.loads(
        controller.handle(event(ROOT, query=query, cookies=COOKIES), now=NOW)["body"]
    )
    second = json.loads(
        controller.handle(
            event(ROOT, query=f"{query}&cursor={first['nextCursor']}", cookies=COOKIES), now=NOW
        )["body"]
    )
    assert first["contract"] == second["contract"] == "gallery-collection-v1"
    assert (
        first["items"][0]["studentId"] == second["items"][0]["studentId"] == slots[4]["studentId"]
    )
    assert first["items"][0]["collector"]["avatar"]["kind"] == "placeholder"
    assert second["items"][0]["isMine"] is False
    assert second["items"][0]["rarity"] is None
    assert not signer.requests

    # The published roster can change while the read Lambda remains warm.
    rows[1]["name"] = uniform
    signer.roster = json.dumps(rows[::-1]).encode()
    refreshed = controller.handle(event(route, cookies=COOKIES), now=NOW)
    updated = {entry["rarity"]: entry for entry in json.loads(refreshed["body"])["items"]}
    assert updated[1]["studentName"] == uniform
    assert updated[1]["studentId"] != slots[1]["studentId"]
    assert updated[4] == slots[4]

    signer.roster_version = "null"
    failure = controller.handle(event(route, cookies=COOKIES), now=NOW)
    assert failure["statusCode"] == 503
    assert json.loads(failure["body"])["error"]["code"] == "GALLERY_UNAVAILABLE"


def test_school_logos_require_auth_and_return_only_validated_catalog_icons(
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
                            "id": "mnc",
                            "name": "MNC",
                            "kind": "organization",
                            "logo_path": "logos/mnc.png",
                            "width": 180,
                            "height": 90,
                            "format": "PNG",
                            "sha256": hashlib.sha256(png).hexdigest(),
                            "gallery_listed": False,
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
            archive.writestr("blue_archive_school_logos/logos/mnc.png", png)
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
    assert controller.handle(event(route, headers=BEARER), now=NOW) == response
    assert signer.catalog_reads == 2


@pytest.mark.parametrize(
    "image_format,width,height,content_type",
    [
        (GachaImageFormat.PNG, 1792, 828, "image/png"),
        (GachaImageFormat.GIF, 1024, 473, "image/gif"),
        (GachaImageFormat.APNG, 1024, 473, "image/apng"),
    ],
)
def test_gallery_signs_only_requested_ready_thumbnails_or_explicit_original(
    setup: Any, image_format: GachaImageFormat, width: int, height: int, content_type: str
) -> None:
    controller, dynamo, signer, _ = setup
    original_key = student_gacha_image_key(OFFER, image_format)
    dynamo.first["stored"]["key"] = original_key
    detail = controller.handle(
        event(f"{ROOT}/{{cardId}}", path={"cardId": card_id()}, cookies=COOKIES), now=NOW
    )
    data = json.loads(detail["body"])
    assert (data["width"], data["height"]) == (width, height)
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
    assert signer.requests[-1] == {
        "Params": {
            "Bucket": "media",
            "Key": original_key,
            "VersionId": "original-version",
            "ResponseContentType": content_type,
            "ResponseCacheControl": "private, no-store",
        },
        "ExpiresIn": 300,
    }

    missing = controller.handle(
        event(f"{ROOT}/{{cardId}}/original", path={"cardId": card_id("e" * 32)}, cookies=COOKIES),
        now=NOW,
    )
    assert missing["statusCode"] == 404
    missing_detail = controller.handle(
        event(f"{ROOT}/{{cardId}}", path={"cardId": card_id("e" * 32)}, cookies=COOKIES),
        now=NOW,
    )
    assert missing_detail["statusCode"] == 404
    assert json.loads(missing_detail["body"])["error"]["code"] == "GALLERY_CARD_NOT_FOUND"
    assert len(signer.requests) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("performer", "other"),
        ("rarity", True),
        ("PK", "other"),
        ("stored", item("e" * 32, image_format=GachaImageFormat.GIF)["stored"]),
    ],
)
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
