"""Moderation changes the subject once, with durable checkpoints and bounded cost."""

from typing import Any, cast

import pytest
from tests.test_momotalk import ROOM_ID, START, WEEK, State, snapshot

from shittim_records.momotalk import PARTICIPANTS, ImageChoice, MomotalkFailure, Room, Turn
from shittim_records.momotalk_generation import MomotalkGenerationService, collect_week


class FallbackState(State):
    def __init__(self, *, reject_alternative=False, questions=2, both_images=False, turns=9):
        value = snapshot()
        original = value.requesters[0].questions[0]
        if questions == 2:
            value.requesters[0].questions.append(
                original.model_copy(update={"record_id": "r" * 43, "text": "休日に読む本は?"})
            )
        super().__init__(value)
        self.image_calls = []
        self.reselections = []
        self.reject_alternative = reject_alternative
        self.both_images = both_images
        self.turn_count = turns
        self.selection_record = None

    def prepare(self, *args, **kwargs):
        plan = super().prepare(*args, **kwargs)
        if self.turn_count == 15:
            plan.turns = [
                Turn(participant=PARTICIPANTS[slot], topic="架空の週の相談")
                for slot in (0, 1, 0, 2, 1, 2, 0, 2, 1, 2, 0, 1, 2, 0, 1)
            ]
        return plan

    def selfie(self, _snapshot, _requester, image, _choice):
        self.image_calls.append((image.mood, _choice.record_id))
        content = super().selfie(_snapshot, _requester, image, _choice)
        if (image.mood == "unhappy" or self.both_images) and (
            _choice.record_id == "q" * 43 or self.reject_alternative
        ):
            raise MomotalkFailure("MOMOTALK_IMAGE_MODERATION_BLOCKED")
        return content

    def reselect_image(self, _snapshot, _requester, image, questions):
        self.reselections.append([q.record_id for q in questions])
        return ImageChoice(
            mood=image.mood,
            record_id=self.selection_record or questions[0].record_id,
            brief="本と自撮り",
        )


def published(state):
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for _ in range(10):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.state == "ready":
            return service
    raise AssertionError("fixture conversation did not finish")


@pytest.mark.parametrize("reject_alternative", [False, True])
def test_moderation_switches_once_and_resumes_saved_image_checkpoints(reject_alternative):
    state = cast(Any, FallbackState(reject_alternative=reject_alternative))
    service = published(state)
    service.run(WEEK.week_id, ROOM_ID, now=START)  # The unaffected happy image.
    before = state.room.model_copy(deep=True)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    image = state.room.images[1]
    assert image.state == "pending" and image.moderation_blocked
    assert not image.alternate_topic_used
    assert state.reselections == []

    boundary = state
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.reselections == [["r" * 43]]
    assert state.room.plan.images[1].record_id == "r" * 43
    assert state.room.images[1].alternate_topic_used
    assert not state.room.images[1].moderation_blocked
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [("unhappy", "q" * 43)]

    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.state == "ready"
    assert state.room.images[1].state == ("failed" if reject_alternative else "ready")
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [
        ("unhappy", "q" * 43),
        ("unhappy", "r" * 43),
    ]
    assert state.room.messages == before.messages
    assert state.room.plan.turns == before.plan.turns
    assert state.room.plan.summary == before.plan.summary
    assert state.room.images[0] == before.images[0]
    calls = state.image_calls.copy()
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", boundary, now=START) is (
        not reject_alternative
    )
    assert state.image_calls == calls
    assert state.reselections == [["r" * 43]]


@pytest.mark.parametrize("duplicate", [False, True])
def test_moderation_without_another_discussion_fails_without_resubmitting(duplicate):
    state = cast(Any, FallbackState(questions=1))
    if duplicate:
        requester = state.snapshot.requesters[0]
        requester.questions.append(
            requester.questions[0].model_copy(update={"record_id": "r" * 43})
        )
    service = published(state)
    for _ in range(3):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.images[1].state == "failed"
    assert state.reselections == []
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [("unhappy", "q" * 43)]


def test_non_moderation_failure_keeps_the_existing_three_attempts_and_topic():
    state = cast(Any, FallbackState())
    state.image_failures.add("unhappy")
    service = published(state)
    for _ in range(4):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.images[1].state == "failed"
    assert state.reselections == []
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [
        ("unhappy", "q" * 43)
    ] * 3
    assert not state.room.images[1].moderation_blocked
    assert not state.room.images[1].alternate_topic_used


@pytest.mark.parametrize("record_id", ["q" * 43, "x" * 43])
def test_reselection_cannot_reuse_the_rejected_or_unknown_record(record_id):
    state = cast(Any, FallbackState())
    service = published(state)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    state.selection_record = record_id
    for _ in range(3):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.images[1].state == "failed"
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [("unhappy", "q" * 43)]


def test_operator_repair_checkpoints_the_switch_and_preserves_other_content():
    state = cast(Any, FallbackState())
    state.image_failures.add("unhappy")
    service = published(state)
    for _ in range(4):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    before = state.room.model_copy(deep=True)
    state.image_failures.clear()
    state.image_calls.clear()
    state.jobs.clear()
    assert service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", state, now=START)
    assert state.image_calls == [("unhappy", "q" * 43), ("unhappy", "r" * 43)]
    assert state.room.plan.images[1].record_id == "r" * 43
    assert state.room.messages == before.messages
    assert state.room.plan.turns == before.plan.turns
    assert state.room.images[0] == before.images[0]
    assert state.room.images[1].state == "ready" and state.room.complete
    assert state.jobs == []


