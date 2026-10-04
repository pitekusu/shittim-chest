"""Moderation changes the subject once, with durable checkpoints and bounded cost."""

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.test_momotalk import ROOM_ID, START, WEEK, State, snapshot

from shittim_records.momotalk import (
    MAX_CHAIN_STEPS,
    PARTICIPANTS,
    ImageChoice,
    MomotalkFailure,
    Room,
    Turn,
    WeekDigest,
    question_chunks,
)
from shittim_records.momotalk_generation import (
    MomotalkGenerationService,
    collect_week,
    continue_week,
)


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

    def unavailable(*_args, **_kwargs):
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


@pytest.mark.parametrize("chunk_count", [2, 12])
@pytest.mark.parametrize("retry_preparation", [False, True])
def test_long_preparation_and_image_fallbacks_resume_in_bounded_chains(
    chunk_count, retry_preparation
):
    class QueuedState(FallbackState):
        def send(self, week_id, room_id, *, steps_remaining=MAX_CHAIN_STEPS):
            self.jobs.append((week_id, room_id, steps_remaining))

        def prepare(self, *args, final, **kwargs):
            if retry_preparation and args[2].attempts < 3:
                # claim increments the saved attempts before calling prepare.
                raise MomotalkFailure("MOMOTALK_GENERATION_FAILED")
            result = super().prepare(*args, final=True, **kwargs)
            return result if final else WeekDigest(summary=result.summary, images=result.images)

    state = cast(Any, QueuedState(both_images=True, turns=15))
    requester = state.snapshot.requesters[0]
    original = requester.questions[0]
    # Each synthetic record fits one chunk; pairs exceed its byte budget.
    requester.questions = [
        original.model_copy(
            update={
                "record_id": f"{index:043d}",
                "text": "読書の相談" * 200,
                "affection": {"synthetic": "x" * 13_000},
            }
        )
        for index in range(chunk_count)
    ]
    requester.questions[0] = original.model_copy(
        update={"text": "散歩の相談" * 200, "affection": {"synthetic": "x" * 13_000}}
    )
    assert len(question_chunks(requester.questions)) == chunk_count
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    chains = []
    while not state.room.complete:
        invocations = 0
        while state.jobs:
            week_id, room_id, remaining = state.jobs.pop(0)
            for receive_count in range(1, 5):
                succeeded = service.run(
                    week_id,
                    room_id,
                    now=START,
                    steps_remaining=remaining,
                    receive_count=receive_count,
                )
                invocations += 1
                if succeeded:
                    break
            assert succeeded
        chains.append(invocations)
        assert invocations <= MAX_CHAIN_STEPS
        if not state.room.complete:
            assert state.room.continuation_pending and state.room.lease_until == 0
            assert continue_week(WEEK.week_id, state, state, now=START) == 1
    assert len(chains) >= 2
    assert state.room.state == "ready" and len(state.room.messages) == 15
    assert all(image.state == "ready" for image in state.room.images)
    assert not state.room.continuation_pending
    assert len(state.image_calls) == 4 and len(state.reselections) == 2
    assert continue_week(WEEK.week_id, state, state, now=START) == 0


def test_exhausted_redelivery_pauses_without_a_provider_call_or_charging_an_attempt():
    state = cast(Any, FallbackState())
    service = published(state)
    before = state.calls.copy()
    attempts = state.room.attempts
    assert service.run(WEEK.week_id, ROOM_ID, now=START, steps_remaining=1, receive_count=2)
    assert state.room.continuation_pending and state.room.attempts == attempts
    assert state.calls == before


