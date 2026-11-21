"""Scoped AWS persistence for weekly inputs, checkpoints and generated images."""

import io
import json
import warnings
from collections.abc import Iterator
from contextlib import suppress
from datetime import date, datetime
from statistics import fmean
from struct import error as StructError
from typing import TYPE_CHECKING, Any, cast

from PIL import Image, ImageChops, ImageOps, ImageStat
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.contracts import MomotalkWeek
from shittim_records.memorial_adapters import MemorialConfigurationRepository
from shittim_records.momotalk import (
    ANIMATION_FRAME_COUNT,
    ANIMATION_FRAME_MS,
    ANIMATION_MAX_BYTES,
    ANIMATION_SIZE,
    MAX_CHAIN_STEPS,
    PARTICIPANTS,
    MomotalkFailure,
    Question,
    RequesterInput,
    Room,
    SavedImage,
    WeeklyInput,
    image_key,
    image_targets,
    persona_checksum,
)
from shittim_records.ranking_adapters import DynamoRankingSource
from shittim_records.read_adapters import DynamoRecordsReader

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_sqs.client import SQSClient

WEEKS_PK = "MOMOTALK#WEEKS"


class DynamoMomotalkStore:
    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self.client = client
        self.table_name = table_name

    def get_week(self, week_id: date) -> dict[str, Any] | None:
        result = self.client.get_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": WEEKS_PK, "SK": str(week_id)}),
            ConsistentRead=True,
        )
        item = result.get("Item")
        return unmarshal_item(item) if item else None

    def create_week(self, snapshot: WeeklyInput, input_version: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "PK": WEEKS_PK,
            "SK": str(snapshot.week.week_id),
            "record_type": "momotalk_week",
            "week": snapshot.week.model_dump(mode="json"),
            "input_version": input_version,
            "room_ids": [requester.room_id for requester in snapshot.requesters],
            "persona_checksum": persona_checksum(snapshot.personas),
            "enqueued": False,
        }
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=marshal_item(item),
                ConditionExpression="attribute_not_exists(PK)",
            )
            return item
        except self.client.exceptions.ConditionalCheckFailedException:
            existing = self.get_week(snapshot.week.week_id)
            if existing is None:
                raise MomotalkFailure() from None
            return existing

    def mark_enqueued(self, week_id: date) -> None:
        self.client.update_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": WEEKS_PK, "SK": str(week_id)}),
            UpdateExpression="SET enqueued = :value",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues=marshal_item({":value": True}),
        )

    @staticmethod
    def room_key(week_id: date, room_id: str) -> dict[str, str]:
        return {"PK": f"MOMOTALK#WEEK#{week_id}", "SK": room_id}

    def get_room(self, week_id: date, room_id: str) -> Room | None:
        result = self.client.get_item(
            TableName=self.table_name,
            Key=marshal_item(self.room_key(week_id, room_id)),
            ConsistentRead=True,
        )
        item = result.get("Item")
        return self.decode_room(unmarshal_item(item)) if item else None

    @staticmethod
    def decode_room(item: dict[str, Any]) -> Room:
        room = Room.model_validate(item["payload"])
        if (
            item.get("record_type") != "momotalk_room"
            or item.get("version") != room.version
            or item.get("lease_until") != room.lease_until
            or item.get("PK") != f"MOMOTALK#WEEK#{room.week_id}"
            or item.get("SK") != room.room_id
        ):
            raise MomotalkFailure()
        return room

    def _item(self, room: Room) -> dict[str, Any]:
        return {
            **self.room_key(room.week_id, room.room_id),
            "record_type": "momotalk_room",
            "version": room.version,
            "lease_until": room.lease_until,
            "payload": room.model_dump(mode="json"),
        }

    def create_room(self, week: MomotalkWeek, requester: RequesterInput) -> None:
        room = Room(
            week_id=week.week_id,
            room_id=requester.room_id,
            display_name=requester.display_name,
            avatar_key=requester.avatar_key,
            question_count=len(requester.questions),
            images=image_targets(requester, week.week_id),
        )
        # Scheduled-event redelivery must preserve existing checkpoints.
        with suppress(self.client.exceptions.ConditionalCheckFailedException):
            self.client.put_item(
                TableName=self.table_name,
                Item=marshal_item(self._item(room)),
                ConditionExpression="attribute_not_exists(PK)",
            )

    def claim(self, room: Room, now: datetime) -> Room | None:
        claimed = room.model_copy(
            update={
                "version": room.version + 1,
                "lease_until": int(now.timestamp()) + 330,
                "attempts": room.attempts + 1,
            },
            deep=True,
        )
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=marshal_item(self._item(claimed)),
                ConditionExpression="#version = :version AND lease_until <= :now",
                ExpressionAttributeNames={"#version": "version"},
                ExpressionAttributeValues=marshal_item(
                    {":version": room.version, ":now": int(now.timestamp())}
                ),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return None
        return claimed

    def save(self, room: Room) -> None:
        previous_version = room.version
        room.version += 1
        room.lease_until = 0
        self.client.put_item(
            TableName=self.table_name,
            Item=marshal_item(self._item(room)),
            ConditionExpression="#version = :version",
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues=marshal_item({":version": previous_version}),
        )

    def page(
        self,
        pk: str,
        *,
        cursor: str | None,
        limit: int,
        reverse: bool = False,
        before: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        expression = "PK = :pk"
        values: dict[str, Any] = {":pk": pk}
        if before:
            expression += " AND SK <= :before"
            values[":before"] = before
        arguments: dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": expression,
            "ExpressionAttributeValues": marshal_item(values),
            "Limit": limit,
            "ConsistentRead": True,
            "ScanIndexForward": not reverse,
        }
        if cursor:
            arguments["ExclusiveStartKey"] = marshal_item({"PK": pk, "SK": cursor})
        result = self.client.query(**arguments)
        last = result.get("LastEvaluatedKey")
        return (
            [unmarshal_item(item) for item in result.get("Items", [])],
            cast(str, unmarshal_item(last)["SK"]) if last else None,
        )

    def all_rooms(self, week_id: date) -> Iterator[Room]:
        for page in self.client.get_paginator("query").paginate(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues=marshal_item({":pk": f"MOMOTALK#WEEK#{week_id}"}),
            ConsistentRead=True,
        ):
            for item in page.get("Items", []):
                yield self.decode_room(unmarshal_item(item))


