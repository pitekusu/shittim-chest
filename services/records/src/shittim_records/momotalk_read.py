"""Authenticated, publication-gated projection of MomoTalk checkpoints."""

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from shittim_records.contracts import (
    AvatarRef,
    ImageAvatarRef,
    MomotalkImage,
    MomotalkMessage,
    MomotalkRoomResponse,
    MomotalkRoomsResponse,
    MomotalkRoomSummary,
    MomotalkWeek,
    MomotalkWeeksResponse,
    ParticipantSummary,
    PlaceholderAvatarRef,
    RequesterSummary,
)
from shittim_records.momotalk import (
    PARTICIPANT_NAMES,
    PARTICIPANTS,
    TOKYO,
    MomotalkFailure,
    Room,
    validate_week_id,
)

if TYPE_CHECKING:
    from shittim_records.momotalk_adapters import DynamoMomotalkStore, MomotalkAssets
    from shittim_records.read_adapters import DynamoRecordsReader


class MomotalkReadService:
    def __init__(
        self, store: DynamoMomotalkStore, assets: MomotalkAssets, reader: DynamoRecordsReader
    ) -> None:
        self.store = store
        self.assets = assets
        self.reader = reader

    def list_weeks(self, *, limit: int, cursor: str | None, now: datetime) -> MomotalkWeeksResponse:
        if cursor:
            validate_week_id(cursor)
        items, next_cursor = self.store.page(
            "MOMOTALK#WEEKS",
            cursor=cursor,
            limit=limit,
            reverse=True,
            before=str(now.astimezone(TOKYO).date()),
        )
        weeks = [MomotalkWeek.model_validate(item["week"]) for item in items]
        return MomotalkWeeksResponse(
            weeks=[week for week in weeks if week.publish_at <= now], next_cursor=next_cursor
        )

    def _week(self, week_id: str, now: datetime) -> MomotalkWeek:
        identifier = validate_week_id(week_id)
        item = self.store.get_week(identifier)
        if item is None:
            raise MomotalkFailure("MOMOTALK_NOT_FOUND", 404)
        week = MomotalkWeek.model_validate(item["week"])
        if week.week_id != identifier or week.publish_at > now:
            raise MomotalkFailure("MOMOTALK_NOT_FOUND", 404)
        return week

    def list_rooms(
        self, *, week_id: str, limit: int, cursor: str | None, now: datetime
    ) -> MomotalkRoomsResponse:
        week = self._week(week_id, now)
        if cursor:
            self._room_id(cursor)
        items, next_cursor = self.store.page(
            f"MOMOTALK#WEEK#{week.week_id}", cursor=cursor, limit=limit
        )
        rooms = [self.store.decode_room(item) for item in items]
        return MomotalkRoomsResponse(
            week=week,
            rooms=[self._summary(room, week, now) for room in rooms],
            next_cursor=next_cursor,
        )

    def get_room(self, *, week_id: str, room_id: str, now: datetime) -> MomotalkRoomResponse:
        # Gate before reading private state or signing a single media URL.
        week = self._week(week_id, now)
        self._room_id(room_id)
        room = self.store.get_room(week.week_id, room_id)
        if room is None:
            raise MomotalkFailure("MOMOTALK_NOT_FOUND", 404)
        summary = self._summary(room, week, now)
        variants: tuple[Literal["cyan", "pink", "lavender"], ...] = ("cyan", "pink", "lavender")
        participants = tuple(
            ParticipantSummary(
                slot=slot,
                display_name=PARTICIPANT_NAMES[slot],
                avatar=self._avatar(
                    f"participants/{slot}/avatar.webp", PARTICIPANT_NAMES[slot], variant
                ),
            )
            for slot, variant in zip(PARTICIPANTS, variants, strict=True)
        )
        images = []
        if summary.state == "ready":
            for image in room.images:
                state = image.state
                if state == "pending" and now >= week.publish_at + timedelta(days=1):
                    state = "failed"
                images.append(
                    MomotalkImage(
                        participant=image.participant,
                        mood=image.mood,
                        state=state,
                        url=self.assets.image_url(room, image.mood) if state == "ready" else None,
                        thumbnail_url=self.assets.image_url(room, image.mood, thumbnail=True)
                        if state == "ready"
                        else None,
                        download_url=self.assets.image_url(room, image.mood, download=True)
                        if state == "ready"
                        else None,
                    )
                )
        return MomotalkRoomResponse.model_validate(
            {
                "week": week,
                "room": summary,
                "participants": participants,
                "messages": [
                    MomotalkMessage(
                        id=index + 1, participant=message.participant, text=message.text
                    )
                    for index, message in enumerate(room.messages)
                ]
                if summary.state == "ready"
                else [],
                "images": images,
            }
        )

    def _summary(self, room: Room, week: MomotalkWeek, now: datetime) -> MomotalkRoomSummary:
        state = room.state
        # Queue retention bounds recovery. A lost/expired job must not leave a room
        # apparently preparing forever; completed late recovery still becomes ready.
        if state == "preparing" and now >= week.publish_at + timedelta(days=1):
            state = "failed"
        return MomotalkRoomSummary(
            room_id=room.room_id,
            question_count=room.question_count,
            state=state,
            requester=RequesterSummary(
                display_name=room.display_name,
                avatar=self._avatar(room.avatar_key, room.display_name, "cyan"),
            ),
        )

    def _avatar(
        self, key: str | None, name: str, variant: Literal["cyan", "pink", "lavender"]
    ) -> AvatarRef:
        if key:
            if not key.startswith(("requesters/", "participants/")):
                raise MomotalkFailure()
            return ImageAvatarRef(
                kind="image",
                url=self.reader.avatar_url(asset_key=key),
                alt=f"{name}のアバター",
                fallback_variant=variant,
            )
        return PlaceholderAvatarRef(
            kind="placeholder", alt=f"{name}のアバター", fallback_variant=variant
        )

    @staticmethod
    def _room_id(value: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise MomotalkFailure("REQUEST_INVALID", 400)
