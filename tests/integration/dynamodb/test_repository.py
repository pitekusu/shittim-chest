"""DynamoDB Local coverage for transactions, leases, indexes, and outbox state."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient

from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateRepository,
    DynamoDbOutboxRepository,
    OutboxOperation,
    OutboxStatus,
)
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application import DebateSnapshot, DiscordBotSlot
from shittim_chest.application.ports import (
    RepositoryBusy,
    RepositoryConflict,
    RepositoryQuotaExceeded,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState

NOW = datetime(2026, 7, 17, 2, 0, tzinfo=UTC)


def new_snapshot(*, offset: int = 0) -> DebateSnapshot:
    created = NOW + timedelta(seconds=offset)
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=created),
        question=f"question-{offset}",
        requester_id="requester",
        guild_id="guild",
        channel_id="channel",
        created_at=created,
        attempt_created_at=created,
    )


@pytest.mark.asyncio
async def test_accept_replay_three_slots_and_terminal_release(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted: list[DebateSnapshot] = []
    for index in range(3):
        source = new_snapshot(offset=index)
        persisted = await repository.create(
            source,
            operation_id=f"accept-{index}",
            lease_owner=f"worker-{index}",
        )
        accepted.append(persisted)
        assert await repository.get(source.state.debate_id) == persisted

    replay = await repository.create(
        new_snapshot(offset=20),
        operation_id="accept-0",
        lease_owner="another-worker",
    )
    assert replay == accepted[0]

    with pytest.raises(RepositoryBusy):
        await repository.create(
            new_snapshot(offset=30),
            operation_id="accept-over-capacity",
            lease_owner="worker-4",
        )

    first = accepted[0]
    cancelled = replace(
        first,
        state=first.state.transition_to(
            DebatePhase.CANCELLED,
            at=first.state.updated_at + timedelta(seconds=1),
        ),
    )
    persisted_cancel = await repository.replace(
        expected=first,
        updated=cancelled,
        operation_id="cancel-0",
    )
    assert persisted_cancel.state.phase is DebatePhase.CANCELLED
    assert persisted_cancel.lease is None

    replacement = await repository.create(
        new_snapshot(offset=40),
        operation_id="accept-after-release",
        lease_owner="worker-4",
    )
    assert replacement.lease is not None
    assert first.lease is not None
    assert replacement.lease.slot == first.lease.slot


@pytest.mark.asyncio
async def test_daily_quota_condition_fails_closed(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    dynamodb_client.put_item(
        TableName=dynamodb_table,
        Item=marshal_item(
            {
                "PK": "QUOTA#GUILD#guild",
                "SK": "DAY#2026-07-17",
                "record_type": "guild_daily_quota",
                "schema_version": 2,
                "count": 30,
            }
        ),
    )
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)

    with pytest.raises(RepositoryQuotaExceeded):
        await repository.create(
            new_snapshot(),
            operation_id="quota-exhausted",
            lease_owner="worker",
        )


@pytest.mark.asyncio
async def test_failed_attempt_retry_is_atomic_and_does_not_consume_quota(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        new_snapshot(),
        operation_id="accept",
        lease_owner="worker-1",
    )
    failed = replace(
        accepted,
        state=accepted.state.transition_to(
            DebatePhase.FAILED,
            at=accepted.state.updated_at + timedelta(seconds=1),
        ),
        error_code="test_failure",
    )
    persisted_failed = await repository.replace(expected=accepted, updated=failed)
    retry_state = persisted_failed.state.new_retry_attempt(
        AttemptId.new(),
        at=persisted_failed.state.updated_at + timedelta(seconds=1),
    )
    retry = replace(
        persisted_failed,
        state=retry_state,
        attempt_created_at=retry_state.updated_at,
        lease=None,
        error_code=None,
    )

    persisted_retry = await repository.create_retry(
        expected_failed=persisted_failed,
        retry=retry,
        operation_id="retry",
        lease_owner="worker-2",
    )

    assert persisted_retry.state.retry_of == persisted_failed.state.attempt_id
    assert persisted_retry.lease is not None
    assert await repository.get_operation_result("retry") == persisted_retry
    assert (
        await repository.create_retry(
            expected_failed=persisted_failed,
            retry=replace(retry, state=replace(retry.state, attempt_id=AttemptId.new())),
            operation_id="retry",
            lease_owner="worker-3",
        )
        == persisted_retry
    )


@pytest.mark.asyncio
async def test_recoverable_gsi_claim_and_lease_renewal_are_fenced(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    repository = DynamoDbDebateRepository(client=dynamodb_client, table_name=dynamodb_table)
    accepted = await repository.create(
        new_snapshot(),
        operation_id="accept",
        lease_owner="old-worker",
    )
    assert accepted.lease is not None
    expired = (NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    for key in (
        {
            "PK": f"DEBATE#{accepted.state.debate_id}",
            "SK": f"ATTEMPT#{accepted.state.attempt_id}#META",
        },
        {"PK": "CONTROL#GLOBAL", "SK": f"SLOT#{accepted.lease.slot}"},
    ):
        dynamodb_client.update_item(
            TableName=dynamodb_table,
            Key=marshal_item(key),
            UpdateExpression="SET lease_expiry=:expired",
            ExpressionAttributeValues=marshal_item({":expired": expired}),
        )

    claimed = await repository.claim_recoverable(lease_owner="new-worker", at=NOW)
    assert len(claimed) == 1
    assert claimed[0].lease is not None
    assert claimed[0].lease.owner_id == "new-worker"
    assert claimed[0].lease.fencing_token == accepted.lease.fencing_token + 1
    stale_update = replace(
        accepted,
        state=accepted.state.transition_to(
            DebatePhase.PREPARING_EVIDENCE,
            at=NOW + timedelta(seconds=1),
        ),
    )
    with pytest.raises(RepositoryConflict):
        await repository.replace(expected=accepted, updated=stale_update)

    renewed = await repository.renew_lease(
        expected=claimed[0],
        at=NOW + timedelta(seconds=20),
    )
    assert renewed.expires_at == NOW + timedelta(seconds=80)
    current = await repository.get(accepted.state.debate_id)
    assert current is not None
    assert current.lease == renewed
    advanced = replace(
        current,
        state=current.state.transition_to(
            DebatePhase.PREPARING_EVIDENCE,
            at=NOW + timedelta(seconds=21),
        ),
    )
    await repository.replace(expected=current, updated=advanced)
    reloaded = await repository.get(accepted.state.debate_id)
    assert reloaded is not None
    assert reloaded.lease == renewed


@pytest.mark.asyncio
async def test_outbox_enforces_chunk_order_claim_retry_and_idempotent_completion(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    debate_repository = DynamoDbDebateRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    outbox_repository = DynamoDbOutboxRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    snapshot = await debate_repository.create(
        new_snapshot(),
        operation_id="accept",
        lease_owner="worker-1",
    )

    def operation(sequence: int) -> OutboxOperation:
        content = f"chunk-{sequence}"
        return OutboxOperation(
            operation_id=f"post-{sequence}",
            debate_id=snapshot.state.debate_id,
            attempt_id=snapshot.state.attempt_id,
            bot_slot=DiscordBotSlot.MODERATOR,
            thread_id="102",
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            nonce="A" * 22 if sequence == 0 else "B" * 22,
            chunk_sequence=sequence,
            status=OutboxStatus.PREPARED,
            created_at=NOW,
        )

    first = await outbox_repository.prepare(expected=snapshot, operation=operation(0))
    second = await outbox_repository.prepare(expected=snapshot, operation=operation(1))
    assert (
        await outbox_repository.claim(
            expected=snapshot,
            operation_id=second.operation_id,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=1),
        )
        is None
    )

    claimed = await outbox_repository.claim(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    assert claimed.status is OutboxStatus.CLAIMED
    rescheduled = await outbox_repository.reschedule(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=2),
        next_retry_at=NOW + timedelta(seconds=5),
    )
    assert rescheduled.status is OutboxStatus.PREPARED
    assert (
        await outbox_repository.claim(
            expected=snapshot,
            operation_id=first.operation_id,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=4),
        )
        is None
    )

    reclaimed = await outbox_repository.claim(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        at=NOW + timedelta(seconds=5),
    )
    assert reclaimed is not None
    sent = await outbox_repository.mark_sent(
        expected=snapshot,
        operation_id=first.operation_id,
        claim_owner="publisher",
        message_id="104",
        at=NOW + timedelta(seconds=6),
    )
    assert sent.status is OutboxStatus.SENT
    assert (
        await outbox_repository.mark_sent(
            expected=snapshot,
            operation_id=first.operation_id,
            claim_owner="publisher",
            message_id="104",
            at=NOW + timedelta(seconds=7),
        )
        == sent
    )

    assert (
        await outbox_repository.claim(
            expected=snapshot,
            operation_id=second.operation_id,
            claim_owner="publisher",
            at=NOW + timedelta(seconds=7),
        )
        is not None
    )