def test_fifteen_turns_and_two_image_fallbacks_finish_below_recursion_limit():
    state = cast(Any, FallbackState(both_images=True, turns=15))
    boundary = state
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for invocations in range(1, 17):
        assert invocations < 16
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.complete:
            break
    assert state.room.complete and invocations < 16
    assert len(state.room.messages) == 15
    assert [image.state for image in state.room.images] == ["ready", "ready"]
    assert len(state.image_calls) == 4 and len(state.reselections) == 2


def test_second_utterance_failure_saves_the_first_and_does_not_regenerate_it(monkeypatch):
    state = cast(Any, State(snapshot(scores=(500, 500, 500))))
    boundary = state
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    original_utter = state.utter
    failed = False

    def utter(value, requester, room):
        nonlocal failed
        if len(room.messages) == 1 and not failed:
            failed = True
            raise MomotalkFailure("MOMOTALK_GENERATION_FAILED")
        return original_utter(value, requester, room)

    monkeypatch.setattr(state, "utter", utter)
    assert not service.run(WEEK.week_id, ROOM_ID, now=START)
    assert len(state.room.messages) == 1
    for _ in range(5):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete
    assert [call for call in state.calls if isinstance(call, int)] == list(range(9))


def test_failures_on_different_batched_turns_do_not_share_attempts(monkeypatch):
    state = cast(Any, State(snapshot(scores=(500, 500, 500))))
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    original_utter = state.utter
    failures = set()

    def utter(value, requester, room):
        index = len(room.messages)
        if index in (1, 2, 3) and index not in failures:
            failures.add(index)
            raise MomotalkFailure("MOMOTALK_GENERATION_FAILED")
        return original_utter(value, requester, room)

    monkeypatch.setattr(state, "utter", utter)
    for message_count in (1, 2, 3):
        assert not service.run(WEEK.week_id, ROOM_ID, now=START)
        assert len(state.room.messages) == message_count
        assert state.room.attempts == 1 and not state.room.complete
    for _ in range(3):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.state == "ready"
    assert [call for call in state.calls if isinstance(call, int)] == list(range(9))


def test_failing_second_batched_turn_gets_exactly_three_attempts(monkeypatch):
    state = cast(Any, State(snapshot(scores=(500, 500, 500))))
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    original_utter = state.utter
    failures = 0

    def utter(value, requester, room):
        nonlocal failures
        if len(room.messages) == 1:
            failures += 1
            raise MomotalkFailure("MOMOTALK_GENERATION_FAILED")
        return original_utter(value, requester, room)

    monkeypatch.setattr(state, "utter", utter)
    for attempt in (1, 2):
        assert not service.run(WEEK.week_id, ROOM_ID, now=START)
        assert state.room.attempts == attempt
    assert service.run(WEEK.week_id, ROOM_ID, now=START)
    assert failures == 3
    assert state.room.complete and state.room.state == "failed"
    assert len(state.room.messages) == 1


def test_enqueue_failure_after_rejection_resumes_without_resending_the_subject(monkeypatch):
    state = cast(Any, FallbackState())
    service = published(state)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    original_send = state.send

    def unavailable(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(state, "send", unavailable)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.images[1].moderation_blocked
    monkeypatch.setattr(state, "send", original_send)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and state.room.images[1].state == "ready"
    assert [call for call in state.image_calls if call[0] == "unhappy"] == [
        ("unhappy", "q" * 43),
        ("unhappy", "r" * 43),
    ]


def test_operator_claim_loss_keeps_the_rejection_for_the_next_repair(monkeypatch):
    state = cast(Any, FallbackState())
    state.image_failures.add("unhappy")
    service = published(state)
    for _ in range(4):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    state.image_failures.clear()
    state.image_calls.clear()
    original_claim = state.claim
    claims = 0

    def claim(room, now):
        nonlocal claims
        claims += 1
        return original_claim(room, now) if claims == 1 else None

    monkeypatch.setattr(state, "claim", claim)
    with pytest.raises(MomotalkFailure, match="MOMOTALK_REPAIR_BUSY"):
        service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", state, now=START)
    assert state.room.images[1].state == "failed" and state.room.images[1].moderation_blocked
    assert state.image_calls == [("unhappy", "q" * 43)]
    monkeypatch.setattr(state, "claim", original_claim)
    assert service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", state, now=START)
    assert state.image_calls == [("unhappy", "q" * 43), ("unhappy", "r" * 43)]


def test_preexisting_checkpoints_load_without_moderation_metadata():
    state = cast(Any, FallbackState())
    published(state)
    payload = state.room.model_dump()
    for image in payload["images"]:
        del image["moderation_blocked"]
        del image["alternate_topic_used"]
    restored = Room.model_validate(payload)
    assert restored.messages == state.room.messages and restored.plan == state.room.plan
    assert all(
        not image.moderation_blocked and not image.alternate_topic_used for image in restored.images
    )
