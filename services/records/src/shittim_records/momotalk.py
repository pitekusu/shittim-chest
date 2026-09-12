"""Weekly MomoTalk rules and checkpoints. No AWS or provider dependencies."""

import hashlib
import unicodedata
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo

import regex
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from shittim_records.contracts import MomotalkWeek, ParticipantSlot

PARTICIPANTS: tuple[ParticipantSlot, ...] = (
    "participant-a",
    "participant-b",
    "participant-c",
)
PARTICIPANT_NAMES = dict(zip(PARTICIPANTS, ("アロナ", "プラナ", "安倍晋三AI"), strict=True))
TOKYO = ZoneInfo("Asia/Tokyo")
MAX_CHARACTERS = 100
MAX_ATTEMPTS = 3
QUESTION_CHUNK_BYTES = 24_000
OpaqueId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
Score = Annotated[int, Field(strict=True, ge=0, le=1000)]


class MomotalkFailure(RuntimeError):
    def __init__(self, code: str = "MOMOTALK_UNAVAILABLE", status: int = 503) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class StoredModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(StoredModel):
    record_id: OpaqueId
    text: str = Field(min_length=1, max_length=1000, repr=False)
    completed_at: AwareDatetime
    affection: dict[str, object] | None = Field(default=None, repr=False)


class RequesterInput(StoredModel):
    room_id: OpaqueId
    display_name: str = Field(min_length=1, max_length=100, repr=False)
    avatar_key: str | None = None
    scores: dict[ParticipantSlot, Score]
    questions: list[Question] = Field(repr=False)

    @field_validator("scores")
    @classmethod
    def all_scores(cls, value: dict[ParticipantSlot, int]) -> dict[ParticipantSlot, int]:
        if set(value) != set(PARTICIPANTS):
            raise ValueError("incomplete affection scores")
        return value


class WeeklyInput(StoredModel):
    week: MomotalkWeek
    personas: dict[ParticipantSlot, str] = Field(repr=False)
    requesters: list[RequesterInput] = Field(repr=False)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if set(self.personas) != set(PARTICIPANTS) or not all(self.personas.values()):
            raise ValueError("incomplete personas")
        if len({requester.room_id for requester in self.requesters}) != len(self.requesters):
            raise ValueError("duplicate requester")
        for requester in self.requesters:
            if len({q.record_id for q in requester.questions}) != len(requester.questions):
                raise ValueError("duplicate question")
            if any(
                not self.week.period_start <= q.completed_at < self.week.period_end
                for q in requester.questions
            ):
                raise ValueError("question outside weekly interval")
        return self


class Turn(StoredModel):
    participant: ParticipantSlot
    topic: str = Field(min_length=1, max_length=300, repr=False)
    # Older checkpoints contain only topic text and remain resumable.
    record_id: OpaqueId | None = None


class ImageChoice(StoredModel):
    mood: Literal["happy", "unhappy"]
    record_id: OpaqueId
    brief: str = Field(min_length=1, max_length=1200, repr=False)


class WeekDigest(StoredModel):
    summary: str = Field(min_length=1, max_length=4000, repr=False)
    images: list[ImageChoice] = Field(max_length=2)


class ConversationPlan(WeekDigest):
    turns: list[Turn] = Field(min_length=9, max_length=15)

    @field_validator("turns")
    @classmethod
    def balanced_turns(cls, value: list[Turn]) -> list[Turn]:
        counts = Counter(turn.participant for turn in value)
        if any(not 3 <= counts[slot] <= 5 for slot in PARTICIPANTS):
            raise ValueError("each participant must speak three to five times")
        if all(
            turn.participant == value[index % 3].participant for index, turn in enumerate(value)
        ):
            raise ValueError("conversation must not repeat a fixed three-person rotation")
        return value


class Utterance(StoredModel):
    text: str = Field(min_length=1, repr=False)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        normalized = unicodedata.normalize(
            "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
        ).strip()
        if not normalized or len(regex.findall(r"\X", normalized)) > MAX_CHARACTERS:
            raise ValueError("utterance must contain one to 100 graphemes")
        if any(unicodedata.category(c) == "Cc" and c != "\n" for c in normalized):
            raise ValueError("utterance contains control characters")
        return normalized


class SavedMessage(Utterance):
    participant: ParticipantSlot


class SavedImage(StoredModel):
    participant: ParticipantSlot
    mood: Literal["happy", "unhappy"]
    state: Literal["pending", "ready", "failed"] = "pending"


