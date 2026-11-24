"""Exercise recruitment offer and job transactions against DynamoDB Local."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.runtime_state import DynamoDbRuntimeStateRepository
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.adapters.dynamodb.student_gacha import DynamoDbStudentGachaRepository
from shittim_chest.adapters.dynamodb.student_gacha_activity import STUDENT_GACHA_ACTIVITY_KEY
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.scale_to_zero import RuntimeState, RuntimeStatus
from shittim_chest.application.student_gacha_models import (
    GACHA_SEXUAL_MODERATION_BLOCKED,
    GachaExpression,
    GachaPresentation,
    StoredGachaImage,
    Student,
)
from shittim_chest.application.student_gacha_state import GachaJobKind
from shittim_chest.domain import ParticipantSlot

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
OWNER = "fixture-opaque-owner"
RUNTIME_OWNER = "runtime"


@pytest.fixture
def runtime_repository(
    dynamodb_client: DynamoDBClient, dynamodb_table: str
) -> DynamoDbRuntimeStateRepository:
    return DynamoDbRuntimeStateRepository(client=dynamodb_client, table_name=dynamodb_table)


async def _start_runtime(repository: DynamoDbRuntimeStateRepository) -> RuntimeState:
    stopped = await repository.get()
    assert stopped is not None and stopped.status is RuntimeStatus.STOPPED
    starting = await repository.replace(expected=stopped, updated=stopped.resume_for_work(at=NOW))
    started = await repository.replace(
        expected=starting, updated=starting.mark_started(at=NOW, runtime_instance_id=RUNTIME_OWNER)
    )
    return await repository.replace(
        expected=started, updated=started.transition(RuntimeStatus.READY, at=NOW)
    )


@pytest.mark.asyncio
async def test_moderation_exclusion_and_award_preserve_the_original_draw(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    runtime_repository: DynamoDbRuntimeStateRepository,
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    other_worker = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    await _start_runtime(runtime_repository)
    blocked = Student("生徒(水着)", "トリニティ", rarity=3)
    performer = ParticipantSlot.PARTICIPANT_A
    expression = GachaExpression.NEUTRAL

    async def submit_open_offer(label: str, *, offer_id: str, owner: str, requester: str) -> None:
        items: tuple[DynamoItem, ...] = (
            {
                "PK": f"DEBATE#{label}",
                "SK": "META",
                "record_type": "debate_meta",
                "requester_id": requester,
            },
            {
                "PK": f"GACHA#OWNER#{owner}",
                "SK": "STATE",
                "active_offer_id": offer_id,
                "count": 0,
                "generation": 1,
                "version": 0,
            },
            {
                "PK": f"GACHA#OFFER#{offer_id}",
                "SK": "META",
                "owner_key": owner,
                "debate_id": label,
                "guild_id": "1",
                "channel_id": "2",
                "message_id": "3",
                "state": "open",
                "version": 0,
                "student": {"name": blocked.name, "school": blocked.school, "rarity": 3},
                "performer": performer.value,
                "expression": expression.value,
            },
        )
        for item in items:
            dynamodb_client.put_item(TableName=dynamodb_table, Item=marshal_item(item))
        assert await repository.submit(
            offer_id=offer_id,
            guild_id="1",
            channel_id="2",
            requester_id=requester,
            message_id="3",
            interaction_id=label,
            teacher_name="先生",
            at=NOW,
        )

    offer_id = "a" * 32
    await submit_open_offer("first", offer_id=offer_id, owner=OWNER, requester="4")
    draw = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert draw is not None and draw.student == blocked
    presentation = GachaPresentation(
        blocked, performer, "帽子", "部活", "STRIKER", "SR", ("{teacher}先生!", "一", "二")
    )
    paid = await repository.checkpoint(
        draw,
        replace(draw, paid_started=True, presentation=presentation, loading_message_id="5"),
        at=NOW,
    )

    legacy_offer_id = "b" * 32
    await submit_open_offer("second", offer_id=legacy_offer_id, owner="other-owner", requester="6")
    legacy = await other_worker.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert legacy is not None and legacy.offer_id == legacy_offer_id and legacy.student == blocked

    # A rejected compare-and-swap cannot add an exclusion on its own.
    current = await repository.checkpoint(paid, paid, at=NOW)
    with pytest.raises(RepositoryConflict):
        await repository.checkpoint(
            paid, replace(paid, failure_code=GACHA_SEXUAL_MODERATION_BLOCKED), at=NOW
        )
    assert await other_worker.excluded_costumes() == frozenset()
    moderated = await repository.checkpoint(
        current, replace(current, failure_code=GACHA_SEXUAL_MODERATION_BLOCKED), at=NOW
    )
    assert await other_worker.excluded_costumes() == {blocked.name}
    with pytest.raises(RepositoryConflict):
        await other_worker.checkpoint(legacy, replace(legacy, paid_started=True), at=NOW)
    with pytest.raises(ValueError, match="no uncertain result"):
        await repository.reroll(current, at=NOW)

    await repository.finish(moderated, at=NOW, park=True)
    assert await repository.resume_parked(offer_id, at=NOW)
    moderated = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert moderated is not None and moderated.job_id == draw.job_id
    assert moderated.failure_code == GACHA_SEXUAL_MODERATION_BLOCKED
    assert moderated.teacher_name == "先生"

    award_id = "c" * 32
    placeholder = StoredGachaImage(f"student-gacha/results/{award_id}.png", "placeholder", "1" * 64)
    await repository.reroll(moderated, at=NOW, award_id=award_id, image=placeholder)
    assert await repository.recovery_state(offer_id) == "claimed"
    assert await repository.recovery_state(award_id) == "redeemed"
    award_offer = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": f"GACHA#OFFER#{award_id}", "SK": "META"}),
        ConsistentRead=True,
    )["Item"]
    assert award_offer["owner_key"] == {"S": OWNER} and "result_message_id" not in award_offer

    with pytest.raises(RepositoryConflict):
        await repository.reroll(
            moderated, at=NOW + timedelta(microseconds=1), award_id=award_id, image=placeholder
        )
    rerolled = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert rerolled is not None and rerolled.job_id == draw.job_id and rerolled.attempts == 1
    assert rerolled.student is None and rerolled.presentation is None
    assert rerolled.stored is None and rerolled.failure_code is None and not rerolled.paid_started
    assert (rerolled.performer, rerolled.expression) == (performer, expression)
    assert rerolled.teacher_name == "先生"
    assert rerolled.loading_message_id is None
    assert rerolled.loading_publication_id == f"{award_id}:loading"

    later = NOW + timedelta(seconds=1)
    await other_worker.reroll(legacy, at=later)
    requeued = await other_worker.claim_next(claim_owner=RUNTIME_OWNER, at=later)
    assert requeued is not None and requeued.job_id == legacy.job_id and requeued.student is None
    await other_worker.finish(requeued, at=later, reopen=True)
    reopened = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item({"PK": f"GACHA#OFFER#{legacy_offer_id}", "SK": "META"}),
        ConsistentRead=True,
    )["Item"]
    assert "student" not in reopened and "presentation" not in reopened

    replacement = Student("生徒", blocked.school, rarity=1)
    generated = StoredGachaImage(f"student-gacha/results/{offer_id}.apng", "generated", "2" * 64)
    successful = await repository.checkpoint(
        rerolled,
        replace(
            rerolled,
            student=replacement,
            paid_started=True,
            stored=generated,
            result_message_id="7",
            teacher_name=None,
        ),
        at=later,
    )
    await repository.finish(successful, at=later, result_message_id="7")
    gallery = dynamodb_client.query(
        TableName=dynamodb_table,
        KeyConditionExpression="PK=:guild",
        ExpressionAttributeValues=marshal_item({":guild": "GACHA#GUILD#1"}),
        ConsistentRead=True,
    )["Items"]
    assert [
        (item["offer_id"]["S"], item["student"]["M"]["name"]["S"], item["stored"]["M"]["key"]["S"])
        for item in gallery
    ] == [(award_id, blocked.name, placeholder.key), (offer_id, replacement.name, generated.key)]


def _complete_first_debate(repository: DynamoDbStudentGachaRepository, *, at: datetime) -> None:
    repository._transact(
        *repository.admission_actions(
            debate_id="first-debate",
            owner_key=OWNER,
            guild_id="1",
            channel_id="2",
            status_message_id="3",
            at=at,
        )
    )
    repository._transact(
        *repository.completion_actions(
            debate_id="first-debate", guild_id="1", channel_id="2", at=at
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RuntimeStatus.READY, RuntimeStatus.BUSY])
async def test_runtime_must_finish_waking_before_claiming_queued_gacha(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    runtime_repository: DynamoDbRuntimeStateRepository,
    status: RuntimeStatus,
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    ready = await _start_runtime(runtime_repository)
    at = NOW + timedelta(seconds=1)
    idle = await runtime_repository.replace(expected=ready, updated=ready.begin_idle(at=at))

    _complete_first_debate(repository, at=at)
    assert await repository.claim_next(claim_owner=RUNTIME_OWNER, at=at) is None

    at += timedelta(seconds=1)
    starting = await runtime_repository.replace(expected=idle, updated=idle.resume_for_work(at=at))
    bound = await runtime_repository.replace(
        expected=starting, updated=starting.mark_started(at=at, runtime_instance_id=RUNTIME_OWNER)
    )
    assert await repository.claim_next(claim_owner=RUNTIME_OWNER, at=at) is None

    await runtime_repository.replace(expected=bound, updated=bound.transition(status, at=at))
    assert await repository.claim_next(claim_owner="stale-runtime", at=at) is None
    notice = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=at)
    assert notice is not None and notice.kind is GachaJobKind.PROGRESS
    assert notice.attempts == 1
    await repository.finish(notice, at=at)


@pytest.mark.asyncio
async def test_stale_idle_transition_waits_for_gacha_activity_to_finish(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    runtime_repository: DynamoDbRuntimeStateRepository,
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    ready = await _start_runtime(runtime_repository)
    at = NOW + timedelta(seconds=1)
    idle = ready.begin_idle(at=at)

    # Recruitment work can enqueue after the reconciler observes an empty queue.
    _complete_first_debate(repository, at=at)
    with pytest.raises(RepositoryConflict):
        await runtime_repository.replace(expected=ready, updated=idle)
    assert await runtime_repository.get() == ready

    notice = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=at)
    assert notice is not None
    with pytest.raises(RepositoryConflict):
        await runtime_repository.replace(expected=ready, updated=idle)

    await repository.finish(notice, at=at)
    assert await runtime_repository.replace(expected=ready, updated=idle) == idle
    assert await runtime_repository.get() == idle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_count", "expected_remaining"), [(0, 2), (1, 2), (2, 2), (3, 1), (4, None)]
)
async def test_legacy_gacha_progress_is_capped_at_three_before_completion(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    runtime_repository: DynamoDbRuntimeStateRepository,
    old_count: int,
    expected_remaining: int | None,
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    await _start_runtime(runtime_repository)
    repository._transact(
        repository._put_new(
            {
                "PK": f"GACHA#OWNER#{OWNER}",
                "SK": "STATE",
                "count": old_count,
                "generation": 1,
                "version": 2,
            }
        )
    )

    _complete_first_debate(repository, at=NOW)

    notice = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert notice is not None
    assert notice.remaining == expected_remaining
    if expected_remaining is None:
        assert notice.kind is GachaJobKind.OFFER and notice.offer_id is not None
    else:
        assert notice.kind is GachaJobKind.PROGRESS and notice.offer_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("rarity", [None, 0], ids=["legacy", "zero-star"])
async def test_cycle_eligibility_survives_contention_and_cannot_cross_a_reward(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
    runtime_repository: DynamoDbRuntimeStateRepository,
    rarity: int | None,
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)
    await _start_runtime(runtime_repository)

    def admit(debate_id: str, *, owner_key: str = OWNER) -> None:
        repository._transact(
            *repository.admission_actions(
                debate_id=debate_id,
                owner_key=owner_key,
                guild_id="1",
                channel_id="2",
                status_message_id="3",
                at=NOW,
            )
        )

    def complete(debate_id: str, *, at: datetime = NOW) -> None:
        actions = repository.completion_actions(
            debate_id=debate_id, guild_id="1", channel_id="2", at=at
        )
        if actions:
            repository._transact(*actions)

    for number in range(1, 3):
        admit(f"debate-{number}")
        complete(f"debate-{number}", at=NOW + timedelta(microseconds=number))
        complete(f"debate-{number}")
        notice = await repository.claim_next(
            claim_owner=RUNTIME_OWNER, at=NOW + timedelta(seconds=1)
        )
        assert notice is not None
        assert notice.kind is GachaJobKind.PROGRESS and notice.remaining == 3 - number
        await repository.finish(notice, at=NOW + timedelta(seconds=1))

    admit("third")
    admit("in-flight")
    third = repository.completion_actions(debate_id="third", guild_id="1", channel_id="2", at=NOW)
    competitor = repository.completion_actions(
        debate_id="in-flight", guild_id="1", channel_id="2", at=NOW
    )
    repository._transact(*third)
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        repository._transact(*competitor)
    notice = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert notice is not None and notice.kind is GachaJobKind.OFFER and notice.offer_id
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(
            {
                "PK": "DEBATE#third",
                "SK": "META",
                "record_type": "debate_meta",
                "requester_id": "4",
            }
        ),
    )
    notice = await repository.checkpoint(notice, replace(notice, offer_message_id="5"), at=NOW)
    await repository.finish(notice, at=NOW)
    offer_id = notice.offer_id
    assert offer_id is not None

    admit("unclaimed")
    reminder = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert reminder is not None and reminder.kind is GachaJobKind.OFFER
    assert reminder.offer_id == offer_id and reminder.previous_offer_message_id == "5"
    reminder = await repository.checkpoint(
        reminder, replace(reminder, offer_message_id="6"), at=NOW
    )
    await repository.finish(reminder, at=NOW)
    offer = await repository.get_offer(offer_id)
    assert offer is not None and offer.live and offer.message_id == "6"
    assert not await repository.claim_warning(
        offer_id=offer_id, guild_id="1", channel_id="2", message_id="5", at=NOW
    )
    assert await repository.claim_warning(
        offer_id=offer_id, guild_id="1", channel_id="2", message_id="6", at=NOW
    )
    assert not await repository.claim_warning(
        offer_id=offer_id, guild_id="1", channel_id="2", message_id="6", at=NOW
    )

    async def submit(interaction_id: str, *, actor_id: str = "4", message_id: str = "6") -> bool:
        return await repository.submit(
            offer_id=offer_id,
            guild_id="1",
            channel_id="2",
            requester_id=actor_id,
            message_id=message_id,
            interaction_id=interaction_id,
            teacher_name="テスト先生",
            at=NOW,
        )

    assert not await submit("70", actor_id="7")
    assert not await submit("71", message_id="5")
    submits = await asyncio.gather(submit("72"), submit("73"))
    assert sorted(submits) == [False, True]
    assert await submit("72" if submits[0] else "73")
    assert (claimed_offer := await repository.get_offer(offer_id)) and not claimed_offer.live

    interrupted = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert interrupted is not None and interrupted.kind is GachaJobKind.DRAW
    assert interrupted.expression is None
    await repository.finish(interrupted, at=NOW, park=True)
    assert await repository.resume_parked(offer_id, at=NOW)
    cleanup = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert cleanup is not None and cleanup.job_id == interrupted.job_id
    assert cleanup.failure_code == "gacha_recovery_without_name"
    assert cleanup.teacher_name is None and not cleanup.paid_started
    await repository.finish(cleanup, at=NOW, reopen=True)

    assert await submit("75")
    first_job = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=NOW)
    assert first_job is not None and first_job.kind is GachaJobKind.DRAW
    student = Student("コハル", "トリニティ", rarity=rarity)
    performer = ParticipantSlot.PARTICIPANT_A
    presentation = GachaPresentation(
        student,
        performer,
        "赤い帽子",
        "おやつ部",
        "STRIKER",
        "SR",
        ("{teacher}先生!", "はじめまして!", "よろしく!"),
    )
    stored = StoredGachaImage(f"student-gacha/results/{offer_id}.png", "version", "1" * 64)
    first_job = await repository.checkpoint(
        first_job,
        replace(
            first_job,
            student=student,
            performer=performer,
            expression=GachaExpression.SMILE,
            presentation=presentation,
            stored=stored,
            paid_started=True,
            teacher_name=None,
        ),
        at=NOW,
    )
    later = NOW + timedelta(minutes=6)
    second_job = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later)
    assert second_job is not None and second_job.stored == stored
    assert second_job.version > first_job.version
    assert second_job.student == student and second_job.presentation == presentation
    assert second_job.expression is GachaExpression.SMILE
    with pytest.raises(ValueError, match="cannot replace a saved selection"):
        await repository.checkpoint(
            second_job, replace(second_job, expression=GachaExpression.UNHAPPY), at=later
        )
    assert second_job.teacher_name is None
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        await repository.finish(first_job, at=NOW, reopen=True)

    assert not await repository.resume_parked(offer_id, at=later)
    await repository.finish(second_job, at=later, park=True)
    assert await repository.recovery_state(offer_id) == "delivery_unconfirmed"
    assert await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later) is None
    resumed = await asyncio.gather(
        repository.resume_parked(offer_id, at=later),
        repository.resume_parked(offer_id, at=later),
    )
    # The transaction token can return idempotent success to both identical writes.
    assert any(resumed)
    assert not await repository.resume_parked(offer_id, at=later)
    assert await repository.recovery_state(offer_id) == "claimed"
    recovered_job = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later)
    assert recovered_job is not None
    assert recovered_job.job_id == second_job.job_id
    assert recovered_job.version > second_job.version
    assert recovered_job.paid_started and recovered_job.stored == stored
    assert recovered_job.student == student and recovered_job.presentation == presentation
    assert recovered_job.expression is GachaExpression.SMILE
    assert recovered_job.after_message_id == "6" and recovered_job.result_after_message_id == "6"
    assert recovered_job.teacher_name is None
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        await repository.finish(second_job, at=later, park=True)

    await repository.finish(recovered_job, at=later, reopen=True)
    assert (open_offer := await repository.get_offer(offer_id)) and open_offer.live
    assert open_offer.message_id == "6"
    assert await submit("74")
    third_job = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later)
    assert third_job is not None and third_job.paid_started and third_job.stored == stored
    assert third_job.result_after_message_id == "6"
    assert third_job.student == student and third_job.presentation == presentation
    assert third_job.expression is GachaExpression.SMILE
    third_job = await repository.checkpoint(
        third_job, replace(third_job, result_message_id="8", teacher_name=None), at=later
    )
    await repository.finish(third_job, at=later, result_message_id="8")
    assert (redeemed := await repository.get_offer(offer_id)) and not redeemed.live
    gallery = dynamodb_client.query(
        TableName=dynamodb_table,
        KeyConditionExpression="PK=:guild",
        ExpressionAttributeValues=marshal_item({":guild": "GACHA#GUILD#1"}),
        ConsistentRead=True,
    )
    assert len(gallery["Items"]) == 1
    result = unmarshal_item(gallery["Items"][0])
    assert "expression" not in result
    assert result["offer_id"] == offer_id and result["performer"] == performer.value
    expected_student: DynamoItem = {"name": student.name, "school": student.school}
    if rarity is not None:
        expected_student["rarity"] = rarity
    assert result["student"] == expected_student
    assert result["stored"] == {
        "key": stored.key,
        "version_id": stored.version_id,
        "sha256": stored.sha256,
    }
    assert "teacher_name" not in result and "owner_key" not in result

    complete("unclaimed")
    complete("in-flight")
    assert await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later) is None
    admit("next")
    complete("next")
    admit("other", owner_key="another-opaque-owner")
    complete("other")
    for _ in range(2):
        progress = await repository.claim_next(claim_owner=RUNTIME_OWNER, at=later)
        assert progress is not None and progress.remaining == 2
        await repository.finish(progress, at=later)

    stored_activity = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(STUDENT_GACHA_ACTIVITY_KEY),
        ConsistentRead=True,
    )
    assert unmarshal_item(stored_activity["Item"])["count"] == 0