def test_continuation_tick_skips_unpaused_and_leased_rooms_and_recovers_failed_send(monkeypatch):
    state = cast(Any, FallbackState())
    service = published(state)
    assert continue_week(WEEK.week_id, state, state, now=START) == 0
    service.run(WEEK.week_id, ROOM_ID, now=START, steps_remaining=1)
    assert state.room.continuation_pending
    state.room.lease_until = int(START.timestamp()) + 1
    assert continue_week(WEEK.week_id, state, state, now=START) == 0
    state.room.lease_until = 0
    original_send = state.send

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(state, "send", unavailable)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        continue_week(WEEK.week_id, state, state, now=START)
    assert state.room.continuation_pending
    monkeypatch.setattr(state, "send", original_send)
    assert continue_week(WEEK.week_id, state, state, now=START) == 1
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert not state.room.continuation_pending


@pytest.mark.parametrize("event_age", [timedelta(0), timedelta(days=2), timedelta(seconds=-1)])
def test_continuation_handler_uses_fresh_schedule_and_never_collects_inputs(monkeypatch, event_age):
    from shittim_records import momotalk_handlers as handlers

    state = cast(Any, FallbackState())
    service = published(state)
    service.run(WEEK.week_id, ROOM_ID, now=START, steps_remaining=1)
    monkeypatch.setattr(handlers, "_components", lambda: (state, None, state, None, None, None))
    monkeypatch.setattr(
        state, "get_week", lambda week: state.week if week == WEEK.week_id else None
    )
    from datetime import datetime

    monkeypatch.setattr(
        handlers,
        "datetime",
        SimpleNamespace(now=lambda _zone: START, fromisoformat=datetime.fromisoformat),
    )
    event = {
        "source": "shittim.momotalk.continuation",
        "time": (START - event_age).isoformat(),
    }
    before = state.calls.copy()
    if event_age == timedelta(0):
        assert handlers.collect_handler(event, None) == {"state": "continued", "rooms": 1}
    else:
        with pytest.raises(RuntimeError, match="MOMOTALK_COLLECTION_FAILED"):
            handlers.collect_handler(event, None)
    assert state.calls == before


@pytest.mark.parametrize("hour", [0, 18])
def test_continuation_handler_includes_prior_week_at_sunday_rollover(monkeypatch, hour):
    from datetime import datetime

    from shittim_records import momotalk_handlers as handlers

    now = START + timedelta(hours=hour - 18)
    checked = []

    def resume(week, *_args, **_kwargs):
        checked.append(week)
        return int(week == WEEK.week_id - timedelta(days=7))

    monkeypatch.setattr(handlers, "continue_week", resume)
    monkeypatch.setattr(handlers, "_components", lambda: (None,) * 6)
    monkeypatch.setattr(
        handlers,
        "datetime",
        SimpleNamespace(now=lambda _zone: now, fromisoformat=datetime.fromisoformat),
    )
    assert handlers.collect_handler(
        {"source": "shittim.momotalk.continuation", "time": now.isoformat()}, None
    ) == {"state": "continued", "rooms": 1}
    assert checked == [WEEK.week_id, WEEK.week_id - timedelta(days=7)]


def test_preexisting_checkpoints_load_without_moderation_metadata():
    state = cast(Any, FallbackState())
    published(state)
    payload = state.room.model_dump()
    del payload["continuation_pending"]
    for image in payload["images"]:
        del image["moderation_blocked"]
        del image["alternate_topic_used"]
    restored = Room.model_validate(payload)
    assert restored.messages == state.room.messages and restored.plan == state.room.plan
    assert not restored.continuation_pending
    assert all(
        not image.moderation_blocked and not image.alternate_topic_used for image in restored.images
    )


def test_worker_job_accepts_old_messages_and_rejects_invalid_chain_budgets():
    from pydantic import ValidationError

    from shittim_records.momotalk_handlers import Job

    payload = {"weekId": str(WEEK.week_id), "roomId": ROOM_ID}
    assert Job.model_validate(payload).steps_remaining == MAX_CHAIN_STEPS
    for remaining in (0, MAX_CHAIN_STEPS + 1, True, "8"):
        with pytest.raises(ValidationError):
            Job.model_validate({**payload, "stepsRemaining": remaining})
