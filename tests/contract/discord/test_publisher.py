"""Offline discord.py contracts for fenced outbox publication and reconciliation."""

from __future__ import annotations

import asyncio
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
    LeaseGrant,
    OutboxOperation,
    OutboxStatus,
    content_sha256,
    nonce_from_uuid7,
)
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import AttemptId, DebateId, DebateState

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
    claims: int = 0
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
        operation_id: str,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> OutboxOperation:
        del expected, operation_id
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RepositoryConflict("injected completion failure")
        operation = self.operation
        if operation is None or operation.claim_owner != claim_owner:
            raise RepositoryConflict("claim owner mismatch")
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

    async def reschedule(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
        claim_owner: str,
        at: datetime,
        next_retry_at: datetime,
    ) -> OutboxOperation:
        del expected, operation_id
        operation = self.operation
        if operation is None or operation.claim_owner != claim_owner:
            raise RepositoryConflict("claim owner mismatch")
        self.retry_delays.append(next_retry_at - at)
        self.operation = replace(
            operation,
            status=OutboxStatus.PREPARED,
            claim_owner=None,
            claim_expires_at=None,
            next_retry_at=next_retry_at,
        )
        return self.operation

    async def list_recoverable(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        at: datetime,
    ) -> tuple[OutboxOperation, ...]:
        operation = await self.get(
            debate_id=debate_id,
            attempt_id=attempt_id,
            operation_id=self.operation.operation_id if self.operation else "missing",
        )
        if operation is None or operation.status is OutboxStatus.SENT:
            return ()
        if operation.next_retry_at is not None and operation.next_retry_at > at:
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
            channel=SimpleNamespace(id=int(operation.thread_id)),
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
        cast(Any, client_mock).http = SimpleNamespace(max_ratelimit_timeout=30.0)
        client_mock.is_ready.return_value = True
        client_mock.get_channel.return_value = thread if slot is operation.bot_slot else None
        client_mock.fetch_channel = AsyncMock(return_value=thread)
        clients[slot] = cast(discord.Client, client_mock)
    return clients, thread, send_mock


def publisher(
    operation: OutboxOperation,
    repository: FakeOutboxRepository,
    *,
    history_messages: tuple[discord.Message, ...] = (),
    clock: FakeClock | None = None,
    delivery_timeout_seconds: float = 45.0,
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
            delivery_timeout_seconds=delivery_timeout_seconds,
        ),
        thread,
        send_mock,
        test_clock,
    )


def test_discord_py_payload_enforces_nonce_and_disables_every_mention() -> None:
    with handle_message_parameters(
        content="hello <@123> @everyone",
        nonce="nonce",
        allowed_mentions=discord.AllowedMentions.none(),
    ) as parameters:
        assert parameters.payload is not None
        assert parameters.payload["nonce"] == "nonce"
        assert parameters.payload["enforce_nonce"] is True
        assert parameters.payload["allowed_mentions"] == {"parse": []}


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
async def test_reconciliation_fails_closed_on_same_nonce_with_different_content() -> None:
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
