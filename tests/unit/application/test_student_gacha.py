"""Test recruitment probabilities and durable draw recovery."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from shittim_chest.application.ports import Clock
from shittim_chest.application.student_gacha import (
    StudentGachaWorker,
    _Discord,
    _draw_student,
    _Generator,
    _Media,
    _Store,
)
from shittim_chest.application.student_gacha_discord import GachaDeliveryFailure
from shittim_chest.application.student_gacha_models import (
    GachaGenerationFailure,
    GachaPresentation,
    StoredGachaImage,
    Student,
)
from shittim_chest.application.student_gacha_state import GachaJob, GachaJobKind
from shittim_chest.domain import ParticipantSlot

NOW = datetime(2026, 11, 14, tzinfo=UTC)
OFFER = "e" * 32
STORED = StoredGachaImage(f"student-gacha/results/{OFFER}.png", "v1", "f" * 64)


def test_student_draw_uses_exact_rarity_probabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    roster = tuple(
        Student(f"student-{rank}-{index}", "school", rank)
        for rank in range(5)
        for index in range(rank + 1)
    )
    expected = [0] * 5 + [1] * 55 + [2] * 30 + [3] * 8 + [4] * 2
    rolls = iter(range(100))
    groups: list[tuple[Student, ...]] = []

    def roll(limit: int) -> int:
        assert limit == 100
        return next(rolls)

    def select(candidates: tuple[Student, ...]) -> Student:
        groups.append(candidates)
        return candidates[-1]

    monkeypatch.setattr("shittim_chest.application.student_gacha.secrets.randbelow", roll)
    monkeypatch.setattr("shittim_chest.application.student_gacha.secrets.choice", select)

    rarities = [_draw_student(roster).rarity for _ in expected]

    assert rarities == expected
    assert Counter(rarities) == {0: 5, 1: 55, 2: 30, 3: 8, 4: 2}
    assert groups == [
        tuple(student for student in roster if student.rarity == rank) for rank in expected
    ]


def _job(**changes: object) -> GachaJob:
    return replace(
        GachaJob(
            job_id="d" * 32,
            kind=GachaJobKind.DRAW,
            guild_id="100",
            channel_id="101",
            after_message_id="102",
            teacher_name="試験先生",
            offer_id=OFFER,
            created_at=NOW,
            next_available_at=NOW,
            attempts=1,
        ),
        **changes,
    )


@pytest.fixture
def worker_fixture() -> tuple[StudentGachaWorker, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    store, media, generator, discord = (AsyncMock() for _ in range(4))
    clock = Mock()
    clock.now.return_value = NOW
    worker = StudentGachaWorker(
        store=cast(_Store, store),
        media=cast(_Media, media),
        generator=cast(_Generator, generator),
        discord=cast(_Discord, discord),
        clock=cast(Clock, clock),
        claim_owner="test-runtime",
    )
    store.checkpoint.side_effect = lambda old, new, **kwargs: new
    media.costume_reference.return_value = b"costume WebP"
    return worker, store, media, generator, discord


@pytest.mark.asyncio
async def test_stored_draw_resumes_after_discord_failure(
    worker_fixture: tuple[StudentGachaWorker, AsyncMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    worker, store, media, generator, discord = worker_fixture
    job = _job(stored=STORED, paid_started=True, loading_message_id="103")
    store.claim_next.return_value = job
    media.load.return_value = b"saved PNG"
    discord.send_result.side_effect = [
        GachaDeliveryFailure(retryable=True, ambiguous=True),
        "104",
    ]

    await worker.process_next()
    store.reschedule.assert_awaited_once()
    store.finish.assert_not_awaited()
    await worker.process_next()

    generator.render.assert_not_awaited()
    assert discord.send_result.await_count == 2
    assert discord.send_result.await_args_list[0] == discord.send_result.await_args_list[1]
    assert discord.send_result.await_args.kwargs["publication_id"] == OFFER
    assert [call[0] for call in discord.mock_calls[:4]] == [
        "delete_loading",
        "send_result",
        "delete_loading",
        "send_result",
    ]
    assert store.finish.await_args.kwargs["result_message_id"] == "104"


@pytest.mark.asyncio
async def test_card_preparation_failure_reopens_the_same_draw(
    worker_fixture: tuple[StudentGachaWorker, AsyncMock, AsyncMock, AsyncMock, AsyncMock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker, store, media, generator, discord = worker_fixture
    student = Student("キキョウ(水着)", "百鬼夜行")
    performer = ParticipantSlot.PARTICIPANT_A
    store.claim_next.return_value = _job(student=student, performer=performer)
    media.loading_gif.return_value = b"loop GIF"
    discord.send_loading.return_value = "103"
    generator.prepare.side_effect = GachaGenerationFailure("gacha_presentation_unavailable")

    await worker.process_next()

    generator.render.assert_not_awaited()
    discord.delete_loading.assert_awaited_once()
    job = store.finish.await_args.args[0]
    assert (job.offer_id, job.student, job.performer) == (OFFER, student, performer)
    assert job.teacher_name is None
    assert store.finish.await_args.kwargs["reopen"] is True
    assert "reason_code=gacha_presentation_unavailable" in caplog.text
    assert "試験先生" not in caplog.text


@pytest.mark.asyncio
async def test_ambiguous_paid_call_uses_saved_state_instead_of_rendering_again(
    worker_fixture: tuple[StudentGachaWorker, AsyncMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    worker, store, media, generator, discord = worker_fixture
    student = Student("アコ(ドレス)", "ゲヘナ")
    performer = ParticipantSlot.PARTICIPANT_C
    presentation = GachaPresentation(
        student,
        performer,
        "青いドレス",
        "戦略研究会",
        "サポーター",
        "HG",
        ("{teacher}、お待たせしました。", "準備は万全です。", "どうぞこちらへ。"),
    )
    store.claim_next.return_value = _job(student=student, performer=performer)
    media.loading_gif.return_value = b"loop GIF"
    discord.send_loading.return_value = "103"
    media.references.return_value = (b"layout", b"portrait")
    costume = b"costume WebP"
    media.costume_reference.return_value = costume
    generator.prepare.return_value = presentation
    generator.render.side_effect = GachaGenerationFailure("image_unavailable", ambiguous=True)

    await worker.process_next()
    generator.render.assert_awaited_once()
    media.costume_reference.assert_awaited_once_with(student)
    generator.prepare.assert_awaited_once_with(
        student=student, performer=performer, costume_reference=costume
    )
    assert generator.render.await_args.kwargs["costume_reference"] is costume
    store.finish.assert_not_awaited()
    pending = store.reschedule.await_args.args[0]
    assert pending.paid_started and pending.presentation == presentation

    store.claim_next.return_value = pending
    media.find.return_value = None
    await worker.process_next()

    generator.render.assert_awaited_once()
    assert store.finish.await_args.kwargs["reopen"] is True
    assert store.finish.await_args.args[0].teacher_name is None
    discord.delete_loading.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_costume_reference_read_leaves_image_request_unstarted(
    worker_fixture: tuple[StudentGachaWorker, AsyncMock, AsyncMock, AsyncMock, AsyncMock],
) -> None:
    worker, store, media, generator, discord = worker_fixture
    student = Student("アツコ\uff08水着\uff09", "アリウス", 1)
    performer = ParticipantSlot.PARTICIPANT_C
    store.claim_next.return_value = _job(student=student, performer=performer)
    media.loading_gif.return_value = b"loop GIF"
    discord.send_loading.return_value = "103"
    media.costume_reference.side_effect = GachaGenerationFailure("gacha_costume_source_unavailable")

    await worker.process_next()

    generator.prepare.assert_not_awaited()
    generator.render.assert_not_awaited()
    finished = store.finish.await_args.args[0]
    assert finished.student == student
    assert finished.performer is performer
    assert not finished.paid_started
    assert store.finish.await_args.kwargs["reopen"] is True
    discord.delete_loading.assert_awaited_once()
