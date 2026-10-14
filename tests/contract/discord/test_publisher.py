"""Offline discord.py contracts for fenced outbox publication and reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid7

import discord
import pytest
from discord.http import handle_message_parameters

from shittim_chest.adapters.discord import (
    DiscordDeliveryConflict,
    DiscordDeliveryRejected,
    DiscordOutboxNotFound,
    DiscordPermissionDenied,
    DiscordPyPublisher,
    DiscordRateLimited,
    DiscordThreadLocked,
    DiscordThreadUnavailable,
    DiscordUnavailable,
)
from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    OUTBOX_CLAIM_SECONDS,
    DebateSnapshot,
    DiscordBotSlot,
    DiscordDeliveryTarget,
    LeaseGrant,
    OutboxOperation,
    OutboxRecoveryAbandoned,
    OutboxStatus,
    content_sha256,
    nonce_from_uuid7,
)
from shittim_chest.application.models import DeliveryAbandonReason
from shittim_chest.application.ports import (
    DebateImageStore,
    RepositoryCancellationCode,
    RepositoryConflict,
    RepositoryTransactionAction,
    RepositoryTransactionConflict,
    RepositoryTransactionStage,
    RepositoryUnavailable,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState

NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
GUILD_ID = "101"
CHANNEL_ID = "102"
THREAD_ID = "103"
MESSAGE_ID = 104
CLAIM_OWNER = "publisher-1"


@dataclass(slots=True)
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


@dataclass(slots=True)
class FakeResponse:
    status: int
    reason: str
    headers: dict[str, str]


@dataclass(slots=True)
class FakeOutboxRepository:
    operation: OutboxOperation | None
    fail_mark_once: bool = False
    mark_failures: list[Exception] = field(default_factory=list)
    claims: int = 0
    mark_attempts: int = 0
    mark_times: list[datetime] = field(default_factory=list)
    marks: list[str] = field(default_factory=list)
    retry_delays: list[timedelta] = field(default_factory=list)

    async def prepare(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
    ) -> OutboxOperation:
        del expected
        self.operation = operation
        return operation

    async def get(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        operation_id: str,
    ) -> OutboxOperation | None:
        operation = self.operation
        if operation is None:
            return None
        if (
            operation.debate_id != debate_id
            or operation.attempt_id != attempt_id
            or operation.operation_id != operation_id
        ):
            return None
        return operation

    async def claim(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
    ) -> OutboxOperation | None:
        operation = await self.get(
            debate_id=expected.state.debate_id,
            attempt_id=expected.state.attempt_id,
            operation_id=operation_id,
        )
        if operation is None or operation.status is OutboxStatus.SENT:
            return None
        if operation.next_retry_at is not None and operation.next_retry_at > at:
            return None
        if (
            operation.status is OutboxStatus.CLAIMED
            and operation.claim_expires_at is not None
            and operation.claim_expires_at >= at
        ):
            return None
        self.claims += 1
        self.operation = replace(
            operation,
            status=OutboxStatus.CLAIMED,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(seconds=OUTBOX_CLAIM_SECONDS),
            delivery_attempt=operation.delivery_attempt + 1,
            next_retry_at=None,
        )
        return self.operation

    async def mark_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        del expected
        self.mark_attempts += 1
        self.mark_times.append(at)
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RepositoryConflict("injected completion failure")
        if self.mark_failures:
            raise self.mark_failures.pop(0)
        if self.operation != operation:
            raise RepositoryConflict("claim generation mismatch")
        self.marks.append(message_id)
        self.operation = replace(
            operation,
            status=OutboxStatus.SENT,
            claim_owner=None,
            claim_expires_at=None,
            message_id=message_id,
            sent_at=at,
        )
        return self.operation

    async def mark_reconciled_sent(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        del expected
        if self.operation != operation:
            raise RepositoryConflict("operation generation mismatch")
        self.marks.append(message_id)
        self.operation = replace(
            operation,
            status=OutboxStatus.SENT,
            claim_owner=None,
            claim_expires_at=None,
            next_retry_at=None,
            message_id=message_id,
            sent_at=at,
        )
        return self.operation

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation: OutboxOperation,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        del expected
        if self.operation != operation:
            raise RepositoryConflict("claim generation mismatch")
        self.retry_delays.append(next_retry_at - at)
        self.operation = replace(
            operation,
            status=OutboxStatus.PREPARED,
            claim_owner=None,
            claim_expires_at=None,
            next_retry_at=next_retry_at,
        )
        return self.operation

    async def list_pending(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        operation = await self.get(
            debate_id=debate_id,
            attempt_id=attempt_id,
            operation_id=self.operation.operation_id if self.operation else "missing",
        )
        if operation is None or operation.status is OutboxStatus.SENT:
            return ()
        return (operation,)

    def expire_claim(self, *, at: datetime) -> None:
        operation = self.operation
        if operation is None or operation.status is not OutboxStatus.CLAIMED:
            raise AssertionError("test operation is not claimed")
        self.operation = replace(operation, claim_expires_at=at - timedelta(microseconds=1))


def snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="question",
        requester_id="105",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        created_at=NOW,
        attempt_created_at=NOW,
        thread_id=THREAD_ID,
        lease=LeaseGrant(CLAIM_OWNER, 0, 1, NOW + timedelta(minutes=10)),
    )


def prepared(expected: DebateSnapshot) -> OutboxOperation:
    content = "hello <@123> @everyone"
    return OutboxOperation(
        operation_id="decision-0000",
        debate_id=expected.state.debate_id,
        attempt_id=expected.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id=THREAD_ID,
        content=content,
        content_hash=content_sha256(content),
        nonce=nonce_from_uuid7(uuid7()),
        chunk_sequence=0,
        status=OutboxStatus.PREPARED,
        created_at=NOW,
    )


def prepared_v2(
    expected: DebateSnapshot,
    *,
    created_at: datetime = NOW,
    delivery_attempt: int = 0,
) -> OutboxOperation:
    return replace(
        prepared(expected),
        created_at=created_at,
        delivery_attempt=delivery_attempt,
        record_schema_version=2,
        phase=DebatePhase.COMPLETED,
        plan_id="terminal-completed",
        delivery_sequence=300,
        deadline_at=created_at + timedelta(minutes=15),
    )


def prepared_channel_v3(expected: DebateSnapshot) -> OutboxOperation:
    return replace(
        prepared_v2(expected),
        operation_id="terminal-completed-affection-0000",
        record_schema_version=3,
        delivery_sequence=320,
        delivery_target=DiscordDeliveryTarget.CHANNEL,
        channel_id=CHANNEL_ID,
    )


def message(
    operation: OutboxOperation,
    *,
    user_id: int = 201,
    message_id: int = MESSAGE_ID,
    content: str | None = None,
) -> discord.Message:
    return cast(
        discord.Message,
        SimpleNamespace(
            id=message_id,
            channel=SimpleNamespace(id=int(operation.delivery_target_id)),
            author=SimpleNamespace(id=user_id),
            nonce=operation.nonce,
            content=operation.content if content is None else content,
        ),
    )


def clients_and_thread(
    operation: OutboxOperation,
    *,
    history_messages: tuple[discord.Message, ...] = (),
) -> tuple[Mapping[DiscordBotSlot, discord.Client], discord.Thread, AsyncMock]:
    thread_mock = MagicMock(spec=discord.Thread)
    thread_mock.guild = SimpleNamespace(id=int(GUILD_ID))
    thread_mock.locked = False

    async def history(
        *,
        limit: int,
        after: datetime,
        oldest_first: bool,
    ) -> AsyncIterator[discord.Message]:
        assert limit == 500
        assert after == operation.created_at
        assert oldest_first
        for item in history_messages:
            yield item

    thread_mock.history.side_effect = history
    send_mock = AsyncMock(return_value=message(operation))
    thread_mock.send = send_mock
    thread = cast(discord.Thread, thread_mock)

    clients: dict[DiscordBotSlot, discord.Client] = {}
    for index, slot in enumerate(DISCORD_BOT_SLOTS):
        client_mock = MagicMock(spec=discord.Client)
        client_mock.user = SimpleNamespace(id=201 + index)
        cast(Any, client_mock).http = SimpleNamespace(max_ratelimit_timeout=300.0)
        client_mock.is_ready.return_value = True
        client_mock.get_channel.return_value = thread if slot is operation.bot_slot else None
        client_mock.fetch_channel = AsyncMock(return_value=thread)
        clients[slot] = cast(discord.Client, client_mock)
    return clients, thread, send_mock


def clients_and_channel(
    operation: OutboxOperation,
) -> tuple[Mapping[DiscordBotSlot, discord.Client], discord.TextChannel, AsyncMock]:
    channel_mock = MagicMock(spec=discord.TextChannel)
    channel_mock.guild = SimpleNamespace(id=int(GUILD_ID))

    async def history(
        *,
        limit: int,
        after: datetime,
        oldest_first: bool,
    ) -> AsyncIterator[discord.Message]:
        assert limit == 500
        assert after == operation.created_at
        assert oldest_first
        if False:  # pragma: no cover - retain an async iterator without messages
            yield message(operation)

    channel_mock.history.side_effect = history
    send_mock = AsyncMock(return_value=message(operation))
    channel_mock.send = send_mock
    channel = cast(discord.TextChannel, channel_mock)

    clients: dict[DiscordBotSlot, discord.Client] = {}
    for index, slot in enumerate(DISCORD_BOT_SLOTS):
        client_mock = MagicMock(spec=discord.Client)
        client_mock.user = SimpleNamespace(id=201 + index)
        cast(Any, client_mock).http = SimpleNamespace(max_ratelimit_timeout=300.0)
        client_mock.is_ready.return_value = True
        client_mock.get_channel.return_value = channel if slot is operation.bot_slot else None
        client_mock.fetch_channel = AsyncMock(return_value=channel)
        clients[slot] = cast(discord.Client, client_mock)
    return clients, channel, send_mock


def publisher(
    operation: OutboxOperation,
    repository: FakeOutboxRepository,
    *,
    history_messages: tuple[discord.Message, ...] = (),
    clock: FakeClock | None = None,
    delivery_timeout_seconds: float = 45.0,
    completion_retry_seconds: float = 0.0,
    image_store: DebateImageStore | None = None,
) -> tuple[DiscordPyPublisher, discord.Thread, AsyncMock, FakeClock]:
    clients, thread, send_mock = clients_and_thread(
        operation,
        history_messages=history_messages,
    )
    test_clock = clock or FakeClock()
    return (
        DiscordPyPublisher(
            clients=clients,
            outbox=repository,
            clock=test_clock,
            claim_owner=CLAIM_OWNER,
            image_store=image_store,
            delivery_timeout_seconds=delivery_timeout_seconds,
            completion_retry_seconds=completion_retry_seconds,
        ),
        thread,
        send_mock,
        test_clock,
    )


def test_discord_py_payload_enforces_nonce_and_disables_every_mention() -> None:
    flags = discord.MessageFlags(suppress_embeds=True)
    with handle_message_parameters(
        content="hello <@123> @everyone",
        nonce="nonce",
        allowed_mentions=discord.AllowedMentions.none(),
        flags=flags,
    ) as parameters:
        assert parameters.payload is not None
        assert parameters.payload["nonce"] == "nonce"
        assert parameters.payload["enforce_nonce"] is True
        assert parameters.payload["allowed_mentions"] == {"parse": []}
        assert parameters.payload["flags"] == flags.value


def test_publisher_rejects_clients_with_unbounded_rate_limit_waits() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, _ = clients_and_thread(operation)
    cast(Any, clients[DiscordBotSlot.MODERATOR]).http.max_ratelimit_timeout = None

    with pytest.raises(ValueError, match="max_ratelimit_timeout"):
        DiscordPyPublisher(
            clients=clients,
            outbox=repository,
            clock=FakeClock(),
            claim_owner=CLAIM_OWNER,
        )


def test_publisher_rejects_a_delivery_timeout_that_can_outlive_the_claim() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, _ = clients_and_thread(operation)

    with pytest.raises(ValueError, match="shorter than the outbox claim"):
        DiscordPyPublisher(
            clients=clients,
            outbox=repository,
            clock=FakeClock(),
            claim_owner=CLAIM_OWNER,
            delivery_timeout_seconds=OUTBOX_CLAIM_SECONDS,
        )


@pytest.mark.parametrize("completion_retries", [True, 0, 11])
def test_publisher_rejects_invalid_completion_retry_counts(
    completion_retries: int,
) -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, _ = clients_and_thread(operation)

    with pytest.raises(ValueError, match="completion retries"):
        DiscordPyPublisher(
            clients=clients,
            outbox=repository,
            clock=FakeClock(),
            claim_owner=CLAIM_OWNER,
            completion_retries=completion_retries,
        )


def test_publisher_rejects_a_negative_completion_retry_delay() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, _ = clients_and_thread(operation)

    with pytest.raises(ValueError, match="completion retry delay"):
        DiscordPyPublisher(
            clients=clients,
            outbox=repository,
            clock=FakeClock(),
            claim_owner=CLAIM_OWNER,
            completion_retry_seconds=-0.1,
        )


@pytest.mark.asyncio
async def test_publisher_claims_sends_with_safe_mentions_and_completes() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)

    sent = await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert sent is not None
    assert sent.status is OutboxStatus.SENT
    assert sent.message_id == str(MESSAGE_ID)
    assert repository.claims == 1
    assert repository.marks == [str(MESSAGE_ID)]
    send_mock.assert_awaited_once()
    await_args = send_mock.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs["nonce"] == operation.nonce
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert kwargs["suppress_embeds"] is True


@pytest.mark.asyncio
async def test_publisher_sends_v3_affection_result_to_the_bound_parent_channel() -> None:
    expected = snapshot()
    operation = prepared_channel_v3(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, send_mock = clients_and_channel(operation)
    subject = DiscordPyPublisher(
        clients=clients,
        outbox=repository,
        clock=FakeClock(),
        claim_owner=CLAIM_OWNER,
    )

    sent = await subject.publish_persisted(
        expected=expected,
        operation_id=operation.operation_id,
    )

    assert sent is not None
    assert sent.status is OutboxStatus.SENT
    send_mock.assert_awaited_once()
    await_args = send_mock.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert kwargs["suppress_embeds"] is True


@pytest.mark.asyncio
async def test_publisher_rejects_a_parent_channel_outside_the_attempt_fence() -> None:
    expected = snapshot()
    operation = prepared_channel_v3(expected)
    repository = FakeOutboxRepository(operation)
    clients, _, send_mock = clients_and_channel(operation)
    subject = DiscordPyPublisher(
        clients=clients,
        outbox=repository,
        clock=FakeClock(),
        claim_owner=CLAIM_OWNER,
    )

    with pytest.raises(DiscordThreadUnavailable):
        await subject.publish_persisted(
            expected=replace(expected, channel_id="999"),
            operation_id=operation.operation_id,
        )

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_publisher_rejects_an_operation_that_was_not_persisted() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(None)
    subject, _, send_mock, _ = publisher(operation, repository)

    with pytest.raises(DiscordOutboxNotFound):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_reconciles_an_existing_message_without_another_send() -> None:
    expected = snapshot()
    operation = replace(
        prepared(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="old-publisher",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    existing = message(operation)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(existing,),
    )

    sent = await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert sent is not None
    assert sent.message_id == str(existing.id)
    assert sent.delivery_attempt == 2
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_on_same_nonce_with_different_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = snapshot()
    operation = replace(
        prepared(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="old-publisher",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    conflicting = message(operation, content="different")
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(conflicting,),
    )

    with pytest.raises(DiscordDeliveryConflict):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()

    event = json.loads(caplog.records[-1].message)
    assert event["stage"] == "history_reconciliation"
    assert event["reason"] == "message_body_mismatch"


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_on_duplicate_exact_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = snapshot()
    operation = replace(
        prepared(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="old-publisher",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(
            message(operation, message_id=MESSAGE_ID),
            message(operation, message_id=MESSAGE_ID + 1),
        ),
    )

    with pytest.raises(DiscordDeliveryConflict):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()

    event = json.loads(caplog.records[-1].message)
    assert event["stage"] == "history_reconciliation"
    assert event["reason"] == "history_duplicate_match"


@pytest.mark.asyncio
async def test_reconciliation_fails_closed_when_history_reaches_500_items(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = snapshot()
    operation = replace(
        prepared(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="old-publisher",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    unrelated = tuple(
        message(operation, user_id=999, message_id=1_000 + index) for index in range(500)
    )
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=unrelated,
    )

    with pytest.raises(DiscordDeliveryConflict):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()

    event = json.loads(caplog.records[-1].message)
    assert event["stage"] == "history_reconciliation"
    assert event["reason"] == "history_limit_reached"


@pytest.mark.asyncio
async def test_terminating_reconciliation_reads_history_without_posting() -> None:
    expected = snapshot()
    operation = replace(
        prepared_v2(expected, delivery_attempt=1),
        status=OutboxStatus.CLAIMED,
        claim_owner="old-publisher",
        claim_expires_at=NOW - timedelta(seconds=1),
    )
    existing = message(operation)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(existing,),
    )

    sent = await subject.reconcile_persisted(
        expected=expected,
        operation_id=operation.operation_id,
    )

    assert sent is not None and sent.status is OutboxStatus.SENT
    assert repository.claims == 0
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_deadline_caps_delivery_timeout_and_raises_typed_abandonment() -> None:
    expected = snapshot()
    created_at = NOW - timedelta(minutes=15) + timedelta(milliseconds=10)
    operation = prepared_v2(expected, created_at=created_at)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        delivery_timeout_seconds=1.0,
    )

    async def blocked_send(*args: object, **kwargs: object) -> discord.Message:
        del args, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    send_mock.side_effect = blocked_send
    with pytest.raises(OutboxRecoveryAbandoned) as captured:
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert captured.value.reason == DeliveryAbandonReason.DEADLINE_EXCEEDED.value
    assert repository.retry_delays == []
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_send_success_then_completion_failure_recovers_from_history() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation, fail_mark_once=True)
    created_message = message(operation)
    subject, _, send_mock, clock = publisher(
        operation,
        repository,
        history_messages=(created_message,),
    )
    send_mock.return_value = created_message

    with pytest.raises(RepositoryConflict):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    repository.expire_claim(at=NOW + timedelta(seconds=61))
    clock.current = NOW + timedelta(seconds=61)
    sent = await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert sent is not None
    assert sent.message_id == str(created_message.id)
    assert sent.delivery_attempt == 2
    assert send_mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion_failure",
    [
        RepositoryTransactionConflict(
            stage=RepositoryTransactionStage.OUTBOX_MARK_SENT,
            failures=(
                (
                    RepositoryTransactionAction.LEASE_FENCE,
                    RepositoryCancellationCode.TRANSACTION_CONFLICT,
                ),
            ),
            reasons_complete=True,
        ),
        RepositoryUnavailable(),
    ],
)
async def test_send_success_retries_only_completion_without_another_discord_post(
    completion_failure: Exception,
) -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation, mark_failures=[completion_failure])
    subject, _, send_mock, _ = publisher(operation, repository)

    sent = await subject.publish_persisted(
        expected=expected,
        operation_id=operation.operation_id,
    )

    assert sent is not None
    assert sent.status is OutboxStatus.SENT
    assert sent.message_id == str(MESSAGE_ID)
    assert repository.mark_attempts == 2
    assert repository.mark_times == [repository.mark_times[0]] * 2
    assert repository.marks == [str(MESSAGE_ID)]
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_send_success_bounds_completion_retries_without_another_discord_post() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(
        operation,
        mark_failures=[RepositoryUnavailable() for _ in range(4)],
    )
    subject, _, send_mock, _ = publisher(operation, repository)

    with pytest.raises(RepositoryUnavailable):
        await subject.publish_persisted(
            expected=expected,
            operation_id=operation.operation_id,
        )

    assert repository.mark_attempts == 4
    assert repository.mark_times == [repository.mark_times[0]] * 4
    assert repository.marks == []
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_rate_limit_is_rescheduled_once_after_sdk_exhaustion() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    response = cast(
        Any,
        FakeResponse(status=429, reason="Too Many Requests", headers={"Retry-After": "12.5"}),
    )
    send_mock.side_effect = discord.HTTPException(
        response,
        {"message": "rate limited", "code": 0},
    )

    with pytest.raises(DiscordRateLimited) as captured:
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert captured.value.retryable
    assert repository.retry_delays == [timedelta(seconds=12.5)]
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_preemptive_rate_limit_uses_the_sdk_retry_after_value() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    send_mock.side_effect = discord.RateLimited(75.0)

    with pytest.raises(DiscordRateLimited):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert repository.retry_delays == [timedelta(seconds=75)]
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_delivery_timeout_reschedules_before_the_outbox_claim_expires() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        delivery_timeout_seconds=0.01,
    )

    async def blocked_send(*args: object, **kwargs: object) -> discord.Message:
        del args, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    send_mock.side_effect = blocked_send

    with pytest.raises(DiscordUnavailable):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert repository.retry_delays == [timedelta(seconds=30)]
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_permission_failure_is_not_retried_or_rescheduled() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    response = cast(
        Any,
        FakeResponse(status=403, reason="Forbidden", headers={}),
    )
    send_mock.side_effect = discord.Forbidden(response, {"message": "forbidden", "code": 0})

    with pytest.raises(DiscordPermissionDenied) as captured:
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert not captured.value.retryable
    assert repository.retry_delays == []
    assert send_mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409, 500])
async def test_retryable_http_failure_is_rescheduled_without_a_publisher_loop(
    status: int,
) -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    response = cast(
        Any,
        FakeResponse(status=status, reason="retryable", headers={}),
    )
    send_mock.side_effect = discord.HTTPException(
        response,
        {"message": "retryable", "code": 0},
    )

    with pytest.raises(DiscordUnavailable):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert repository.retry_delays == [timedelta(seconds=30)]
    assert send_mock.await_count == 1


@pytest.mark.asyncio
async def test_nonretryable_http_rejection_keeps_the_claim_for_failure_handling() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    response = cast(
        Any,
        FakeResponse(status=400, reason="Bad Request", headers={}),
    )
    send_mock.side_effect = discord.HTTPException(
        response,
        {"message": "invalid", "code": 0},
    )

    with pytest.raises(DiscordDeliveryRejected) as captured:
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert not captured.value.retryable
    assert repository.retry_delays == []


@pytest.mark.asyncio
async def test_locked_thread_is_not_unlocked_or_sent_to() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(operation, repository)
    cast(Any, thread).locked = True

    with pytest.raises(DiscordThreadLocked):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_thread_from_another_guild_is_rejected_before_send() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(operation, repository)
    cast(Any, thread).guild = SimpleNamespace(id=999)

    with pytest.raises(DiscordThreadUnavailable):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_is_propagated_without_reschedule() -> None:
    expected = snapshot()
    operation = prepared(expected)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository)
    send_mock.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert repository.retry_delays == []
    assert send_mock.await_count == 1


def image_operation(expected: DebateSnapshot) -> OutboxOperation:
    import hashlib

    from shittim_chest.domain.attachments import DebateImage

    identity = "12345678-1234-5678-1234-abcdefabcdef"
    content = b"original image fixture"
    image = DebateImage(
        identity,
        "image/png",
        len(content),
        hashlib.sha256(content).hexdigest(),
        10,
        20,
        f"debate-images/{identity}/original.png",
        f"debate-images/{identity}/vision.webp",
    )
    return replace(
        prepared(expected),
        operation_id=f"input-image-{identity}",
        record_schema_version=2,
        plan_id="input-images",
        phase=DebatePhase.SCORING_AFFECTION,
        delivery_sequence=0,
        deadline_at=NOW + timedelta(minutes=15),
        attachments=(image,),
    )


def image_message(
    operation: OutboxOperation, *, missing_content_type: bool = False, size_delta: int = 0
) -> discord.Message:
    received = cast(Any, message(operation))
    image = operation.attachments[0]
    received.attachments = [
        SimpleNamespace(
            filename=f"{image.attachment_id}-{image.sha256[:16]}.png",
            size=image.size_bytes + size_delta,
            content_type=None if missing_content_type else image.content_type,
            width=image.width,
            height=image.height,
        )
    ]
    return cast(discord.Message, received)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_content_type", [False, True])
@pytest.mark.parametrize(
    "size_delta",
    [-1, 0, 1, 10 * 1024 * 1024],
    ids=["smaller", "same", "larger", "above-input-limit"],
)
async def test_image_upload_uses_validated_original_and_neutral_filename(
    missing_content_type: bool,
    size_delta: int,
) -> None:
    expected = snapshot()
    operation = image_operation(expected)

    class Store:
        async def load_original(self, image: object) -> bytes:
            return b"original image fixture"

    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(
        operation, repository, image_store=cast(DebateImageStore, Store())
    )
    cast(Any, thread).guild = SimpleNamespace(id=int(GUILD_ID), me=object())
    cast(Any, thread).permissions_for.return_value = SimpleNamespace(attach_files=True)
    transmitted: list[bytes] = []

    async def send(*args: object, **kwargs: Any) -> discord.Message:
        files = kwargs["files"]
        assert len(files) == 1
        assert files[0].filename.endswith(".png")
        transmitted.append(files[0].fp.read())
        return image_message(
            operation, missing_content_type=missing_content_type, size_delta=size_delta
        )

    send_mock.side_effect = send
    sent = await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)
    assert sent is not None and sent.status is OutboxStatus.SENT
    assert transmitted == [b"original image fixture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_content_type", [False, True])
@pytest.mark.parametrize(
    "size_delta",
    [-1, 0, 1, 10 * 1024 * 1024],
    ids=["smaller", "same", "larger", "above-input-limit"],
)
async def test_image_accepted_post_recovers_without_uploading_again(
    missing_content_type: bool,
    size_delta: int,
) -> None:
    expected = snapshot()
    operation = replace(
        image_operation(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="prior",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(
            image_message(
                operation, missing_content_type=missing_content_type, size_delta=size_delta
            ),
        ),
    )
    sent = await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)
    assert sent is not None and sent.status is OutboxStatus.SENT
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_same_nonce_with_changed_attachment_fails_closed() -> None:
    expected = snapshot()
    operation = replace(
        image_operation(expected),
        status=OutboxStatus.CLAIMED,
        claim_owner="prior",
        claim_expires_at=NOW - timedelta(seconds=1),
        delivery_attempt=1,
    )
    received = image_message(operation)
    cast(Any, received).attachments[0].filename = "different.png"
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(operation, repository, history_messages=(received,))
    with pytest.raises(DiscordDeliveryConflict):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "size_delta",
    [-1, 0, 1, 10 * 1024 * 1024],
    ids=["smaller", "same", "larger", "above-input-limit"],
)
async def test_terminating_image_reconciliation_accepts_provider_metadata_without_posting(
    size_delta: int,
) -> None:
    expected = snapshot()
    operation = replace(image_operation(expected), delivery_attempt=1)
    repository = FakeOutboxRepository(operation)
    subject, _, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(
            image_message(operation, missing_content_type=True, size_delta=size_delta),
        ),
    )

    sent = await subject.reconcile_persisted(expected=expected, operation_id=operation.operation_id)

    assert sent is not None and sent.status is OutboxStatus.SENT
    assert repository.claims == 0
    assert repository.marks == [str(MESSAGE_ID)]
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["send-response", "history", "termination"])
@pytest.mark.parametrize(
    ("changed_field", "changed_value", "reason"),
    [
        ("count", 0, "attachment_count_mismatch"),
        ("filename", "private-filename-sentinel.png", "attachment_filename_mismatch"),
        ("size", 0, "attachment_size_invalid"),
        ("size", -1, "attachment_size_invalid"),
        ("content_type", "image/jpeg", "attachment_content_type_mismatch"),
        ("content_type", "application/octet-stream", "attachment_content_type_mismatch"),
        ("content_type", "", "attachment_content_type_mismatch"),
    ],
)
async def test_image_response_conflicts_identify_fixed_check_without_exposing_payload(
    caplog: pytest.LogCaptureFixture,
    mode: str,
    changed_field: str,
    changed_value: str | int,
    reason: str,
) -> None:
    expected = snapshot()
    operation = image_operation(expected)
    if mode == "history":
        operation = replace(
            operation,
            status=OutboxStatus.CLAIMED,
            claim_owner="prior",
            claim_expires_at=NOW - timedelta(seconds=1),
            delivery_attempt=1,
        )
    elif mode == "termination":
        operation = replace(operation, delivery_attempt=1)
    received = image_message(operation, missing_content_type=True)
    if changed_field == "count":
        cast(Any, received).attachments = []
    else:
        setattr(cast(Any, received).attachments[0], changed_field, changed_value)
    load_original = AsyncMock(return_value=b"original image fixture")
    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(
        operation,
        repository,
        history_messages=(received,) if mode != "send-response" else (),
        image_store=cast(DebateImageStore, SimpleNamespace(load_original=load_original)),
    )
    cast(Any, thread).guild = SimpleNamespace(id=int(GUILD_ID), me=object())
    cast(Any, thread).permissions_for.return_value = SimpleNamespace(attach_files=True)
    send_mock.return_value = received

    with (
        caplog.at_level(logging.WARNING, logger="shittim_chest"),
        pytest.raises(DiscordDeliveryConflict),
    ):
        if mode == "termination":
            await subject.reconcile_persisted(
                expected=expected, operation_id=operation.operation_id
            )
        else:
            await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    assert repository.marks == []
    assert repository.retry_delays == []
    if mode != "send-response":
        send_mock.assert_not_awaited()
        load_original.assert_not_awaited()
    else:
        send_mock.assert_awaited_once()
        load_original.assert_awaited_once()
    event = json.loads(caplog.records[-1].message)
    assert event == {
        "severity": "WARNING",
        "event": "discord_delivery_conflict",
        "debate_id": str(operation.debate_id),
        "stage": "send_response" if mode == "send-response" else "history_reconciliation",
        "reason": reason,
        "action": "attachment",
        "phase": "scoring_affection",
        "delivery_sequence": 0,
        "delivery_attempt": 2 if mode == "history" else 1,
    }
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    image = operation.attachments[0]
    for private_value in (
        operation.content,
        operation.nonce,
        operation.content_hash,
        operation.operation_id,
        image.attachment_id,
        image.sha256,
        image.asset_key,
        image.vision_asset_key,
        "private-filename-sentinel.png",
        "image/jpeg",
        "application/octet-stream",
    ):
        assert private_value not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (b"private-short-source-sentinel", "source_size_mismatch"),
        (b"original image fixture"[::-1], "source_hash_mismatch"),
    ],
)
async def test_source_image_conflict_keeps_size_and_checksum_guards_before_send(
    caplog: pytest.LogCaptureFixture, source: bytes, reason: str
) -> None:
    expected = snapshot()
    operation = image_operation(expected)
    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(
        operation,
        repository,
        image_store=cast(
            DebateImageStore, SimpleNamespace(load_original=AsyncMock(return_value=source))
        ),
    )
    cast(Any, thread).guild = SimpleNamespace(id=int(GUILD_ID), me=object())
    cast(Any, thread).permissions_for.return_value = SimpleNamespace(attach_files=True)

    with (
        caplog.at_level(logging.WARNING, logger="shittim_chest"),
        pytest.raises(DiscordDeliveryConflict),
    ):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_not_awaited()
    assert repository.marks == []
    event = json.loads(caplog.records[-1].message)
    assert event["stage"] == "source_image"
    assert event["reason"] == reason
    assert source.decode() not in caplog.text
    assert operation.attachments[0].sha256 not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "reason"),
    [
        ("channel", "message_target_mismatch"),
        ("author", "message_author_mismatch"),
        ("nonce", "message_nonce_mismatch"),
        ("content", "message_body_mismatch"),
    ],
)
async def test_missing_attachment_mime_preserves_message_identity_and_body_guards(
    caplog: pytest.LogCaptureFixture, changed_field: str, reason: str
) -> None:
    expected = snapshot()
    operation = image_operation(expected)
    received = image_message(operation, missing_content_type=True)
    conflicting_id = int(THREAD_ID) * 10**15
    if changed_field in {"channel", "author"}:
        setattr(cast(Any, received), changed_field, SimpleNamespace(id=conflicting_id))
    else:
        setattr(cast(Any, received), changed_field, "private-returned-payload-sentinel")
    repository = FakeOutboxRepository(operation)
    subject, thread, send_mock, _ = publisher(
        operation,
        repository,
        image_store=cast(
            DebateImageStore,
            SimpleNamespace(load_original=AsyncMock(return_value=b"original image fixture")),
        ),
    )
    cast(Any, thread).guild = SimpleNamespace(id=int(GUILD_ID), me=object())
    cast(Any, thread).permissions_for.return_value = SimpleNamespace(attach_files=True)
    send_mock.return_value = received

    with (
        caplog.at_level(logging.WARNING, logger="shittim_chest"),
        pytest.raises(DiscordDeliveryConflict),
    ):
        await subject.publish_persisted(expected=expected, operation_id=operation.operation_id)

    send_mock.assert_awaited_once()
    assert repository.marks == []
    event = json.loads(caplog.records[-1].message)
    assert event["stage"] == "send_response"
    assert event["reason"] == reason
    assert "private-returned-payload-sentinel" not in caplog.text
    assert str(conflicting_id) not in caplog.text
