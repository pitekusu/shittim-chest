"""Read the gallery projected from confirmed Discord posts."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from shittim_chest.adapters.assets.student_gacha import RESULT_SIZE
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.application.student_gacha_models import student_gacha_image_key

from shittim_records.auth import SessionRecord, sign_session_message
from shittim_records.contracts import (
    GALLERY_CARD_ID_PATTERN,
    GalleryCard,
    GalleryCollector,
    GalleryDetailResponse,
    GalleryListResponse,
    GalleryOriginalResponse,
    GalleryThumbnail,
    GalleryThumbnailsResponse,
    NonEmptyText,
    ParticipantSlot,
    RecordId,
)
from shittim_records.gallery_keys import (
    GALLERY_RECORD_TYPE,
    GALLERY_RESULT_KEY,
    GALLERY_RESULT_PREFIX,
    GALLERY_THUMBNAIL_PREFIX,
    GALLERY_THUMBNAIL_SIZE,
)
from shittim_records.read_api import CursorCodec, ReadFailure, RequesterProfile

GALLERY_PAGE_SIZE = 100
GALLERY_THUMBNAIL_BATCH_SIZE = 24
GALLERY_IMAGE_URL_TTL_SECONDS = 300
_CARD_ID = re.compile(GALLERY_CARD_ID_PATTERN)


class GalleryAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str
    version_id: Annotated[str, Field(min_length=1, max_length=1024)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @model_validator(mode="after")
    def validate_version(self) -> GalleryAsset:
        if self.version_id == "null" or not self.version_id.isprintable():
            raise ValueError("invalid gallery asset version")
        return self


class GalleryThumbnailAsset(GalleryAsset):
    width: int
    height: int


class GalleryProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pk: str = Field(alias="PK")
    sk: str = Field(alias="SK")
    schema_version: Literal[1]
    record_type: str
    offer_id: str
    posted_at: str
    performer: ParticipantSlot
    student_name: Annotated[str, Field(min_length=1, max_length=60)]
    school_name: Annotated[str, Field(min_length=1, max_length=40)]
    rarity: Annotated[int, Field(ge=0, le=4)] | None = None
    owner_key: RecordId | None = None
    collector_display_name: NonEmptyText | None = None
    stored: GalleryAsset
    thumbnail: GalleryThumbnailAsset | None = None

    @model_validator(mode="after")
    def validate_keys(self) -> GalleryProjection:
        if (
            self.record_type != GALLERY_RECORD_TYPE
            or self.sk != f"{GALLERY_RESULT_PREFIX}{self.posted_at}#{self.offer_id}"
        ):
            raise ValueError("invalid gallery result key")
        _card_id(self.sk)
        if self.stored.key != student_gacha_image_key(self.offer_id):
            raise ValueError("invalid gallery original key")
        if self.thumbnail is not None and (
            self.thumbnail.key
            != f"{GALLERY_THUMBNAIL_PREFIX}{self.offer_id}/{self.stored.sha256}.webp"
            or (self.thumbnail.width, self.thumbnail.height) != GALLERY_THUMBNAIL_SIZE
        ):
            raise ValueError("invalid gallery thumbnail key")
        return self


class GalleryStore(Protocol):
    def list_page(
        self, partition: str, after: str | None
    ) -> tuple[list[DynamoItem], str | None]: ...

    def load_card(self, partition: str, sort_key: str) -> DynamoItem | None: ...

    def load_cards(self, partition: str, sort_keys: tuple[str, ...]) -> list[DynamoItem]: ...

    def sign(self, asset: GalleryAsset, *, thumbnail: bool) -> str: ...


class GalleryProfiles(Protocol):
    def load_profiles(self, *, requester_keys: tuple[str, ...]) -> dict[str, RequesterProfile]: ...


class GalleryReadService:
    def __init__(
        self, store: GalleryStore, profiles: GalleryProfiles, *, guild_id: str, session_key: bytes
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._partition = f"GACHA#GALLERY#{guild_id}"
        self._session_key = session_key
        self._cursor_codec = CursorCodec(session_key)

    def list_cards(
        self, *, cursor: str | None, viewer: SessionRecord, now: datetime
    ) -> GalleryListResponse:
        after = self._decode_cursor(cursor, now) if cursor is not None else None
        items, next_key = self._store.list_page(self._partition, after)
        rows = [self._projection(item) for item in items]
        return GalleryListResponse(
            schema_version=1,
            items=self._summaries(rows, viewer),
            next_cursor=None if next_key is None else self._encode_cursor(next_key, now),
        )

    def get_card(self, *, card_id: str, viewer: SessionRecord) -> GalleryDetailResponse:
        row = self._load_card(card_id)
        width, height = RESULT_SIZE
        return GalleryDetailResponse(
            schema_version=1, card=self._summaries([row], viewer)[0], width=width, height=height
        )

    def get_thumbnails(self, *, ids: tuple[str, ...], now: datetime) -> GalleryThumbnailsResponse:
        if not 1 <= len(ids) <= GALLERY_THUMBNAIL_BATCH_SIZE or len(set(ids)) != len(ids):
            raise ReadFailure("REQUEST_INVALID", 400)
        keys = tuple(
            _result_key(card_id, error_code="REQUEST_INVALID", status=400) for card_id in ids
        )
        rows = [self._projection(item) for item in self._store.load_cards(self._partition, keys)]
        thumbnails = {row.sk: row.thumbnail for row in rows if row.sk in keys}
        expires_at = now + timedelta(seconds=GALLERY_IMAGE_URL_TTL_SECONDS)
        return GalleryThumbnailsResponse(
            schema_version=1,
            items=[
                GalleryThumbnail(
                    card_id=card_id,
                    url=self._store.sign(asset, thumbnail=True),
                    expires_at=expires_at,
                )
                for card_id, key in zip(ids, keys, strict=True)
                if (asset := thumbnails.get(key)) is not None
            ],
        )

    def get_original(self, *, card_id: str, now: datetime) -> GalleryOriginalResponse:
        row = self._load_card(card_id)
        return GalleryOriginalResponse(
            schema_version=1,
            url=self._store.sign(row.stored, thumbnail=False),
            expires_at=now + timedelta(seconds=GALLERY_IMAGE_URL_TTL_SECONDS),
        )

    def _load_card(self, card_id: str) -> GalleryProjection:
        sort_key = _result_key(card_id, error_code="GALLERY_CARD_NOT_FOUND", status=404)
        item = self._store.load_card(self._partition, sort_key)
        if item is None:
            raise ReadFailure("GALLERY_CARD_NOT_FOUND", 404)
        row = self._projection(item)
        if row.sk != sort_key:
            raise ReadFailure("GALLERY_UNAVAILABLE", 503)
        return row

    def _projection(self, item: DynamoItem) -> GalleryProjection:
        try:
            row = GalleryProjection.model_validate(item)
        except ValidationError:
            raise ReadFailure("GALLERY_UNAVAILABLE", 503) from None
        if row.pk != self._partition:
            raise ReadFailure("GALLERY_UNAVAILABLE", 503)
        return row

    def _summaries(self, rows: list[GalleryProjection], viewer: SessionRecord) -> list[GalleryCard]:
        keys = tuple(
            dict.fromkeys(
                row.owner_key
                for row in rows
                if row.owner_key is not None and row.owner_key != viewer.requester_key
            )
        )
        profiles = self._profiles.load_profiles(requester_keys=keys) if keys else {}
        summaries = []
        for row in rows:
            owner = row.owner_key
            is_mine = owner == viewer.requester_key
            profile = profiles.get(owner) if owner is not None else None
            name = (
                viewer.display_name
                if is_mine
                else (profile.display_name if profile else row.collector_display_name)
            )
            collector = (
                GalleryCollector(
                    id=_digest(self._session_key, f"gallery:collector:{self._partition}:{owner}"),
                    display_name=name,
                )
                if owner is not None and name is not None
                else None
            )
            summaries.append(
                GalleryCard(
                    card_id=_card_id(row.sk),
                    posted_at=datetime.fromisoformat(row.posted_at),
                    performer=row.performer,
                    student_name=row.student_name,
                    school_name=row.school_name,
                    rarity=row.rarity,
                    collector=collector,
                    is_mine=is_mine,
                    thumbnail_ready=row.thumbnail is not None,
                )
            )
        return summaries

    def _encode_cursor(self, key: str, now: datetime) -> str:
        try:
            card_id = _card_id(key)
        except ValueError:
            raise ReadFailure("GALLERY_UNAVAILABLE", 503) from None
        return self._cursor_codec.encode_position(card_id, scope=self._partition, now=now)

    def _decode_cursor(self, cursor: str, now: datetime) -> str:
        card_id = self._cursor_codec.decode_position(cursor, scope=self._partition, now=now)
        return _result_key(card_id, error_code="CURSOR_INVALID", status=400)


def _card_id(sort_key: str) -> str:
    match = GALLERY_RESULT_KEY.fullmatch(sort_key)
    if match is None:
        raise ValueError("invalid gallery result key")
    datetime.fromisoformat(match[1])
    return base64.urlsafe_b64encode(sort_key.removeprefix(GALLERY_RESULT_PREFIX).encode()).decode()


def _result_key(card_id: str, *, error_code: str, status: int) -> str:
    if _CARD_ID.fullmatch(card_id) is None:
        raise ReadFailure(error_code, status)
    try:
        key = GALLERY_RESULT_PREFIX + base64.urlsafe_b64decode(card_id).decode("ascii")
        if _card_id(key) == card_id:
            return key
    except ValueError:
        pass
    raise ReadFailure(error_code, status)


def _digest(key: bytes, value: str) -> str:
    digest = sign_session_message(key, value)
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
