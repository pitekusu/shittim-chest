"""Restartable weekly collection and bounded, one-step-at-a-time generation."""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any, Protocol

from shittim_records.contracts import MomotalkWeek
from shittim_records.momotalk import (
    MAX_ATTEMPTS,
    MAX_CHAIN_STEPS,
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

LOGGER = logging.getLogger(__name__)
FAILURE_CODES = frozenset(
    {
        "MOMOTALK_GENERATION_FAILED",
        "MOMOTALK_OUTPUT_INVALID",
        "MOMOTALK_IMAGE_INVALID",
        "MOMOTALK_IMAGE_MODERATION_BLOCKED",
        "MOMOTALK_UNAVAILABLE",
    }
)


def _log_step_failure(room: Room, error: MomotalkFailure) -> None:
    stage = (
        "prepare"
        if room.plan is None
        else ("utter" if len(room.messages) < len(room.plan.turns) else "image")
    )
    LOGGER.warning(
        "momotalk_step_failed week=%s stage=%s attempt=%s code=%s",
        room.week_id,
        stage,
        room.attempts,
        error.code if error.code in FAILURE_CODES else "MOMOTALK_UNAVAILABLE",
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
    def send(
        self, week_id: date, room_id: str, *, steps_remaining: int = MAX_CHAIN_STEPS
    ) -> None: ...


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

    def reselect_image(
        self,
        snapshot: WeeklyInput,
        requester: RequesterInput,
        image: SavedImage,
        questions: list[Question],
    ) -> ImageChoice: ...


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


def continue_week(week_id: date, store: Store, queue: JobQueue, *, now: datetime) -> int:
    """Resume only deliberately paused work from an independent scheduled event."""
    week = store.get_week(week_id)
    if week is None or not week["enqueued"]:
        return 0
    count = 0
    for room in store.all_rooms(week_id):
        if not room.complete and room.continuation_pending and room.lease_until <= now.timestamp():
            # Keep the marker until a worker starts. Failed/unknown sends are
            # recoverable at the next tick; conditional claims handle duplicates.
            queue.send(week_id, room.room_id)
            count += 1
    return count


class MomotalkGenerationService:
    def __init__(self, store: Store, assets: Assets, queue: JobQueue, generator: Generator) -> None:
        self.store = store
        self.assets = assets
        self.queue = queue
        self.generator = generator

    def run(
        self,
        week_id: date,
        room_id: str,
        *,
        now: datetime,
        steps_remaining: int = MAX_CHAIN_STEPS,
    ) -> bool:
        """Return false for a bounded SQS retry; never log the provider's exception."""
        if not 1 <= steps_remaining <= MAX_CHAIN_STEPS:
            raise MomotalkFailure("MOMOTALK_JOB_INVALID")
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
        room.continuation_pending = False
        if room.attempts > MAX_ATTEMPTS:
            self._fail_step(room)
        else:
            snapshot, _version = self.assets.load_input(week_id, week["input_version"])
            requester = next((r for r in snapshot.requesters if r.room_id == room_id), None)
            if requester is None:
                raise MomotalkFailure("MOMOTALK_JOB_INVALID")
            try:
                self._step(snapshot, requester, room)
            except MomotalkFailure as error:
                _log_step_failure(room, error)
                if room.attempts < MAX_ATTEMPTS:
                    self.store.save(room)
                    return False
                self._fail_step(room)
        room.attempts = 0
        if room.state == "ready" and all(image.state != "pending" for image in room.images):
            room.complete = True
        room.continuation_pending = not room.complete and steps_remaining == 1
        self.store.save(room)
        # Enqueue after saving. If this send fails, redelivery resumes the saved next
        # step rather than re-running the expensive completed provider request.
        if not room.complete and not room.continuation_pending:
            self.queue.send(week_id, room_id, steps_remaining=steps_remaining - 1)
        elif room.complete:
            self._cleanup(week_id, week)
        return True

    def retry_failed_image(
        self, week_id: date, room_id: str, mood: str, source: InputSource, *, now: datetime
    ) -> bool:
        """One repair, with one alternate subject only after a moderation rejection."""
        room = self.store.get_room(week_id, room_id)
        if room is None or room.state != "ready" or not room.complete or room.plan is None:
            raise MomotalkFailure("MOMOTALK_REPAIR_INVALID")
        index = next((i for i, image in enumerate(room.images) if image.mood == mood), None)
        if index is None:
            raise MomotalkFailure("MOMOTALK_REPAIR_INVALID")
        if room.images[index].state == "ready":
            return True
        if room.images[index].state != "failed":
            raise MomotalkFailure("MOMOTALK_REPAIR_INVALID")
        # Completed weeks have no frozen input. Recollect original Archive questions;
        # the saved plan owns the initial subject, speaker, mood and composition.
        stored_week = self.store.get_week(week_id)
        if stored_week is None:
            raise MomotalkFailure("MOMOTALK_REPAIR_INVALID")
        snapshot = source.collect(MomotalkWeek.model_validate(stored_week["week"]))
        requester = next((r for r in snapshot.requesters if r.room_id == room_id), None)
        choice = room.plan.images[index]
        if (
            snapshot.week.week_id != week_id
            or requester is None
            or not any(q.record_id == choice.record_id for q in requester.questions)
        ):
            raise MomotalkFailure("MOMOTALK_REPAIR_INVALID")
        claimed = self.store.claim(room, now)
        if claimed is None:
            raise MomotalkFailure("MOMOTALK_REPAIR_BUSY")
        try:
            # A moderation rejection and reselection are separate checkpoints.
            # Renew the claim between them; operator repair can outlive one lease.
            for _ in range(3):
                if self._image_step(snapshot, requester, claimed, index):
                    return claimed.images[index].state == "ready"
                claimed.attempts = 0
                self.store.save(claimed)
                claimed = self.store.claim(claimed, datetime.now(UTC))
                if claimed is None:
                    raise MomotalkFailure("MOMOTALK_REPAIR_BUSY")
            return False
        except MomotalkFailure as error:
            if claimed is None:
                raise
            _log_step_failure(claimed, error)
            return False
        finally:
            if claimed is not None:
                claimed.attempts = 0
                self.store.save(claimed)

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
            # Two requests fit the worker deadline. The job budget, rather than
            # preparation or turn counts, bounds the SQS invocation chain.
            for offset in range(min(2, len(room.plan.turns) - len(room.messages))):
                if offset:
                    # The next turn starts its own first attempt in this claim.
                    room.attempts = 1
                utterance = self.generator.utter(snapshot, requester, room)
                participant = room.plan.turns[len(room.messages)].participant
                room.messages.append(SavedMessage(participant=participant, text=utterance.text))
            if len(room.messages) == len(room.plan.turns):
                room.state = "ready"
            return
        for index, image in enumerate(room.images):
            if image.state != "pending":
                continue
            self._image_step(snapshot, requester, room, index)
            return

    @staticmethod
    def _alternative_questions(requester: RequesterInput, choice: ImageChoice) -> list[Question]:
        rejected = next((q for q in requester.questions if q.record_id == choice.record_id), None)
        return [
            question
            for question in requester.questions
            if question.record_id != choice.record_id
            and (rejected is None or question.text.strip() != rejected.text.strip())
        ]

    def _image_step(
        self, snapshot: WeeklyInput, requester: RequesterInput, room: Room, index: int
    ) -> bool:
        """Run one provider step; return false only while changing the rejected subject."""
        if room.plan is None:
            raise MomotalkFailure("MOMOTALK_OUTPUT_INVALID")
        image = room.images[index]
        choice = room.plan.images[index]
        if self.assets.image_exists(room, image.mood):
            image.state = "ready"
            image.moderation_blocked = False
            return True
        if image.moderation_blocked:
            questions = self._alternative_questions(requester, choice)
            if image.alternate_topic_used or not questions:
                image.state = "failed"
                return True
            replacement = self.generator.reselect_image(snapshot, requester, image, questions)
            validate_image_choices([replacement], [image], questions)
            room.plan.images[index] = replacement
            image.alternate_topic_used = True
            image.moderation_blocked = False
            return False
        try:
            content = self.generator.selfie(snapshot, requester, image, choice)
        except MomotalkFailure as error:
            if error.code != "MOMOTALK_IMAGE_MODERATION_BLOCKED":
                raise
            _log_step_failure(room, error)
            image.moderation_blocked = True
            if image.alternate_topic_used or not self._alternative_questions(requester, choice):
                image.state = "failed"
                return True
            return False
        self.assets.store_image(room, image.mood, content)
        image.state = "ready"
        return True

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
