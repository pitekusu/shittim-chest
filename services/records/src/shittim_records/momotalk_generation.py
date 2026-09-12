"""Restartable weekly collection and bounded, one-step-at-a-time generation."""

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, Protocol

from shittim_records.contracts import MomotalkWeek
from shittim_records.momotalk import (
    MAX_ATTEMPTS,
    ConversationPlan,
    ImageChoice,
    MomotalkFailure,
    Question,
    RequesterInput,
    Room,
    SavedImage,
    SavedMessage,
    Utterance,
    WeekDigest,
    WeeklyInput,
    question_chunks,
    validate_image_choices,
)


class Store(Protocol):
    def get_week(self, week_id: date) -> dict[str, Any] | None: ...
    def create_week(self, snapshot: WeeklyInput, input_version: str) -> dict[str, Any]: ...
    def mark_enqueued(self, week_id: date) -> None: ...
    def create_room(self, week: MomotalkWeek, requester: RequesterInput) -> None: ...
    def get_room(self, week_id: date, room_id: str) -> Room | None: ...
    def claim(self, room: Room, now: datetime) -> Room | None: ...
    def save(self, room: Room) -> None: ...
    def all_rooms(self, week_id: date) -> Iterator[Room]: ...


class Assets(Protocol):
    def freeze(self, snapshot: WeeklyInput) -> tuple[WeeklyInput, str]: ...
    def load_input(self, week_id: date, version: str | None = None) -> tuple[WeeklyInput, str]: ...
    def delete_input(self, week_id: date, version: str) -> None: ...
    def image_exists(self, room: Room, mood: str) -> bool: ...
    def store_image(self, room: Room, mood: str, content: bytes) -> None: ...


class InputSource(Protocol):
    def collect(self, week: MomotalkWeek) -> WeeklyInput: ...


class JobQueue(Protocol):
    def send(self, week_id: date, room_id: str) -> None: ...


class Generator(Protocol):
    def prepare(
        self,
        snapshot: WeeklyInput,
        requester: RequesterInput,
        room: Room,
        questions: list[Question],
        *,
        final: bool,
    ) -> WeekDigest: ...
    def utter(self, snapshot: WeeklyInput, requester: RequesterInput, room: Room) -> Utterance: ...
    def selfie(
        self,
        snapshot: WeeklyInput,
        requester: RequesterInput,
        image: SavedImage,
        choice: ImageChoice,
    ) -> bytes: ...


def collect_week(
    week: MomotalkWeek, source: InputSource, store: Store, assets: Assets, queue: JobQueue
) -> int:
    existing = store.get_week(week.week_id)
    if existing and existing["enqueued"]:
        return 0
    if existing:
        snapshot, version = assets.load_input(week.week_id, existing["input_version"])
    else:
        snapshot, version = assets.freeze(source.collect(week))
        store.create_week(snapshot, version)
    # Create the complete roster before any worker can clean up shared input.
    for requester in snapshot.requesters:
        store.create_room(snapshot.week, requester)
    for requester in snapshot.requesters:
        queue.send(snapshot.week.week_id, requester.room_id)
    store.mark_enqueued(snapshot.week.week_id)
    rooms = list(store.all_rooms(week.week_id))
    if {room.room_id for room in rooms} == {r.room_id for r in snapshot.requesters} and all(
        room.complete for room in rooms
    ):
        assets.delete_input(week.week_id, version)
    return len(snapshot.requesters)


class MomotalkGenerationService:
    def __init__(self, store: Store, assets: Assets, queue: JobQueue, generator: Generator) -> None:
        self.store = store
        self.assets = assets
        self.queue = queue
        self.generator = generator

    def run(self, week_id: date, room_id: str, *, now: datetime) -> bool:
        """Return false for a bounded SQS retry; never log the provider's exception."""
        room = self.store.get_room(week_id, room_id)
        week = self.store.get_week(week_id)
        if room is None or week is None:
            raise MomotalkFailure("MOMOTALK_JOB_INVALID")
        if room.complete:
            self._cleanup(week_id, week)
            return True
        claimed = self.store.claim(room, now)
        if claimed is None:
            return False
        room = claimed
        if room.attempts > MAX_ATTEMPTS:
            self._fail_step(room)
        else:
            snapshot, _version = self.assets.load_input(week_id, week["input_version"])
            requester = next((r for r in snapshot.requesters if r.room_id == room_id), None)
            if requester is None:
                raise MomotalkFailure("MOMOTALK_JOB_INVALID")
            try:
                self._step(snapshot, requester, room)
            except MomotalkFailure:
                if room.attempts < MAX_ATTEMPTS:
                    self.store.save(room)
                    return False
                self._fail_step(room)
        room.attempts = 0
        if room.state == "ready" and all(image.state != "pending" for image in room.images):
            room.complete = True
        self.store.save(room)
        # Enqueue after saving. If this send fails, redelivery resumes the saved next
        # step rather than re-running the expensive completed provider request.
        if not room.complete:
            self.queue.send(week_id, room_id)
        else:
            self._cleanup(week_id, week)
        return True

    def _step(self, snapshot: WeeklyInput, requester: RequesterInput, room: Room) -> None:
        if room.plan is None:
            chunks = question_chunks(requester.questions)
            last = room.digest_chunks == len(chunks) - 1
            result = self.generator.prepare(
                snapshot, requester, room, chunks[room.digest_chunks], final=last
            )
            validate_image_choices(result.images, room.images, requester.questions)
            room.digest_chunks += 1
            if last:
                room.plan = ConversationPlan.model_validate(result.model_dump())
                room.digest = None
            else:
                room.digest = result
            return
        if len(room.messages) < len(room.plan.turns):
            utterance = self.generator.utter(snapshot, requester, room)
            participant = room.plan.turns[len(room.messages)].participant
            room.messages.append(SavedMessage(participant=participant, text=utterance.text))
            if len(room.messages) == len(room.plan.turns):
                room.state = "ready"
            return
        for image, choice in zip(room.images, room.plan.images, strict=True):
            if image.state != "pending":
                continue
            if not self.assets.image_exists(room, image.mood):
                content = self.generator.selfie(snapshot, requester, image, choice)
                self.assets.store_image(room, image.mood, content)
            image.state = "ready"
            return

    @staticmethod
    def _fail_step(room: Room) -> None:
        if room.state != "ready":
            room.state = "failed"
            room.complete = True
            for image in room.images:
                if image.state == "pending":
                    image.state = "failed"
        else:
            for image in room.images:
                if image.state == "pending":
                    image.state = "failed"
                    break

    def _cleanup(self, week_id: date, week: dict[str, Any]) -> None:
        if not week["enqueued"]:
            return
        rooms = list(self.store.all_rooms(week_id))
        if {room.room_id for room in rooms} == set(week["room_ids"]) and all(
            room.complete for room in rooms
        ):
            self.assets.delete_input(week_id, week["input_version"])