def _selfie_seam_start(frame: Image.Image) -> int | None:
    # A displaced provider panel leaves a narrow, almost entirely black vertical seam.
    gray = frame.convert("L")
    dark = [
        x
        for x in range(frame.width * 4 // 5, frame.width - 4)
        if sum(gray.crop((x, 0, x + 1, frame.height)).histogram()[:33]) >= frame.height * 0.95
    ]
    if 1 <= len(dark) <= 3 and dark[-1] - dark[0] == len(dark) - 1:
        return dark[0]
    return None


def _selfie_frame_change(frames: list[Image.Image]) -> float:
    samples = [frame.resize((120, 180), Image.Resampling.BOX).convert("L") for frame in frames]
    return fmean(
        ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
        for a, b in zip(samples, samples[1:] + samples[:1], strict=True)
    )


def _repair_slipping_selfie(frames: list[Image.Image]) -> list[Image.Image] | None:
    seams = [_selfie_seam_start(frame) for frame in frames]
    boundaries = [seam for seam in seams if seam is not None]
    if len(boundaries) != len(frames) or max(boundaries) - min(boundaries) < 8:
        return None

    # Keep the shared region of the primary panel to the left of each seam.
    # The right strip may be another panel, so wrapping it back adds a second seam.
    width, height = ANIMATION_SIZE
    inset = 4
    common_width = min(boundaries) - inset
    crop_height = round(common_width * height / width)
    repaired = []
    for frame, boundary in zip(frames, boundaries, strict=True):
        right = boundary - (inset - 1)
        cropped = frame.crop((right - common_width, inset, right, inset + crop_height))
        repaired.append(cropped.resize(ANIMATION_SIZE, Image.Resampling.LANCZOS))

    # A real slip moves the whole scene. Avoid changing an incidental dark line.
    if _selfie_frame_change(repaired) < _selfie_frame_change(frames) * 0.75:
        return repaired
    return None


def decode_selfie_animation(content: bytes) -> tuple[bytes, Image.Image]:
    """Validate one provider GIF and produce the looping original and its poster."""
    if not 1 <= len(content) <= ANIMATION_MAX_BYTES or not content.endswith(b";"):
        raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
    try:
        with (
            warnings.catch_warnings(action="error", category=Image.DecompressionBombWarning),
            Image.open(io.BytesIO(content), formats=["GIF"]) as source,
        ):
            if source.size != ANIMATION_SIZE:
                raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
            loop = source.info.get("loop")
            frames = []
            durations = []
            for index in range(ANIMATION_FRAME_COUNT):
                source.seek(index)
                if source.size != ANIMATION_SIZE:
                    raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
                frames.append(source.convert("RGB"))
                durations.append(source.info.get("duration"))
            try:
                source.seek(ANIMATION_FRAME_COUNT)
            except EOFError:
                pass
            else:
                raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
        repaired = _repair_slipping_selfie(frames)
        if repaired is not None:
            frames = repaired
        poster = frames[0]
        if not any(ImageChops.difference(poster, frame).getbbox() for frame in frames[1:]):
            raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
        if (
            repaired is None
            and loop == 0
            and durations == [ANIMATION_FRAME_MS] * ANIMATION_FRAME_COUNT
        ):
            return content, poster
        # The decoded RGB frames can inherit the provider's old palette metadata.
        for frame in frames:
            frame.info.clear()
        with io.BytesIO() as buffer:
            poster.save(
                buffer,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=ANIMATION_FRAME_MS,
                loop=0,
            )
            normalized = buffer.getvalue()
        if len(normalized) > ANIMATION_MAX_BYTES:
            raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
        return normalized, poster
    except (
        EOFError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        IndexError,
        OSError,
        StructError,
        SyntaxError,
        ValueError,
    ):
        raise MomotalkFailure("MOMOTALK_IMAGE_INVALID") from None


class MomotalkAssets:
    def __init__(self, client: S3Client, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @staticmethod
    def input_key(week_id: date) -> str:
        return f"momotalk/inputs/{week_id}/snapshot.json"

    def freeze(self, snapshot: WeeklyInput) -> tuple[WeeklyInput, str]:
        try:
            result = self.client.put_object(
                Bucket=self.bucket,
                Key=self.input_key(snapshot.week.week_id),
                Body=snapshot.model_dump_json().encode(),
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
            version = result.get("VersionId")
            if not version:
                raise MomotalkFailure("MOMOTALK_INPUT_VERSION_MISSING")
            return snapshot, version
        except self.client.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            return self.load_input(snapshot.week.week_id)

    def load_input(self, week_id: date, version: str | None = None) -> tuple[WeeklyInput, str]:
        parameters: dict[str, Any] = {"Bucket": self.bucket, "Key": self.input_key(week_id)}
        if version:
            parameters["VersionId"] = version
        response = self.client.get_object(**parameters)
        with response["Body"] as body:
            snapshot = WeeklyInput.model_validate_json(body.read())
        if snapshot.week.week_id != week_id or not response.get("VersionId"):
            raise MomotalkFailure()
        return snapshot, response["VersionId"]

    def delete_input(self, week_id: date, version: str) -> None:
        # The Media bucket is versioned: remove the exact payload, not a delete marker.
        self.client.delete_object(
            Bucket=self.bucket, Key=self.input_key(week_id), VersionId=version
        )

    def image_exists(self, room: Room, image: SavedImage) -> bool:
        full_key = image_key(room, image)
        thumbnail_key = image_key(room, image, thumbnail=True)
        # A narrowly scoped ListBucket permission distinguishes absence from a 403.
        response = self.client.list_objects_v2(
            Bucket=self.bucket, Prefix=full_key.rsplit(".", 1)[0], MaxKeys=10
        )
        keys = {entry["Key"] for entry in response.get("Contents", [])}
        if full_key not in keys:
            return False
        if thumbnail_key not in keys:
            # Recover a failed thumbnail write without paying to generate the image again.
            response = self.client.get_object(Bucket=self.bucket, Key=full_key)
            with response["Body"] as body:
                limit = ANIMATION_MAX_BYTES if image.animated else 20 * 1024 * 1024
                self.store_image(room, image, body.read(limit + 1), thumbnail_only=True)
        return True

    def store_image(
        self,
        room: Room,
        image: SavedImage,
        content: bytes,
        *,
        thumbnail_only: bool = False,
    ) -> None:
        limit = ANIMATION_MAX_BYTES if image.animated else 20 * 1024 * 1024
        if not 1 <= len(content) <= limit:
            raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
        if image.animated and not thumbnail_only:
            content, decoded = decode_selfie_animation(content)
        else:
            # An already stored, normalized GIF only needs its first frame for repair.
            with Image.open(
                io.BytesIO(content), formats=["GIF"] if image.animated else None
            ) as source:
                if source.width * source.height > 16_000_000 or min(source.size) < 256:
                    raise MomotalkFailure("MOMOTALK_IMAGE_INVALID")
                decoded = ImageOps.exif_transpose(source).convert("RGB")
        for thumbnail in (True,) if thumbnail_only else (False, True):
            if image.animated and not thumbnail:
                body, content_type = content, "image/gif"
            else:
                output = decoded.copy()
                output.thumbnail((320, 480) if thumbnail else (1024, 1536))
                with io.BytesIO() as buffer:
                    output.save(buffer, format="WEBP", quality=84 if thumbnail else 94, method=4)
                    body = buffer.getvalue()
                content_type = "image/webp"
            self.client.put_object(
                Bucket=self.bucket,
                Key=image_key(room, image, thumbnail=thumbnail),
                Body=body,
                ContentType=content_type,
                ServerSideEncryption="AES256",
                CacheControl="private, max-age=300",
            )

    def image_url(
        self,
        room: Room,
        image: SavedImage,
        *,
        thumbnail: bool = False,
        download: bool = False,
    ) -> str:
        key = image_key(room, image, thumbnail=thumbnail)
        parameters = {"Bucket": self.bucket, "Key": key}
        if download:
            name = "selfie" if image.subjects is not None else image.mood
            extension = key.rsplit(".", 1)[1]
            parameters["ResponseContentDisposition"] = (
                f'attachment; filename="momotalk-{room.week_id}-{name}.{extension}"'
            )
        return self.client.generate_presigned_url("get_object", Params=parameters, ExpiresIn=300)


class MomotalkInputSource:
    def __init__(
        self,
        client: DynamoDBClient,
        archive_table: str,
        statistics_table: str,
        reader: DynamoRecordsReader,
        configuration: MemorialConfigurationRepository,
    ) -> None:
        self.client = client
        self.archive_table = archive_table
        self.statistics_table = statistics_table
        self.reader = reader
        self.configuration = configuration

    def collect(self, week: MomotalkWeek) -> WeeklyInput:
        names: dict[str, str] = {}
        questions: dict[str, list[Question]] = {}
        for page in self.client.get_paginator("query").paginate(
            TableName=self.archive_table,
            IndexName="gsi1",
            KeyConditionExpression="gsi1pk = :pk",
            ExpressionAttributeValues=marshal_item({":pk": "ARCHIVE#COMPLETED"}),
            ProjectionExpression=(
                "record_id, requester_key, requester_display_name, "
                "completed_at, question, affection"
            ),
            Select="SPECIFIC_ATTRIBUTES",
            ScanIndexForward=True,
        ):
            for raw in page.get("Items", []):
                item = unmarshal_item(raw)
                completed = datetime.fromisoformat(str(item["completed_at"]))
                if completed >= week.period_end:
                    continue
                requester = str(item["requester_key"])
                names[requester] = str(item["requester_display_name"])
                if completed >= week.period_start:
                    questions.setdefault(requester, []).append(
                        Question.model_validate(
                            {
                                "record_id": item["record_id"],
                                "text": item["question"],
                                "completed_at": completed,
                                "affection": item.get("affection"),
                            }
                        )
                    )
        affection_profiles = {
            str(profile["SK"]): profile
            for profile in DynamoRankingSource(
                self.client, self.archive_table, self.statistics_table
            ).list_affection_profiles()
        }
        # An accepted question may have changed affection even if the discussion later
        # failed and produced no Archive. That requester still receives a zero-question room.
        for requester, profile in affection_profiles.items():
            names.setdefault(requester, str(profile["display_name"]))
        profiles = self.reader.load_profiles(requester_keys=tuple(names))
        requesters = []
        for requester in sorted(names):
            profile = profiles.get(requester)
            scores = affection_profiles.get(requester, {}).get(
                "scores", {slot: 500 for slot in PARTICIPANTS}
            )
            requesters.append(
                RequesterInput.model_validate(
                    {
                        "room_id": requester,
                        "display_name": profile.display_name if profile else names[requester],
                        "avatar_key": profile.avatar_asset_key if profile else None,
                        "scores": scores,
                        "questions": questions.get(requester, []),
                    }
                )
            )
        return WeeklyInput(
            week=week,
            requesters=requesters,
            personas={
                slot: self.configuration.load_participant_prompt(slot) for slot in PARTICIPANTS
            },
        )


class MomotalkQueue:
    def __init__(self, client: SQSClient, url: str) -> None:
        self.client = client
        self.url = url

    def send(self, week_id: date, room_id: str, *, steps_remaining: int = MAX_CHAIN_STEPS) -> None:
        self.client.send_message(
            QueueUrl=self.url,
            MessageBody=json.dumps(
                {
                    "weekId": str(week_id),
                    "roomId": room_id,
                    "stepsRemaining": steps_remaining,
                }
            ),
        )