class Room(StoredModel):
    week_id: date
    room_id: OpaqueId
    display_name: str = Field(repr=False)
    avatar_key: str | None = None
    question_count: int = Field(ge=0)
    state: Literal["preparing", "ready", "failed"] = "preparing"
    plan: ConversationPlan | None = Field(default=None, repr=False)
    digest: WeekDigest | None = Field(default=None, repr=False)
    digest_chunks: int = 0
    messages: list[SavedMessage] = Field(default_factory=list, repr=False)
    images: list[SavedImage] = Field(default_factory=list)
    attempts: int = 0
    version: int = 0
    lease_until: int = 0
    complete: bool = False

    @model_validator(mode="after")
    def valid_checkpoint(self) -> Self:
        if len(self.messages) > 15 or len(self.images) > 2:
            raise ValueError("checkpoint limits exceeded")
        if self.messages and self.plan is None:
            raise ValueError("conversation has no plan")
        if self.plan:
            if len(self.messages) > len(self.plan.turns) or any(
                message.participant != self.plan.turns[index].participant
                for index, message in enumerate(self.messages)
            ):
                raise ValueError("conversation does not follow its plan")
            if [image.mood for image in self.images] != [
                choice.mood for choice in self.plan.images
            ]:
                raise ValueError("image targets do not match")
        if self.state == "ready" and (
            self.plan is None or len(self.messages) != len(self.plan.turns)
        ):
            raise ValueError("incomplete conversation cannot be published")
        if self.complete and (
            self.state == "preparing" or any(image.state == "pending" for image in self.images)
        ):
            raise ValueError("incomplete checkpoint marked complete")
        return self


def week_for_schedule(scheduled_at: datetime) -> MomotalkWeek:
    local = scheduled_at.astimezone(TOKYO)
    if local.weekday() != 6 or local.hour != 18:
        raise MomotalkFailure("MOMOTALK_SCHEDULE_INVALID", 400)
    cutoff = local.replace(minute=0, second=0, microsecond=0)
    return MomotalkWeek(
        week_id=cutoff.date(),
        period_start=(cutoff - timedelta(days=7)).astimezone(UTC),
        period_end=cutoff.astimezone(UTC),
        publish_at=(cutoff + timedelta(hours=2)).astimezone(UTC),
    )


def validate_week_id(value: str) -> date:
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value or result.weekday() != 6:
            raise ValueError
        return result
    except ValueError as error:
        raise MomotalkFailure("REQUEST_INVALID", 400) from error


def image_targets(requester: RequesterInput, week_id: date) -> list[SavedImage]:
    if not requester.questions:
        return []
    rotation = (week_id - date(2026, 1, 4)).days // 7 % 3
    order = (*PARTICIPANTS[rotation:], *PARTICIPANTS[:rotation])
    highest = max(order, key=lambda slot: requester.scores[slot])
    lowest = min(order, key=lambda slot: requester.scores[slot])
    images: list[SavedImage] = []
    if requester.scores[highest] >= 800:
        images.append(SavedImage(participant=highest, mood="happy"))
    if requester.scores[lowest] <= 200:
        images.append(SavedImage(participant=lowest, mood="unhappy"))
    return images


def question_chunks(questions: list[Question]) -> list[list[Question]]:
    chunks: list[list[Question]] = [[]]
    size = 0
    for question in questions:
        length = len(question.model_dump_json().encode())
        if chunks[-1] and size + length > QUESTION_CHUNK_BYTES:
            chunks.append([])
            size = 0
        chunks[-1].append(question)
        size += length
    return chunks


def validate_image_choices(
    choices: list[ImageChoice],
    targets: list[SavedImage],
    questions: list[Question],
) -> None:
    if [choice.mood for choice in choices] != [target.mood for target in targets]:
        raise MomotalkFailure("MOMOTALK_OUTPUT_INVALID")
    identifiers = {question.record_id for question in questions}
    if any(choice.record_id not in identifiers for choice in choices):
        raise MomotalkFailure("MOMOTALK_OUTPUT_INVALID")


def image_key(week_id: date, room_id: str, mood: str, *, thumbnail: bool = False) -> str:
    # Identifiers have already passed the stored/public boundary validators.
    suffix = "-thumb" if thumbnail else ""
    return f"momotalk/images/{week_id}/{room_id}/{mood}{suffix}.webp"


def persona_checksum(personas: dict[ParticipantSlot, str]) -> str:
    return hashlib.sha256("\x00".join(personas[slot] for slot in PARTICIPANTS).encode()).hexdigest()
