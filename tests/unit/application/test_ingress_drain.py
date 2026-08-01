"""Tests for the post-recovery durable ingress drainer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.application.commands import AppliedIngressCommand
from shittim_chest.application.errors import InvalidApplicationOperation, RuntimeNotReady
from shittim_chest.application.ingress_drain import (
    IngressDrainer,
    IngressDrainReport,
    IngressDrainStop,
    IngressFailureDisposition,
    IngressRejectedFailure,
    IngressRetryableFailure,
    IngressTerminalFailure,
    RuntimeIngressDrainGate,
    classify_ingress_failure,
)
from shittim_chest.application.ports import (
    Clock,
    IngressRepository,
    RepositoryBusy,
    RepositoryConflict,
    RepositoryQuotaExceeded,
    RepositoryUnavailable,
    RuntimeStateRepository,
)
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressRequest,
    IngressStatus,
    RuntimeState,
    RuntimeStatus,
)
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
RUNTIME_ID = "runtime-instance"


@dataclass(slots=True)
class FakeClock:
    current: datetime = NOW

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


@dataclass(slots=True)
class FakeAdmission:
    is_accepting: bool = True
    identities_ready: bool = True
    checks: int = 0

    async def all_identities_ready(self) -> bool:
        self.checks += 1
        return self.identities_ready


@dataclass(slots=True)
class FakeRuntimeStateRepository:
    state: RuntimeState | None = field(default_factory=lambda: ready_runtime())
    error: Exception | None = None
    reads: int = 0

    async def get(self) -> RuntimeState | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.state


@dataclass(slots=True)
class FakeIngressRepository:
    ready: tuple[IngressRequest, ...] = ()
    claim_losses: set[str] = field(default_factory=set)
    events: list[tuple[str, str]] = field(default_factory=list)
    accepted: list[tuple[str, DebateId, AttemptId]] = field(default_factory=list)
    rescheduled: list[tuple[str, datetime, str]] = field(default_factory=list)
    terminal: list[tuple[str, IngressStatus, str | None]] = field(default_factory=list)
    list_calls: int = 0
    list_error: Exception | None = None
    claim_error: Exception | None = None
    accept_error: Exception | None = None
    settlement_error: Exception | None = None
    timeline: list[str] | None = None

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        del at
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return self.ready

    async def claim(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        self.events.append(("claim", request.interaction_id))
        if self.claim_error is not None:
            raise self.claim_error
        if request.interaction_id in self.claim_losses:
            return None
        return replace(
            request,
            status=IngressStatus.CLAIMED,
            updated_at=at,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(minutes=2),
            next_attempt_at=None,
            delivery_attempt=request.delivery_attempt + 1,
        )

    async def reschedule(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        assert request.claim_owner == claim_owner
        self.events.append(("reschedule", request.interaction_id))
        if self.settlement_error is not None:
            raise self.settlement_error
        self.rescheduled.append((request.interaction_id, next_attempt_at, error_code))
        return replace(
            request,
            status=IngressStatus.RETRYING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
        )

    async def mark_accepted(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest:
        assert request.claim_owner == claim_owner
        self.events.append(("accept", request.interaction_id))
        if self.timeline is not None:
            self.timeline.append(f"mark_accepted:{request.interaction_id}")
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted.append((request.interaction_id, debate_id, attempt_id))
        return replace(
            request,
            status=IngressStatus.ACCEPTED,
            updated_at=at,
            claim_owner=None,
            claim_expires_at=None,
            accepted_debate_id=debate_id,
            accepted_attempt_id=attempt_id,
        )

    async def mark_claim_terminal(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        assert request.claim_owner == claim_owner
        self.events.append(("terminal", request.interaction_id))
        if self.settlement_error is not None:
            raise self.settlement_error
        self.terminal.append((request.interaction_id, status, error_code))
        return replace(
            request,
            status=status,
            updated_at=at,
            claim_owner=None,
            claim_expires_at=None,
            completed_at=at,
            error_code=error_code,
        )


@dataclass(slots=True)
class FakeCommands:
    errors: dict[str, BaseException] = field(default_factory=dict)
    results: dict[str, AppliedIngressCommand] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    aborted: list[tuple[str, str]] = field(default_factory=list)
    abort_error: Exception | None = None
    timeline: list[str] | None = None

    async def apply(
        self,
        request: IngressRequest,
        *,
        claim_owner: str,
        at: datetime,
    ) -> AppliedIngressCommand:
        assert request.claim_owner == claim_owner
        assert request.claim_expires_at is not None and request.claim_expires_at > at
        self.calls.append(request.interaction_id)
        if self.timeline is not None:
            self.timeline.append(f"apply:{request.interaction_id}")
        error = self.errors.get(request.interaction_id)
        if error is not None:
            raise error
        return self.results.setdefault(
            request.interaction_id,
            AppliedIngressCommand(request.kind, DebateId.new(), AttemptId.new()),
        )

    async def abort_pre_activation(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
        *,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> str:
        del applied
        assert request.claim_owner == claim_owner
        assert request.claim_expires_at is not None and request.claim_expires_at > at
        self.aborted.append((request.interaction_id, error_code))
        if self.timeline is not None:
            self.timeline.append(f"abort:{request.interaction_id}")
        if self.abort_error is not None:
            raise self.abort_error
        return error_code


@dataclass(slots=True)
class FakeContext:
    preflight_errors: dict[str, Exception] = field(default_factory=dict)
    prepare_errors: dict[str, Exception] = field(default_factory=dict)
    activation_error: Exception | None = None
    preflighted: list[str] = field(default_factory=list)
    prepared: list[str] = field(default_factory=list)
    activated: list[str] = field(default_factory=list)
    timeline: list[str] | None = None

    async def preflight(self, request: IngressRequest) -> None:
        self.preflighted.append(request.interaction_id)
        if self.timeline is not None:
            self.timeline.append(f"preflight:{request.interaction_id}")
        error = self.preflight_errors.get(request.interaction_id)
        if error is not None:
            raise error

    async def prepare(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        del applied
        self.prepared.append(request.interaction_id)
        if self.timeline is not None:
            self.timeline.append(f"prepare:{request.interaction_id}")
        error = self.prepare_errors.get(request.interaction_id)
        if error is not None:
            raise error

    async def activate(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        del applied
        assert request.status is IngressStatus.ACCEPTED
        self.activated.append(request.interaction_id)
        if self.timeline is not None:
            self.timeline.append(f"activate:{request.interaction_id}")
        if self.activation_error is not None:
            raise self.activation_error


@dataclass(slots=True)
class FakeRuntimeSession:
    calls: int = 0
    timeline: list[str] | None = None

    async def mark_busy(self) -> object:
        self.calls += 1
        if self.timeline is not None:
            self.timeline.append("mark_busy")
        return object()


def request(interaction_id: str, *, offset: int = 0) -> IngressRequest:
    created_at = NOW - timedelta(seconds=offset)
    pending = IngressRequest.new_debate(
        interaction_id=interaction_id,
        operation_id=f"operation-{interaction_id}",
        application_id="application-id",
        question=f"question {interaction_id}",
        requester_id="requester-id",
        requester_username="display-only-username",
        requester_display_name="display-only-name",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=created_at,
    )
    return replace(
        pending,
        status_message_id=f"12345{offset}",
        status_message_updated_at=created_at,
    )


def control_request(interaction_id: str, kind: IngressKind) -> IngressRequest:
    if kind not in {IngressKind.RETRY, IngressKind.CANCEL}:
        raise ValueError("test control request must be retry or cancel")
    return IngressRequest.control_operation(
        interaction_id=interaction_id,
        operation_id=f"operation-{interaction_id}",
        kind=kind,
        application_id="application-id",
        requester_id="requester-id",
        requester_username="display-only-username",
        requester_display_name="display-only-name",
        requester_can_manage_messages=False,
        guild_id="guild-id",
        channel_id="thread-id",
        parent_channel_id="channel-id",
        source_message_id="panel-id",
        source_thread_id="thread-id",
        target_debate_id=DebateId.new(),
        expected_attempt_id=AttemptId.new(),
        custom_id=f"shittim:v1:{kind.value}:operation",
        created_at=NOW,
    )


def ready_runtime(
    *,
    runtime_id: str = RUNTIME_ID,
    status: RuntimeStatus = RuntimeStatus.READY,
) -> RuntimeState:
    starting = RuntimeState.stopped(at=NOW - timedelta(minutes=1)).request_wake(
        at=NOW - timedelta(seconds=50)
    )
    started = starting.mark_started(
        at=NOW - timedelta(seconds=40),
        runtime_instance_id=runtime_id,
    )
    ready = started.transition(
        RuntimeStatus.READY,
        at=NOW - timedelta(seconds=30),
        runtime_instance_id=runtime_id,
    )
    if status is RuntimeStatus.BUSY:
        return ready.transition(RuntimeStatus.BUSY, at=NOW - timedelta(seconds=20))
    if status is RuntimeStatus.READY:
        return ready
    if status is RuntimeStatus.IDLE:
        return ready.begin_idle(at=NOW - timedelta(seconds=20))
    raise AssertionError("test helper supports READY, BUSY, or IDLE")


def open_gate(admission: FakeAdmission | None = None) -> RuntimeIngressDrainGate:
    gate = RuntimeIngressDrainGate(admission or FakeAdmission())
    gate.mark_supervisor_started()
    gate.mark_local_command_schema_checked()
    gate.mark_recovery_complete()
    return gate


def drainer(
    *,
    ingress: FakeIngressRepository,
    commands: FakeCommands | None = None,
    runtime: FakeRuntimeStateRepository | None = None,
    gate: RuntimeIngressDrainGate | None = None,
    clock: FakeClock | None = None,
    context: FakeContext | None = None,
    runtime_session: FakeRuntimeSession | None = None,
) -> IngressDrainer:
    return IngressDrainer(
        clock=cast(Clock, clock or FakeClock()),
        ingress=cast(IngressRepository, ingress),
        runtime_state=cast(RuntimeStateRepository, runtime or FakeRuntimeStateRepository()),
        commands=commands or FakeCommands(),
        context=context or FakeContext(),
        gate=gate or open_gate(),
        runtime_instance_id=RUNTIME_ID,
        runtime_session=runtime_session,
    )


@pytest.mark.asyncio
async def test_drain_remains_closed_until_supervisor_schema_recovery_and_admission() -> None:
    ingress = FakeIngressRepository(ready=(request("first"),))
    admission = FakeAdmission()
    gate = RuntimeIngressDrainGate(admission)
    worker = drainer(ingress=ingress, gate=gate)

    assert (await worker.drain_once()).stop is IngressDrainStop.GATE_CLOSED
    gate.mark_supervisor_started()
    assert (await worker.drain_once()).stop is IngressDrainStop.GATE_CLOSED
    gate.mark_local_command_schema_checked()
    assert (await worker.drain_once()).stop is IngressDrainStop.GATE_CLOSED
    gate.mark_recovery_complete()
    admission.is_accepting = False
    assert (await worker.drain_once()).stop is IngressDrainStop.GATE_CLOSED
    admission.is_accepting = True
    admission.identities_ready = False
    assert (await worker.drain_once()).stop is IngressDrainStop.GATE_CLOSED

    assert ingress.list_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RuntimeStatus.READY, RuntimeStatus.BUSY])
async def test_drain_preserves_repository_fifo_and_persists_accepted_ids(
    status: RuntimeStatus,
) -> None:
    requests = (request("oldest", offset=3), request("middle", offset=2), request("newest"))
    ingress = FakeIngressRepository(ready=requests)
    commands = FakeCommands()
    runtime = FakeRuntimeStateRepository(ready_runtime(status=status))

    report = await drainer(ingress=ingress, commands=commands, runtime=runtime).drain_once()

    assert report.stop is IngressDrainStop.QUEUE_DRAINED
    assert (report.claimed, report.accepted) == (3, 3)
    assert commands.calls == [item.interaction_id for item in requests]
    assert [item[0] for item in ingress.accepted] == commands.calls
    assert [item[1:] for item in ingress.accepted] == [
        (
            commands.results[item.interaction_id].debate_id,
            commands.results[item.interaction_id].attempt_id,
        )
        for item in requests
    ]


@pytest.mark.asyncio
async def test_context_and_task_order_is_apply_prepare_accept_busy_activate() -> None:
    timeline: list[str] = []
    item = request("first")
    ingress = FakeIngressRepository(ready=(item,), timeline=timeline)
    commands = FakeCommands(timeline=timeline)
    context = FakeContext(timeline=timeline)
    session = FakeRuntimeSession(timeline=timeline)

    report = await drainer(
        ingress=ingress,
        commands=commands,
        context=context,
        runtime_session=session,
    ).drain_once()

    assert report.accepted == 1
    assert timeline == [
        "apply:first",
        "prepare:first",
        "mark_accepted:first",
        "mark_busy",
        "activate:first",
    ]
    assert context.preflighted == []
    assert session.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [IngressKind.RETRY, IngressKind.CANCEL])
async def test_control_context_is_preflighted_before_command_mutation(
    kind: IngressKind,
) -> None:
    timeline: list[str] = []
    item = control_request(kind.value, kind)
    ingress = FakeIngressRepository(ready=(item,), timeline=timeline)
    commands = FakeCommands(timeline=timeline)
    context = FakeContext(timeline=timeline)

    report = await drainer(
        ingress=ingress,
        commands=commands,
        context=context,
    ).drain_once()

    assert report.accepted == 1
    assert timeline == [
        f"preflight:{kind.value}",
        f"apply:{kind.value}",
        f"prepare:{kind.value}",
        f"mark_accepted:{kind.value}",
        f"activate:{kind.value}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime",
    [
        FakeRuntimeStateRepository(None),
        FakeRuntimeStateRepository(ready_runtime(runtime_id="replacement-runtime")),
        FakeRuntimeStateRepository(ready_runtime(status=RuntimeStatus.IDLE)),
    ],
)
async def test_drain_rejects_absent_foreign_or_nonready_runtime_generation(
    runtime: FakeRuntimeStateRepository,
) -> None:
    ingress = FakeIngressRepository(ready=(request("first"),))

    report = await drainer(ingress=ingress, runtime=runtime).drain_once()

    assert report.stop is IngressDrainStop.RUNTIME_NOT_READY
    assert ingress.list_calls == 0


@pytest.mark.asyncio
async def test_slot_shortage_reschedules_head_and_never_overtakes_it() -> None:
    first = request("first", offset=1)
    second = request("second")
    ingress = FakeIngressRepository(ready=(first, second))
    commands = FakeCommands(errors={first.interaction_id: RepositoryBusy("busy")})

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is IngressDrainStop.SLOT_BUSY
    assert (report.claimed, report.rescheduled) == (1, 1)
    assert commands.calls == ["first"]
    assert ingress.events == [("claim", "first"), ("reschedule", "first")]
    assert ingress.rescheduled[0][2] == "execution_slots_busy"
    assert ingress.rescheduled[0][1] > NOW


@pytest.mark.asyncio
async def test_missing_starter_status_reschedules_before_slot_or_context_work() -> None:
    first = replace(
        request("first", offset=1),
        status_message_id=None,
        status_message_updated_at=None,
    )
    ingress = FakeIngressRepository(ready=(first, request("second")))
    commands = FakeCommands()
    context = FakeContext()

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.stop is IngressDrainStop.RETRY_SCHEDULED
    assert (report.claimed, report.rescheduled) == (1, 1)
    assert ingress.rescheduled[0][2] == "status_message_pending"
    assert commands.calls == []
    assert context.prepared == []
    assert context.activated == []
    assert ingress.events == [("claim", "first"), ("reschedule", "first")]


@pytest.mark.asyncio
async def test_claim_loss_stops_without_settlement_or_overtaking() -> None:
    ingress = FakeIngressRepository(
        ready=(request("first", offset=1), request("second")),
        claim_losses={"first"},
    )
    commands = FakeCommands()

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is IngressDrainStop.CLAIM_LOST
    assert report.claimed == 0
    assert commands.calls == []
    assert ingress.events == [("claim", "first")]


@pytest.mark.asyncio
async def test_quota_rejection_is_terminal_then_fifo_continues() -> None:
    first = request("first", offset=1)
    second = request("second")
    ingress = FakeIngressRepository(ready=(first, second))
    commands = FakeCommands(errors={"first": RepositoryQuotaExceeded("quota")})

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is IngressDrainStop.QUEUE_DRAINED
    assert (report.claimed, report.rejected, report.accepted) == (2, 1, 1)
    assert ingress.terminal == [("first", IngressStatus.REJECTED, "daily_quota_exceeded")]
    assert commands.calls == ["first", "second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code", "expected_stop"),
    [
        (
            RuntimeNotReady("not ready"),
            IngressStatus.RETRYING,
            "runtime_not_ready",
            IngressDrainStop.RETRY_SCHEDULED,
        ),
        (
            IngressRetryableFailure("discord_temporarily_unavailable"),
            IngressStatus.RETRYING,
            "discord_temporarily_unavailable",
            IngressDrainStop.RETRY_SCHEDULED,
        ),
        (
            InvalidApplicationOperation("invalid"),
            IngressStatus.REJECTED,
            "invalid_application_operation",
            IngressDrainStop.QUEUE_DRAINED,
        ),
        (
            IngressRejectedFailure("discord_context_missing"),
            IngressStatus.REJECTED,
            "discord_context_missing",
            IngressDrainStop.QUEUE_DRAINED,
        ),
        (
            IngressTerminalFailure("discord_context_corrupt"),
            IngressStatus.FAILED,
            "discord_context_corrupt",
            IngressDrainStop.QUEUE_DRAINED,
        ),
    ],
)
async def test_failure_classification_selects_durable_settlement(
    error: Exception,
    expected_status: IngressStatus,
    expected_code: str,
    expected_stop: IngressDrainStop,
) -> None:
    item = request("first")
    ingress = FakeIngressRepository(ready=(item,))
    commands = FakeCommands(errors={item.interaction_id: error})

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is expected_stop
    if expected_status is IngressStatus.RETRYING:
        assert ingress.rescheduled[0][2] == expected_code
        assert ingress.terminal == []
    else:
        assert ingress.terminal == [("first", expected_status, expected_code)]
        assert ingress.rescheduled == []


def test_repository_conflict_is_classified_as_unsettled_claim_loss() -> None:
    disposition = classify_ingress_failure(RepositoryConflict("stale claim"))

    assert disposition.status is None
    assert disposition.error_code == "claim_lost"


@pytest.mark.asyncio
async def test_cancellation_leaves_claim_for_expiry_and_propagates() -> None:
    item = request("first")
    ingress = FakeIngressRepository(ready=(item,))
    commands = FakeCommands(errors={item.interaction_id: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await drainer(ingress=ingress, commands=commands).drain_once()

    assert ingress.accepted == []
    assert ingress.rescheduled == []
    assert ingress.terminal == []


@pytest.mark.asyncio
async def test_shutdown_gate_cannot_be_reopened_by_late_recovery_completion() -> None:
    ingress = FakeIngressRepository(ready=(request("first"),))
    gate = open_gate()
    gate.begin_shutdown()
    gate.mark_recovery_complete()

    report = await drainer(ingress=ingress, gate=gate).drain_once()

    assert report.stop is IngressDrainStop.GATE_CLOSED
    assert ingress.list_calls == 0


@pytest.mark.asyncio
async def test_reconnect_recovery_closes_an_open_gate_until_completed_again() -> None:
    ingress = FakeIngressRepository()
    gate = open_gate()
    assert gate.recovery_complete
    gate.begin_recovery()

    assert not gate.recovery_complete
    assert (await drainer(ingress=ingress, gate=gate).drain_once()).stop is (
        IngressDrainStop.GATE_CLOSED
    )

    gate.mark_recovery_complete()
    assert (await drainer(ingress=ingress, gate=gate).drain_once()).stop is (
        IngressDrainStop.QUEUE_EMPTY
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"runtime_instance_id": " "}, "runtime instance ID"),
        ({"retry_delay": timedelta(0)}, "retry delay"),
        ({"poll_seconds": 0.0}, "poll interval"),
    ],
)
def test_drainer_rejects_invalid_process_configuration(
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        IngressDrainer(
            clock=cast(Clock, FakeClock()),
            ingress=cast(IngressRepository, FakeIngressRepository()),
            runtime_state=cast(RuntimeStateRepository, FakeRuntimeStateRepository()),
            commands=FakeCommands(),
            context=FakeContext(),
            gate=open_gate(),
            runtime_instance_id=cast(str, arguments.get("runtime_instance_id", RUNTIME_ID)),
            retry_delay=cast(
                timedelta,
                arguments.get("retry_delay", timedelta(seconds=5)),
            ),
            poll_seconds=cast(float, arguments.get("poll_seconds", 1.0)),
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"claimed": -1},
        {"claimed": 1, "accepted": 2},
    ],
)
def test_report_rejects_negative_or_over_settled_counters(arguments: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        IngressDrainReport(IngressDrainStop.QUEUE_EMPTY, **arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RepositoryConflict("conflict"), RepositoryUnavailable()])
async def test_runtime_state_read_failure_is_fail_closed(error: Exception) -> None:
    ingress = FakeIngressRepository(ready=(request("first"),))
    runtime = FakeRuntimeStateRepository(error=error)

    report = await drainer(ingress=ingress, runtime=runtime).drain_once()

    assert report.stop is IngressDrainStop.RUNTIME_STATE_UNAVAILABLE
    assert ingress.list_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RepositoryConflict("conflict"), RepositoryUnavailable()])
async def test_fifo_query_failure_claims_nothing(error: Exception) -> None:
    ingress = FakeIngressRepository(list_error=error)

    report = await drainer(ingress=ingress).drain_once()

    assert report.stop is IngressDrainStop.REPOSITORY_UNAVAILABLE
    assert ingress.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_stop"),
    [
        (RepositoryConflict("claim race"), IngressDrainStop.CLAIM_LOST),
        (RepositoryUnavailable(), IngressDrainStop.REPOSITORY_UNAVAILABLE),
    ],
)
async def test_claim_failure_stops_before_command(
    error: Exception,
    expected_stop: IngressDrainStop,
) -> None:
    ingress = FakeIngressRepository(ready=(request("first"),), claim_error=error)
    commands = FakeCommands()

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is expected_stop
    assert commands.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_stop"),
    [
        (RepositoryConflict("expired claim"), IngressDrainStop.CLAIM_LOST),
        (RepositoryUnavailable(), IngressDrainStop.REPOSITORY_UNAVAILABLE),
    ],
)
async def test_accept_settlement_failure_relies_on_replay_and_claim_expiry(
    error: Exception,
    expected_stop: IngressDrainStop,
) -> None:
    ingress = FakeIngressRepository(ready=(request("first"),), accept_error=error)
    commands = FakeCommands()
    context = FakeContext()

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.stop is expected_stop
    assert report.claimed == 1
    assert report.accepted == 0
    assert commands.calls == ["first"]
    assert context.prepared == ["first"]
    assert context.activated == []


@pytest.mark.asyncio
async def test_prepare_failure_is_settled_before_acceptance_and_activation() -> None:
    item = request("first")
    ingress = FakeIngressRepository(ready=(item,))
    commands = FakeCommands()
    context = FakeContext(
        prepare_errors={"first": IngressRejectedFailure("discord_context_invalid")}
    )

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert (report.rejected, report.accepted) == (1, 0)
    assert commands.aborted == [("first", "discord_context_invalid")]
    assert ingress.terminal == [("first", IngressStatus.REJECTED, "discord_context_invalid")]
    assert context.prepared == ["first"]
    assert context.activated == []


@pytest.mark.asyncio
async def test_compensated_replay_settles_original_error_without_discord_prepare() -> None:
    item = request("first")
    commands = FakeCommands(
        results={
            "first": AppliedIngressCommand(
                IngressKind.NEW_DEBATE,
                DebateId.new(),
                AttemptId.new(),
                "discord_context_invalid",
            )
        }
    )
    ingress = FakeIngressRepository(ready=(item,))
    context = FakeContext()

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.failed == 1
    assert commands.aborted == []
    assert context.prepared == []
    assert ingress.terminal == [("first", IngressStatus.FAILED, "discord_context_invalid")]


@pytest.mark.asyncio
async def test_control_preflight_failure_settles_without_mutating_debate() -> None:
    item = control_request("cancel", IngressKind.CANCEL)
    commands = FakeCommands()
    context = FakeContext(
        preflight_errors={"cancel": IngressTerminalFailure("discord_context_invalid")}
    )
    ingress = FakeIngressRepository(ready=(item,))

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.failed == 1
    assert commands.calls == []
    assert commands.aborted == []
    assert ingress.terminal == [("cancel", IngressStatus.FAILED, "discord_context_invalid")]
    assert context.preflighted == ["cancel"]
    assert context.prepared == []
    assert context.activated == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "stop"),
    [
        (RepositoryConflict("claim lost"), IngressDrainStop.CLAIM_LOST),
        (RepositoryUnavailable(), IngressDrainStop.REPOSITORY_UNAVAILABLE),
    ],
)
async def test_compensation_failure_never_settles_ingress_or_overtakes_fifo(
    error: Exception,
    stop: IngressDrainStop,
) -> None:
    first = request("first", offset=1)
    commands = FakeCommands(abort_error=error)
    context = FakeContext(
        prepare_errors={"first": IngressTerminalFailure("discord_context_invalid")}
    )
    ingress = FakeIngressRepository(ready=(first, request("second")))

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.stop is stop
    assert commands.calls == ["first"]
    assert ingress.terminal == []


@pytest.mark.asyncio
async def test_mismatched_command_result_fails_before_context_preparation() -> None:
    item = request("first")
    commands = FakeCommands(
        results={
            "first": AppliedIngressCommand(
                IngressKind.CANCEL,
                DebateId.new(),
                AttemptId.new(),
            )
        }
    )
    ingress = FakeIngressRepository(ready=(item,))
    context = FakeContext()

    report = await drainer(ingress=ingress, commands=commands, context=context).drain_once()

    assert report.failed == 1
    assert ingress.terminal == [("first", IngressStatus.FAILED, "command_result_kind_mismatch")]
    assert context.prepared == []
    assert context.activated == []


@pytest.mark.asyncio
async def test_activation_failure_propagates_after_durable_acceptance() -> None:
    item = request("first")
    ingress = FakeIngressRepository(ready=(item,))
    context = FakeContext(activation_error=RuntimeError("task registry unavailable"))

    with pytest.raises(RuntimeError, match="task registry unavailable"):
        await drainer(ingress=ingress, context=context).drain_once()

    assert len(ingress.accepted) == 1
    assert context.prepared == ["first"]
    assert context.activated == ["first"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_stop"),
    [
        (RepositoryConflict("expired claim"), IngressDrainStop.CLAIM_LOST),
        (RepositoryUnavailable(), IngressDrainStop.REPOSITORY_UNAVAILABLE),
    ],
)
async def test_error_settlement_failure_does_not_claim_the_next_request(
    error: Exception,
    expected_stop: IngressDrainStop,
) -> None:
    ingress = FakeIngressRepository(
        ready=(request("first", offset=1), request("second")),
        settlement_error=error,
    )
    commands = FakeCommands(errors={"first": IngressRejectedFailure("invalid_context")})

    report = await drainer(ingress=ingress, commands=commands).drain_once()

    assert report.stop is expected_stop
    assert report.claimed == 1
    assert commands.calls == ["first"]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (RepositoryUnavailable(), IngressStatus.RETRYING, "repository_unavailable"),
        (TimeoutError(), IngressStatus.RETRYING, "command_timeout"),
        (RuntimeError("unknown"), IngressStatus.RETRYING, "command_failed"),
    ],
)
def test_additional_failure_classifications(
    error: Exception,
    expected_status: IngressStatus,
    expected_code: str,
) -> None:
    assert classify_ingress_failure(error) == IngressFailureDisposition(
        expected_status,
        expected_code,
    )


@pytest.mark.parametrize("code", [" ", "x" * 101])
def test_typed_failure_rejects_invalid_public_error_code(code: str) -> None:
    with pytest.raises(ValueError, match="error code"):
        IngressRetryableFailure(code)


def test_failure_disposition_rejects_nonsettlement_status() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        IngressFailureDisposition(IngressStatus.ACCEPTED, "bad_status")


@pytest.mark.asyncio
async def test_poll_loop_waits_without_busy_spinning_and_stops_cleanly() -> None:
    ingress = FakeIngressRepository()
    worker = IngressDrainer(
        clock=cast(Clock, FakeClock()),
        ingress=cast(IngressRepository, ingress),
        runtime_state=cast(RuntimeStateRepository, FakeRuntimeStateRepository()),
        commands=FakeCommands(),
        context=FakeContext(),
        gate=open_gate(),
        runtime_instance_id=RUNTIME_ID,
        poll_seconds=0.001,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))

    await asyncio.sleep(0.01)
    stop.set()
    await task

    assert ingress.list_calls >= 2
