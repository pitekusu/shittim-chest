"""Exercise recruitment offer and job transactions against DynamoDB Local."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.adapters.dynamodb.student_gacha import DynamoDbStudentGachaRepository
from shittim_chest.adapters.dynamodb.student_gacha_activity import STUDENT_GACHA_ACTIVITY_KEY
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.application.student_gacha_models import (
    GachaPresentation,
    StoredGachaImage,
    Student,
)
from shittim_chest.application.student_gacha_state import GachaJobKind
from shittim_chest.domain import ParticipantSlot

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
OWNER = "fixture-opaque-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("rarity", [None, 0], ids=["legacy", "zero-star"])
async def test_cycle_eligibility_survives_contention_and_cannot_cross_a_reward(
    dynamodb_client: DynamoDBClient, dynamodb_table: str, rarity: int | None
) -> None:
    repository = DynamoDbStudentGachaRepository(client=dynamodb_client, table_name=dynamodb_table)

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

    for number in range(1, 5):
        admit(f"debate-{number}")
        complete(f"debate-{number}", at=NOW + timedelta(microseconds=number))
        complete(f"debate-{number}")
        notice = await repository.claim_next(claim_owner="runtime", at=NOW + timedelta(seconds=1))
        assert notice is not None
        assert notice.kind is GachaJobKind.PROGRESS and notice.remaining == 5 - number
        await repository.finish(notice, at=NOW + timedelta(seconds=1))

    admit("fifth")
    admit("in-flight")
    fifth = repository.completion_actions(debate_id="fifth", guild_id="1", channel_id="2", at=NOW)
    competitor = repository.completion_actions(
        debate_id="in-flight", guild_id="1", channel_id="2", at=NOW
    )
    repository._transact(*fifth)
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        repository._transact(*competitor)
    notice = await repository.claim_next(claim_owner="runtime", at=NOW)
    assert notice is not None and notice.kind is GachaJobKind.OFFER and notice.offer_id
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(
            {
                "PK": "DEBATE#fifth",
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
    reminder = await repository.claim_next(claim_owner="runtime", at=NOW)
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

    interrupted = await repository.claim_next(claim_owner="runtime", at=NOW)
    assert interrupted is not None and interrupted.kind is GachaJobKind.DRAW
    await repository.finish(interrupted, at=NOW, park=True)
    assert await repository.resume_parked(offer_id, at=NOW)
    cleanup = await repository.claim_next(claim_owner="runtime", at=NOW)
    assert cleanup is not None and cleanup.job_id == interrupted.job_id
    assert cleanup.failure_code == "gacha_recovery_without_name"
    assert cleanup.teacher_name is None and not cleanup.paid_started
    await repository.finish(cleanup, at=NOW, reopen=True)

    assert await submit("75")
    first_job = await repository.claim_next(claim_owner="first", at=NOW)
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
            presentation=presentation,
            stored=stored,
            paid_started=True,
            teacher_name=None,
        ),
        at=NOW,
    )
    later = NOW + timedelta(minutes=6)
    second_job = await repository.claim_next(claim_owner="second", at=later)
    assert second_job is not None and second_job.stored == stored
    assert second_job.student == student and second_job.presentation == presentation
    assert second_job.teacher_name is None
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        await repository.finish(first_job, at=NOW, reopen=True)

    assert not await repository.resume_parked(offer_id, at=later)
    await repository.finish(second_job, at=later, park=True)
    assert await repository.recovery_state(offer_id) == "delivery_unconfirmed"
    assert await repository.claim_next(claim_owner="runtime", at=later) is None
    resumed = await asyncio.gather(
        repository.resume_parked(offer_id, at=later),
        repository.resume_parked(offer_id, at=later),
    )
    # The transaction token can return idempotent success to both identical writes.
    assert any(resumed)
    assert not await repository.resume_parked(offer_id, at=later)
    assert await repository.recovery_state(offer_id) == "claimed"
    recovered_job = await repository.claim_next(claim_owner="recovered", at=later)
    assert recovered_job is not None
    assert recovered_job.job_id == second_job.job_id
    assert recovered_job.version > second_job.version
    assert recovered_job.paid_started and recovered_job.stored == stored
    assert recovered_job.student == student and recovered_job.presentation == presentation
    assert recovered_job.after_message_id == "6" and recovered_job.result_after_message_id == "6"
    assert recovered_job.teacher_name is None
    with pytest.raises(RepositoryConflict, match="gacha transaction conflict"):
        await repository.finish(second_job, at=later, park=True)

    await repository.finish(recovered_job, at=later, reopen=True)
    assert (open_offer := await repository.get_offer(offer_id)) and open_offer.live
    assert open_offer.message_id == "6"
    assert await submit("74")
    third_job = await repository.claim_next(claim_owner="third", at=later)
    assert third_job is not None and third_job.paid_started and third_job.stored == stored
    assert third_job.result_after_message_id == "6"
    assert third_job.student == student and third_job.presentation == presentation
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
    assert await repository.claim_next(claim_owner="runtime", at=later) is None
    admit("next")
    complete("next")
    admit("other", owner_key="another-opaque-owner")
    complete("other")
    for _ in range(2):
        progress = await repository.claim_next(claim_owner="runtime", at=later)
        assert progress is not None and progress.remaining == 4
        await repository.finish(progress, at=later)

    stored_activity = dynamodb_client.get_item(
        TableName=dynamodb_table,
        Key=marshal_item(STUDENT_GACHA_ACTIVITY_KEY),
        ConsistentRead=True,
    )
    assert unmarshal_item(stored_activity["Item"])["count"] == 0
