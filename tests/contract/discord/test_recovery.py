"""Offline contracts for persisted Discord outbox recovery ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.adapters.discord import (
    DiscordOutboxRecovery,
    DiscordThreadLocked,
    DiscordUnavailable,
)
from shittim_chest.application import (
    DebateSnapshot,
    DiscordBotSlot,
    LeaseGrant,
    OutboxOperation,
    OutboxRecoveryAbandoned,
    OutboxRecoveryFailed,
    OutboxStatus,
    content_sha256,
)
from shittim_chest.application.models import DeliveryAbandonReason, MetricEvent
from shittim_chest.application.ports import DiscordOutboxRepository
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        return self.current


@dataclass(slots=True)
class FakeMetrics:
    events: list[MetricEvent] = field(default_factory=list)

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None:
        del debate_id
        self.events.append(event)


@dataclass(slots=True)
class FakeOutbox:
    operations: list[OutboxOperation]

    async def list_pending(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> tuple[OutboxOperation, ...]:
        return tuple(
            sorted(
                (
                    operation
                    for operation in self.operations
                    if operation.debate_id == debate_id
                    and operation.attempt_id == attempt_id
                    and operation.status not in {OutboxStatus.SENT, OutboxStatus.ABANDONED}
                ),
                key=lambda operation: (
                    operation.chunk_sequence
                    if operation.delivery_sequence is None
                    else operation.delivery_sequence,
                    operation.operation_id,
                ),
            )
        )

    def replace(self, updated: OutboxOperation) -> None:
        self.operations = [
            updated if operation.operation_id == updated.operation_id else operation
            for operation in self.operations
        ]


class FakePublisher:
    def __init__(
        self,
        *,
        outbox: FakeOutbox,
        clock: FakeClock,
        failure: Exception | None = None,
        return_none_once: bool = False,
        block: bool = False,
    ) -> None:
        self._outbox = outbox
        self._clock = clock
        self._failure = failure
        self._return_none_once = return_none_once
        self._block = block
        self.calls: list[str] = []
        self.publish_calls: list[str] = []
        self.reconcile_calls: list[str] = []

    async def publish_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None:
        del expected
        self.calls.append(operation_id)
        self.publish_calls.append(operation_id)
        operation = next(
            item for item in self._outbox.operations if item.operation_id == operation_id
        )
        if self._block:
            await asyncio.Event().wait()
        if self._return_none_once:
            self._return_none_once = False
            return None
        if self._failure is not None:
            failure = self._failure
            self._failure = None
            if isinstance(failure, DiscordUnavailable):
                self._outbox.replace(
                    replace(
                        operation,
                        status=OutboxStatus.PREPARED,
                        claim_owner=None,
                        claim_expires_at=None,
                        delivery_attempt=operation.delivery_attempt + 1,
                        next_retry_at=self._clock.current + timedelta(seconds=30),
                    )
                )
            raise failure
        sent = replace(
            operation,
            status=OutboxStatus.SENT,
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=operation.delivery_attempt + 1,
            next_retry_at=None,
            message_id=str(900 + operation.chunk_sequence),
            sent_at=self._clock.current,
        )
        self._outbox.replace(sent)
        return sent

    async def reconcile_persisted(
        self,
        *,
        expected: DebateSnapshot,
        operation_id: str,
    ) -> OutboxOperation | None:
        del expected
        self.calls.append(operation_id)
        self.reconcile_calls.append(operation_id)
        operation = next(
            item for item in self._outbox.operations if item.operation_id == operation_id
        )
        if self._failure is not None:
            failure = self._failure
            self._failure = None
            raise failure
        if operation.delivery_attempt == 0:
            return None
        sent = replace(
            operation,
            status=OutboxStatus.SENT,
            claim_owner=None,
            claim_expires_at=None,
            next_retry_at=None,
            message_id=str(900 + operation.chunk_sequence),
            sent_at=self._clock.current,
        )
        self._outbox.replace(sent)
        return sent


def leased_snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="100",
        channel_id="101",
        created_at=NOW,
        attempt_created_at=NOW,
        thread_id="102",
        lease=LeaseGrant("worker", 0, 1, NOW + timedelta(minutes=10)),
    )


def operation(
    expected: DebateSnapshot,
    sequence: int,
    *,
    next_retry_at: datetime | None = None,
    claimed_until: datetime | None = None,
    v2: bool = False,
    delivery_attempt: int | None = None,
    created_at: datetime = NOW,
) -> OutboxOperation:
    content = f"chunk-{sequence}"
    claimed = claimed_until is not None
    return OutboxOperation(
        operation_id=f"post-{sequence:04d}",
        debate_id=expected.state.debate_id,
        attempt_id=expected.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="102",
        content=content,
        content_hash=content_sha256(content),
        nonce=chr(ord("A") + sequence) * 22,
        chunk_sequence=sequence,
        status=OutboxStatus.CLAIMED if claimed else OutboxStatus.PREPARED,
        created_at=created_at,
        claim_owner="old-worker" if claimed else None,
        claim_expires_at=claimed_until,
        delivery_attempt=(
            delivery_attempt
            if delivery_attempt is not None
            else 1
            if claimed or next_retry_at is not None
            else 0
        ),
        next_retry_at=next_retry_at,
        record_schema_version=2 if v2 else 1,
        phase=DebatePhase.COMPLETED if v2 else None,
        plan_id="terminal-completed" if v2 else None,
        delivery_sequence=300 + sequence if v2 else None,
        deadline_at=created_at + timedelta(minutes=15) if v2 else None,
    )


def recovery(
    expected: DebateSnapshot,
    operations: list[OutboxOperation],
    *,
    failure: Exception | None = None,
    return_none_once: bool = False,
    block: bool = False,
) -> tuple[DiscordOutboxRecovery, FakeClock, FakeMetrics, FakePublisher, list[float]]:
    del expected
    clock = FakeClock()
    metrics = FakeMetrics()
    outbox = FakeOutbox(operations)
    publisher = FakePublisher(
        outbox=outbox,
        clock=clock,
        failure=failure,
        return_none_once=return_none_once,
        block=block,
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock.current += timedelta(seconds=delay)
        await asyncio.sleep(0)

    return (
        DiscordOutboxRecovery(
            outbox=cast(DiscordOutboxRepository, outbox),
            publisher=publisher,
            clock=clock,
            metrics=metrics,
            sleep=sleep,
        ),
        clock,
        metrics,
        publisher,
        sleeps,
    )


@pytest.mark.asyncio
async def test_recovery_waits_for_persisted_retry_and_drains_chunks_in_order() -> None:
    expected = leased_snapshot()
    operations = [
        operation(expected, 0, next_retry_at=NOW + timedelta(seconds=5)),
        operation(expected, 1),
    ]
    subject, _, metrics, publisher, sleeps = recovery(expected, operations)

    await subject.drain(expected=expected)

    assert publisher.calls == ["post-0000", "post-0001"]
    assert sleeps == [5.0]
    assert metrics.events == [MetricEvent.OUTBOX_RECOVERED, MetricEvent.OUTBOX_RECOVERED]


@pytest.mark.asyncio
async def test_retryable_failure_uses_the_new_persisted_schedule() -> None:
    expected = leased_snapshot()
    subject, _, metrics, publisher, sleeps = recovery(
        expected,
        [operation(expected, 0)],
        failure=DiscordUnavailable(),
    )

    await subject.drain(expected=expected)

    assert publisher.calls == ["post-0000", "post-0000"]
    assert sleeps == [30.0]
    assert metrics.events == [
        MetricEvent.OUTBOX_RETRY_SCHEDULED,
        MetricEvent.OUTBOX_RECOVERED,
    ]


@pytest.mark.asyncio
async def test_unexpired_claim_and_claim_race_wait_without_busy_looping() -> None:
    expected = leased_snapshot()
    subject, _, _, publisher, sleeps = recovery(
        expected,
        [operation(expected, 0, claimed_until=NOW + timedelta(seconds=10))],
        return_none_once=True,
    )

    await subject.drain(expected=expected)

    assert publisher.calls == ["post-0000", "post-0000"]
    assert sleeps == [pytest.approx(10.000001), 1.0]


@pytest.mark.asyncio
async def test_nonretryable_delivery_failure_preserves_the_stable_code() -> None:
    expected = leased_snapshot()
    subject, _, metrics, _, _ = recovery(
        expected,
        [operation(expected, 0)],
        failure=DiscordThreadLocked(),
    )

    with pytest.raises(OutboxRecoveryFailed) as captured:
        await subject.drain(expected=expected)

    assert captured.value.delivery_code == "DISCORD_THREAD_LOCKED"
    assert metrics.events == []


@pytest.mark.asyncio
async def test_recovery_cancellation_is_not_swallowed() -> None:
    expected = leased_snapshot()
    subject, _, _, _, _ = recovery(
        expected,
        [operation(expected, 0)],
        block=True,
    )
    task = asyncio.create_task(subject.drain(expected=expected))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_recovery_rejects_non_positive_poll_interval() -> None:
    outbox = FakeOutbox([])
    clock = FakeClock()

    with pytest.raises(ValueError, match="positive"):
        DiscordOutboxRecovery(
            outbox=cast(DiscordOutboxRepository, outbox),
            publisher=FakePublisher(outbox=outbox, clock=clock),
            clock=clock,
            metrics=FakeMetrics(),
            poll_seconds=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "created_at", "delivery_attempt"),
    (
        (
            DeliveryAbandonReason.ATTEMPTS_EXHAUSTED,
            NOW,
            3,
        ),
        (
            DeliveryAbandonReason.DEADLINE_EXCEEDED,
            NOW - timedelta(minutes=15),
            None,
        ),
    ),
)
async def test_v2_bound_waits_for_active_claim_before_stopping(
    reason: DeliveryAbandonReason,
    created_at: datetime,
    delivery_attempt: int | None,
) -> None:
    expected = leased_snapshot()
    subject, _, _, publisher, sleeps = recovery(
        expected,
        [
            operation(
                expected,
                0,
                v2=True,
                claimed_until=NOW + timedelta(seconds=10),
                created_at=created_at,
                delivery_attempt=delivery_attempt,
            )
        ],
    )

    with pytest.raises(OutboxRecoveryAbandoned) as captured:
        await subject.drain(expected=expected)

    assert captured.value.reason == reason.value
    assert sleeps == [pytest.approx(10.000001)]
    assert publisher.publish_calls == []


@pytest.mark.asyncio
async def test_v2_retry_schedule_is_capped_at_the_delivery_deadline() -> None:
    expected = leased_snapshot()
    subject, _, _, publisher, sleeps = recovery(
        expected,
        [
            operation(
                expected,
                0,
                v2=True,
                created_at=NOW - timedelta(minutes=15) + timedelta(seconds=5),
                next_retry_at=NOW + timedelta(seconds=30),
            )
        ],
    )

    with pytest.raises(OutboxRecoveryAbandoned) as captured:
        await subject.drain(expected=expected)

    assert captured.value.reason == DeliveryAbandonReason.DEADLINE_EXCEEDED.value
    assert sleeps == [5.0]
    assert publisher.publish_calls == []


@pytest.mark.asyncio
async def test_v2_nonretryable_failure_maps_to_typed_abandonment() -> None:
    expected = leased_snapshot()
    subject, _, metrics, _, _ = recovery(
        expected,
        [operation(expected, 0, v2=True)],
        failure=DiscordThreadLocked(),
    )

    with pytest.raises(OutboxRecoveryAbandoned) as captured:
        await subject.drain(expected=expected)

    assert captured.value.reason == DeliveryAbandonReason.NON_RETRYABLE.value
    assert captured.value.delivery_code == "DISCORD_THREAD_LOCKED"
    assert metrics.events == []


@pytest.mark.asyncio
async def test_terminating_reconciliation_waits_and_never_calls_publish() -> None:
    expected = leased_snapshot()
    subject, _, metrics, publisher, sleeps = recovery(
        expected,
        [
            operation(
                expected,
                0,
                v2=True,
                claimed_until=NOW + timedelta(seconds=10),
            )
        ],
    )

    assert await subject.terminate(expected=expected)
    assert sleeps == [pytest.approx(10.000001)]
    assert publisher.publish_calls == []
    assert publisher.reconcile_calls == ["post-0000"]
    assert metrics.events == [MetricEvent.OUTBOX_RECOVERED]


@pytest.mark.asyncio
async def test_terminating_reconciliation_reports_unattempted_remainder() -> None:
    expected = leased_snapshot()
    subject, _, metrics, publisher, sleeps = recovery(
        expected,
        [operation(expected, 0, v2=True)],
    )

    assert not await subject.terminate(expected=expected)
    assert sleeps == []
    assert publisher.publish_calls == []
    assert publisher.reconcile_calls == ["post-0000"]
    assert metrics.events == []


@pytest.mark.asyncio
async def test_terminating_reconciliation_reports_history_error_without_posting() -> None:
    expected = leased_snapshot()
    subject, _, metrics, publisher, _ = recovery(
        expected,
        [operation(expected, 0, v2=True, delivery_attempt=1)],
        failure=DiscordUnavailable(),
    )

    assert not await subject.terminate(expected=expected)
    assert publisher.publish_calls == []
    assert publisher.reconcile_calls == ["post-0000"]
    assert metrics.events == []


@pytest.mark.asyncio
async def test_recovery_uses_global_delivery_sequence_across_local_chunks() -> None:
    expected = leased_snapshot()
    later = operation(expected, 1, v2=True)
    earlier = replace(
        operation(expected, 0, v2=True),
        operation_id="post-earlier",
        chunk_sequence=1,
        delivery_sequence=300,
    )
    subject, _, _, publisher, _ = recovery(expected, [later, earlier])

    await subject.drain(expected=expected)

    assert publisher.publish_calls == ["post-earlier", "post-0001"]
