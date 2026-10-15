"""Weekly boundaries, restart checkpoints, and the private publication boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.test_http_api import NOW, SESSION_KEY, FakeSessionStore, event, session

from shittim_records.auth import SESSION_COOKIE_NAME
from shittim_records.http_api import ReadHttpController
from shittim_records.momotalk import (
    PARTICIPANTS,
    ConversationPlan,
    ImageChoice,
    ImageSubject,
    MomotalkFailure,
    Question,
    RequesterInput,
    Room,
    SavedImage,
    Turn,
    Utterance,
    WeeklyInput,
    image_key,
    image_targets,
    question_chunks,
    validate_image_choices,
    week_for_schedule,
)
from shittim_records.momotalk_generation import MomotalkGenerationService, collect_week
from shittim_records.momotalk_read import MomotalkReadService


@pytest.mark.parametrize("failure,existing", [(False, False), (True, False), (False, True)])
def test_operator_image_repair_is_one_attempt_and_preserves_published_chat(failure, existing):
    state = State(legacy_images=True)
    state.image_failures.add("unhappy")
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for _ in range(30):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        assert state.room is not None
        if state.room.complete:
            break
    assert state.room is not None
    before = state.room.model_copy(deep=True)
    state.calls.clear()
    state.jobs.clear()
    if not failure:
        state.image_failures.clear()
    if existing:
        boundary.image_exists = lambda *_: True
    succeeded = service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", boundary, now=START)
    assert succeeded is (not failure)
    assert state.calls == ([] if existing else ["unhappy"])
    assert state.jobs == []
    assert state.room is not None
    assert state.room.messages == before.messages
    assert state.room.plan == before.plan
    assert state.room.images[0] == before.images[0]
    assert state.room.images[1].state == ("failed" if failure else "ready")
    assert state.room.complete and state.room.state == "ready" and state.room.attempts == 0


def test_image_repair_rejects_missing_original_question_before_generating():
    state = State(legacy_images=True)
    state.image_failures.add("unhappy")
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for _ in range(30):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        assert state.room is not None
        if state.room.complete:
            break
    state.snapshot.requesters[0].questions.clear()
    state.calls.clear()
    with pytest.raises(MomotalkFailure, match="MOMOTALK_REPAIR_INVALID"):
        service.retry_failed_image(WEEK.week_id, ROOM_ID, "unhappy", boundary, now=START)
    assert state.calls == []
    assert state.room is not None
    assert state.room.images[1].state == "failed"


START = datetime(2026, 9, 13, 9, tzinfo=UTC)
WEEK = week_for_schedule(START)
ROOM_ID = "a" * 43


def snapshot(*, scores=(850, 150, 500), questions=True) -> WeeklyInput:
    return WeeklyInput(
        week=WEEK,
        personas=dict(
            zip(
                PARTICIPANTS,
                ("架空の明るい人格", "架空の冷静な人格", "架空の雄弁な人格"),
                strict=True,
            )
        ),
        requesters=[
            RequesterInput(
                room_id=ROOM_ID,
                display_name="テスト利用者",
                scores=dict(zip(PARTICIPANTS, scores, strict=True)),
                questions=[
                    Question(
                        record_id="q" * 43,
                        text="秋に散歩するならどこがいい?",
                        completed_at=START - timedelta(days=1),
                    )
                ]
                if questions
                else [],
            )
        ],
    )


class State:
    """Small fake at persistence/provider boundaries, with real stored validation."""

    def __init__(self, value=None, *, legacy_images=False):
        self.snapshot = value or snapshot()
        self.legacy_images = legacy_images
        self.week: dict[str, Any] | None = None
        self.room = None
        self.jobs = []
        self.deleted = []
        self.calls = []
        self.image_failures = set()
        self.image_participant_calls = []
        self.generated_subjects = []
        self.stored_images = set()

    def collect(self, _week):
        return self.snapshot

    def freeze(self, value):
        return value, "frozen-version"

    def load_input(self, *_args):
        return self.snapshot, "frozen-version"

    def delete_input(self, *args):
        self.deleted.append(args)

    def get_week(self, _week):
        return self.week

    def create_week(self, value, version):
        self.week = {
            "week": value.week.model_dump(),
            "enqueued": False,
            "input_version": version,
            "room_ids": [ROOM_ID],
        }
        return self.week

    def mark_enqueued(self, _week):
        assert self.week is not None
        self.week["enqueued"] = True

    def create_room(self, week, requester):
        if self.room is None:
            targets = image_targets(requester, week.week_id)
            if self.legacy_images:
                # Historical checkpoints contained only one individual image per mood.
                historical = []
                for target in targets:
                    for subject in target.image_subjects:
                        if not any(image.mood == subject.mood for image in historical):
                            historical.append(SavedImage(**subject.model_dump()))
                targets = historical
            self.room = Room(
                week_id=week.week_id,
                room_id=requester.room_id,
                display_name=requester.display_name,
                question_count=len(requester.questions),
                images=targets,
            )

    def get_room(self, *_args):
        assert self.room is not None
        return self.room.model_copy(deep=True)

    def all_rooms(self, _week):
        return iter([self.room] if self.room else [])

    def claim(self, room, _now):
        return room.model_copy(update={"attempts": room.attempts + 1})

    def save(self, room):
        self.room = Room.model_validate_json(room.model_dump_json())

    def send(self, *args, **_kwargs):
        self.jobs.append(args)

    def prepare(self, _snapshot, requester, room, questions, *, final):
        self.calls.append("prepare")
        assert final
        return ConversationPlan(
            summary="架空の散歩の質問について雑談。" if questions else "今週の質問は0件。",
            images=[
                ImageChoice(
                    mood=i.mood, record_id=requester.questions[0].record_id, brief="散歩道で自撮り"
                )
                for i in room.images
            ],
            turns=[
                Turn(participant=PARTICIPANTS[i], topic="散歩の話題を受ける")
                for i in (0, 1, 0, 2, 1, 2, 0, 2, 1)
            ],
        )

    def utter(self, _snapshot, _requester, room):
        self.calls.append(len(room.messages))
        return Utterance(text=f"架空の発言{len(room.messages) + 1}です。")

    def image_exists(self, room, image):
        return image_key(room, image) in self.stored_images

    def selfie(self, _snapshot, _requester, image, _choice):
        self.calls.append(image.mood)
        self.image_participant_calls.append(image.participant)
        self.generated_subjects.append([subject.model_dump() for subject in image.image_subjects])
        if image.mood in self.image_failures:
            raise MomotalkFailure("MOMOTALK_IMAGE_FAILED")
        return b"fake image"

    def store_image(self, room, image, _content):
        self.stored_images.add(image_key(room, image))

    def image_url(self, *_args, **_kwargs):
        return "https://media.example.invalid/signed-image"


@pytest.mark.parametrize(
    "scores,moods",
    [
        ((799, 201, 500), []),
        ((800, 500, 500), ["happy"]),
        ((500, 200, 500), ["unhappy"]),
        ((800, 200, 500), ["happy", "unhappy"]),
        ((800, 800, 800), ["happy", "happy", "happy"]),
        ((200, 200, 200), ["unhappy", "unhappy", "unhappy"]),
        ((800, 800, 200), ["happy", "happy", "unhappy"]),
        ((0, 1000, 201), ["happy", "unhappy"]),
    ],
)
def test_thresholds_and_zero_questions(scores, moods):
    requester = snapshot(scores=scores).requesters[0]
    targets = image_targets(requester, WEEK.week_id)
    assert len(targets) == (1 if moods else 0)
    assert [subject.mood for image in targets for subject in image.image_subjects] == moods
    assert image_targets(requester.model_copy(update={"questions": []}), WEEK.week_id) == []


@pytest.mark.parametrize(
    "scores,slots",
    [
        ((850, 900, 800), ["participant-b", "participant-a", "participant-c"]),
        ((200, 100, 0), ["participant-c", "participant-b", "participant-a"]),
        ((800, 100, 900), ["participant-c", "participant-a", "participant-b"]),
    ],
)
def test_all_eligible_subjects_share_one_image_in_deterministic_order(scores, slots):
    requester = snapshot(scores=scores).requesters[0]
    targets = image_targets(requester, WEEK.week_id)
    assert len(targets) == 1
    assert [subject.participant for subject in targets[0].image_subjects] == slots


def test_checkpoint_rejects_multiple_images_with_the_same_mood():
    state = State(legacy_images=True)
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    assert state.room is not None
    state.room.images[1].mood = "happy"
    with pytest.raises(ValidationError, match="duplicate image mood"):
        Room.model_validate_json(state.room.model_dump_json())


@pytest.mark.parametrize(
    "subjects",
    [
        [],
        [{"participant": "participant-a", "mood": "happy"}] * 2,
        [{"participant": "participant-b", "mood": "happy"}],
        [{"participant": "participant-a", "mood": "unhappy"}],
    ],
)
def test_saved_group_subjects_are_nonempty_unique_and_match_the_lead(subjects):
    with pytest.raises(ValidationError):
        SavedImage(participant="participant-a", mood="happy", subjects=subjects)


def test_legacy_image_without_subjects_retains_its_single_person():
    image = SavedImage(participant="participant-b", mood="unhappy")
    assert image.subjects is None
    assert image.image_subjects == [ImageSubject(participant="participant-b", mood="unhappy")]


@pytest.mark.parametrize("failed", [False, True])
def test_group_image_is_generated_once_and_late_failure_preserves_chat_and_roster(failed):
    state = cast(Any, State(snapshot(scores=(800, 800, 800))))
    if failed:
        state.image_failures.add("happy")
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    for _ in range(20):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.complete:
            break
        # Recreate the service to exercise the persisted group and per-person expressions.
        service = MomotalkGenerationService(state, state, state, state)
    assert state.room.complete and state.room.state == "ready"
    assert len(state.room.messages) == 9
    assert len(state.room.images) == 1
    assert state.room.images[0].state == ("failed" if failed else "ready")
    expected = [{"participant": slot, "mood": "happy"} for slot in PARTICIPANTS]
    assert state.generated_subjects == [expected] * (3 if failed else 1)
    before = state.image_participant_calls.copy()
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.image_participant_calls == before


def test_existing_checkpoint_keeps_its_original_image_roster_and_reuses_saved_image():
    state = cast(Any, State(snapshot(scores=(850, 900, 150)), legacy_images=True))
    collect_week(WEEK, state, state, state, state)
    state.stored_images.add(image_key(state.room, state.room.images[0]))
    service = MomotalkGenerationService(state, state, state, state)
    for _ in range(20):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.complete:
            break
    assert state.room.complete
    assert [(image.participant, image.mood) for image in state.room.images] == [
        ("participant-b", "happy"),
        ("participant-c", "unhappy"),
    ]
    assert all(image.subjects is None for image in state.room.images)
    assert state.image_participant_calls == ["participant-c"]


def test_saved_image_is_reused_after_its_checkpoint_save_fails(monkeypatch):
    state = cast(Any, State(snapshot(scores=(800, 800, 800))))
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    for _ in range(10):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.state == "ready":
            break
    original_save = state.save

    def unavailable(_room):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(state, "save", unavailable)
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.image_participant_calls == ["participant-a"]
    assert state.room.images[0].state == "pending"
    monkeypatch.setattr(state, "save", original_save)
    for _ in range(3):
        service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.room.complete and all(image.state == "ready" for image in state.room.images)
    assert state.image_participant_calls == ["participant-a"]


def test_repair_by_any_group_subject_preserves_the_frozen_roster_and_expressions():
    state = cast(Any, State(snapshot(scores=(800, 100, 900))))
    state.image_failures.add("happy")
    collect_week(WEEK, state, state, state, state)
    service = MomotalkGenerationService(state, state, state, state)
    for _ in range(20):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.complete:
            break
    before = state.room.model_copy(deep=True)
    state.image_participant_calls.clear()
    state.image_failures.clear()
    state.generated_subjects.clear()
    state.snapshot.requesters[0].scores = dict.fromkeys(PARTICIPANTS, 500)
    with pytest.raises(MomotalkFailure, match="MOMOTALK_REPAIR_INVALID"):
        service.retry_failed_image(
            WEEK.week_id, ROOM_ID, "happy", state, now=START, participant="participant-b"
        )
    assert state.image_participant_calls == []
    assert service.retry_failed_image(
        WEEK.week_id, ROOM_ID, "unhappy", state, now=START, participant="participant-b"
    )
    assert state.image_participant_calls == ["participant-c"]
    assert state.room.images[0].subjects == before.images[0].subjects
    assert state.generated_subjects == [
        [subject.model_dump() for subject in before.images[0].image_subjects]
    ]
    assert state.room.messages == before.messages and state.room.plan == before.plan
    assert all(image.state == "ready" for image in state.room.images)


def test_graphemes_balancing_and_week_boundaries():
    assert WEEK.period_start == START - timedelta(days=7)
    assert WEEK.period_end == START
    assert WEEK.publish_at == START + timedelta(hours=2)
    assert Utterance(text="👩‍💻" * 100).text == "👩‍💻" * 100
    assert Utterance(text="か\u3099\r\nはい").text == "が\nはい"
    with pytest.raises(ValidationError):
        Utterance(text="👩‍💻" * 101)
    with pytest.raises(ValidationError):
        ConversationPlan(
            summary="架空",
            images=[],
            turns=[Turn(participant="participant-a", topic="話") for _ in range(9)],
        )
    requester = snapshot(scores=(900, 900, 900)).requesters[0]
    with pytest.raises(ValidationError):
        ConversationPlan(
            summary="架空",
            images=[],
            turns=[Turn(participant=slot, topic="話") for slot in PARTICIPANTS * 3],
        )
    choices = [
        image_targets(requester, WEEK.week_id + timedelta(weeks=i))[0].participant for i in range(3)
    ]
    assert len(set(choices)) == 3
    assert image_targets(requester, WEEK.week_id) == image_targets(requester, WEEK.week_id)
    value = snapshot()
    value.requesters[0].questions[0].completed_at = START
    with pytest.raises(ValidationError):
        WeeklyInput.model_validate_json(value.model_dump_json())


def test_all_questions_are_chunked_and_image_subject_must_exist():
    requester = snapshot().requesters[0]
    questions = [
        requester.questions[0].model_copy(update={"record_id": f"{i:043d}", "text": "秋" * 1000})
        for i in range(30)
    ]
    chunks = question_chunks(questions)
    assert len(chunks) > 1
    assert [q.record_id for chunk in chunks for q in chunk] == [q.record_id for q in questions]
    with pytest.raises(MomotalkFailure):
        validate_image_choices(
            [ImageChoice(mood="happy", record_id="x" * 43, brief="無関係")],
            image_targets(snapshot(scores=(850, 500, 500)).requesters[0], WEEK.week_id),
            questions,
        )


@pytest.mark.parametrize("questions", [True, False])
def test_restart_each_turn_and_late_image_failure_preserves_chat(questions):
    state = State(snapshot(questions=questions), legacy_images=True)
    state.image_failures.add("unhappy")
    boundary = cast(Any, state)
    assert collect_week(WEEK, boundary, boundary, boundary, boundary) == 1
    assert state.room is not None
    assert collect_week(WEEK, boundary, boundary, boundary, boundary) == 0
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for _ in range(20):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        if state.room.complete:
            break
    assert state.room.state == "ready"
    assert [call for call in state.calls if isinstance(call, int)] == list(range(9))
    assert (
        state.room.images == []
        if not questions
        else [i.state for i in state.room.images] == ["ready", "failed"]
    )
    assert state.calls.count("unhappy") == (3 if questions else 0)
    calls = state.calls.copy()
    service.run(WEEK.week_id, ROOM_ID, now=START)
    assert state.calls == calls
    assert state.deleted[-1] == (WEEK.week_id, "frozen-version")
    assert state.snapshot.requesters[0].scores == snapshot().requesters[0].scores


def test_publication_gate_and_private_inputs_are_not_returned():
    state = State()
    boundary = cast(Any, state)
    collect_week(WEEK, boundary, boundary, boundary, boundary)
    service = MomotalkGenerationService(boundary, boundary, boundary, boundary)
    for _ in range(16):
        service.run(WEEK.week_id, ROOM_ID, now=START)
        assert state.room is not None
        if state.room.state == "ready":
            break
    reader = MomotalkReadService(
        boundary,
        boundary,
        cast(Any, SimpleNamespace(avatar_url=lambda **_: "https://media.example.invalid/avatar")),
    )
    with pytest.raises(MomotalkFailure, match="MOMOTALK_NOT_FOUND"):
        reader.get_room(
            week_id=str(WEEK.week_id),
            room_id=ROOM_ID,
            now=WEEK.publish_at - timedelta(microseconds=1),
        )
    result = reader.get_room(week_id=str(WEEK.week_id), room_id=ROOM_ID, now=WEEK.publish_at)
    assert len(result.messages) == 9
    assert all(image.url is None for image in result.images)
    assert all(
        private not in result.model_dump_json()
        for private in ("personas", "scores", "brief", "questions", "frozen-version", "MOMOTALK#")
    )
    service.run(WEEK.week_id, ROOM_ID, now=WEEK.publish_at)
    assert (
        reader.get_room(week_id=str(WEEK.week_id), room_id=ROOM_ID, now=WEEK.publish_at)
        .images[0]
        .url
    )


@pytest.mark.parametrize(
    "route",
    [
        "GET /api/v1/momotalk/weeks",
        "GET /api/v1/momotalk/weeks/{weekId}/rooms",
        "GET /api/v1/momotalk/weeks/{weekId}/rooms/{roomId}",
    ],
)
def test_all_routes_require_session_and_are_no_store(route):
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(None)), session_key=SESSION_KEY, records=cast(Any, None)
    )
    result = controller.handle(event(route), now=START)
    assert result["statusCode"] == 401
    assert result["headers"]["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("query", ["limit=1&limit=2", "extra=x", "cursor=x&cursor=y"])
def test_invalid_query_rejected_for_authenticated_user(query):
    controller = ReadHttpController(
        store=cast(Any, FakeSessionStore(session())),
        session_key=SESSION_KEY,
        records=cast(Any, None),
        momotalk=cast(Any, object()),
    )
    result = controller.handle(
        event(
            "GET /api/v1/momotalk/weeks",
            query=query,
            cookies=[f"{SESSION_COOKIE_NAME}=session-token"],
        ),
        now=NOW,
    )
    assert result["statusCode"] == 400
