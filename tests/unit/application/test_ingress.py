"""Durability, authorization, and side-effect ordering for Discord HTTP ingress."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.application import (
    DiscordBotSlot,
    DiscordHttpOperation,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressStatus,
    PanelAction,
    PanelCustomId,
)
from shittim_chest.application.ingress import (
    DiscordIngressApplication,
    IngressOutcome,
)
from shittim_chest.application.models import DebateAuthorizationSnapshot
from shittim_chest.application.ports import (
    DebateLookup,
    IngressRepository,
    ReconciliationTriggerUnavailable,
    RepositoryIdentityConflict,
    RepositoryQueueFull,
    RepositoryUnavailable,
    RuntimeReconciliationTrigger,
    RuntimeStateRepository,
    StatusPublicationTrigger,
    StatusTriggerUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    EnqueuedIngress,
    RuntimeState,
    RuntimeStatus,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

NOW = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
GUILD_ID = "101"
CHANNEL_ID = "102"
THREAD_ID = "103"
PANEL_ID = "104"
REQUESTER_ID = "105"
MODERATOR_APPLICATION_ID = "201"


def runtime_config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=tuple(
            DiscordIdentityConfig(slot=slot, application_id=str(201 + index))
            for index, slot in enumerate(DiscordBotSlot)
        ),
        schema_version="1",
    )


def command(**changes: object) -> DiscordHttpOperation:
    operation = DiscordHttpOperation(
        interaction_id="301",
        operation_id="301",
        kind=IngressKind.NEW_DEBATE,
        application_id=MODERATOR_APPLICATION_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        channel_type=0,
        parent_channel_id=None,
        requester_id=REQUESTER_ID,
        requester_username="requester",
        requester_display_name="Requester",
        can_manage_messages=False,
        received_at=NOW,
        command_name="shittim",
        question="甘い朝ごはんは何がいい?",
    )
    return replace(operation, **changes)


def component(
    *,
    action: PanelAction = PanelAction.CANCEL,
    debate_id: DebateId | None = None,
    attempt_id: AttemptId | None = None,
    **changes: object,
) -> DiscordHttpOperation:
    selected_debate = debate_id or DebateId.new()
    selected_attempt = attempt_id or AttemptId.new()
    panel = PanelCustomId.for_attempt(
        debate_id=selected_debate,
        attempt_id=selected_attempt,
        action=action,
    )
    operation = DiscordHttpOperation(
        interaction_id="302",
        operation_id=panel.operation_id,
        kind=(IngressKind.CANCEL if action is PanelAction.CANCEL else IngressKind.RETRY),
        application_id=MODERATOR_APPLICATION_ID,
        guild_id=GUILD_ID,
        channel_id=THREAD_ID,
        channel_type=11,
        parent_channel_id=CHANNEL_ID,
        requester_id=REQUESTER_ID,
        requester_username="requester",
        requester_display_name="Requester",
        can_manage_messages=False,
        received_at=NOW,
        debate_id=selected_debate,
        expected_attempt_id=selected_attempt,
        custom_id=panel.encode(),
        source_message_id=PANEL_ID,
        source_thread_id=THREAD_ID,
    )
    return replace(operation, **changes)


class FakeIngress:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.replay: EnqueuedIngress | None = None
        self.replay_error: Exception | None = None
        self.enqueue_error: Exception | None = None
        self.saved: IngressRequest | None = None

    async def get_replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        self.events.append("replay")
        if self.replay_error is not None:
            raise self.replay_error
        return self.replay

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        self.events.append("enqueue")
        if self.replay is not None:
            return self.replay
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.saved = request
        return _enqueued(request, created=True)


class FakeClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class FakeRuntimeState:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.state: RuntimeState | None = None

    async def get(self) -> RuntimeState | None:
        self.events.append("runtime-read")
        if self.error is not None:
            raise self.error
        return self.state


class FakeStatusTrigger:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.interaction_ids: list[str] = []

    async def request_publication(self, interaction_id: str) -> None:
        self.events.append("status")
        self.interaction_ids.append(interaction_id)
        if self.error is not None:
            raise self.error


class FakeReconcilerTrigger:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.error: Exception | None = None
        self.interaction_ids: list[str] = []

    async def request_reconciliation(self, interaction_id: str) -> None:
        self.events.append("reconcile")
        self.interaction_ids.append(interaction_id)
        if self.error is not None:
            raise self.error


class FakeDebates:
    def __init__(self, snapshot: DebateAuthorizationSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[DebateId, AttemptId]] = []

    async def get(
        self,
        debate_id: DebateId,
        expected_attempt_id: AttemptId,
    ) -> DebateAuthorizationSnapshot | None:
        self.calls.append((debate_id, expected_attempt_id))
        if (
            self.snapshot is not None
            and self.snapshot.debate_id == debate_id
            and self.snapshot.attempt_id == expected_attempt_id
        ):
            return self.snapshot
        return None


def application(
    *,
    debates: FakeDebates | None = None,
    clock: FakeClock | None = None,
) -> tuple[
    DiscordIngressApplication,
    FakeIngress,
    FakeRuntimeState,
    FakeStatusTrigger,
    FakeReconcilerTrigger,
    list[str],
]:
    events: list[str] = []
    ingress = FakeIngress(events)
    runtime = FakeRuntimeState(events)
    status = FakeStatusTrigger(events)
    reconciler = FakeReconcilerTrigger(events)
    app = DiscordIngressApplication(
        runtime_config=runtime_config(),
        clock=clock or FakeClock(),
        ingress=cast(IngressRepository, ingress),
        runtime_state=cast(RuntimeStateRepository, runtime),
        status_trigger=cast(StatusPublicationTrigger, status),
        reconciler_trigger=cast(RuntimeReconciliationTrigger, reconciler),
        debates=cast(DebateLookup, debates or FakeDebates()),
    )
    return app, ingress, runtime, status, reconciler, events


@pytest.mark.asyncio
async def test_persists_before_status_and_reconciler_kicks() -> None:
    app, ingress, _, _, _, events = application()

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.STARTING
    assert result.created
    assert events == ["enqueue", "runtime-read", "status", "reconcile"]
    assert ingress.saved is not None
    assert ingress.saved.application_id == MODERATOR_APPLICATION_ID
    assert not ingress.saved.requester_can_manage_messages
    assert ingress.saved.question == "甘い朝ごはんは何がいい?"


@pytest.mark.asyncio
async def test_required_persistence_failure_runs_no_accelerator() -> None:
    app, ingress, _, _, _, events = application()
    ingress.enqueue_error = RepositoryUnavailable()

    with pytest.raises(RepositoryUnavailable):
        await app.accept(command())

    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_queue_full_is_ephemeral_result_without_side_effects() -> None:
    app, ingress, _, _, _, events = application()
    ingress.enqueue_error = RepositoryQueueFull()

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.QUEUE_FULL
    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_post_persistence_failures_keep_accepted_result() -> None:
    app, _, runtime, status, reconciler, events = application()
    runtime.error = RepositoryUnavailable()

    assert (await app.accept(command())).outcome is IngressOutcome.STARTING
    assert events == ["enqueue", "runtime-read", "status", "reconcile"]

    events.clear()
    runtime.error = None
    status.error = StatusTriggerUnavailable()
    reconciler.error = ReconciliationTriggerUnavailable()
    assert (await app.accept(command())).outcome is IngressOutcome.STARTING
    assert events == ["enqueue", "runtime-read", "status", "reconcile"]


@pytest.mark.asyncio
async def test_expired_accelerator_budget_leaves_durable_reconciliation_work() -> None:
    app, _, _, _, _, events = application(clock=FakeClock(NOW + timedelta(milliseconds=1500)))

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.STARTING
    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_ready_runtime_uses_operation_specific_accepted_responses() -> None:
    app, _, runtime, _, _, _ = application()
    runtime.state = _ready_runtime()

    assert (await app.accept(command())).outcome is IngressOutcome.ACCEPTED

    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    failed = replace(
        _snapshot(debate_id=debate_id, attempt_id=attempt_id),
        phase=DebatePhase.FAILED,
    )
    app, _, runtime, _, _, _ = application(debates=FakeDebates(failed))
    runtime.state = _ready_runtime()

    result = await app.accept(
        component(
            action=PanelAction.RETRY,
            debate_id=debate_id,
            attempt_id=attempt_id,
        )
    )

    assert result.outcome is IngressOutcome.RETRY_ACCEPTED


@pytest.mark.asyncio
async def test_duplicate_uses_canonical_interaction_for_recovery_kicks() -> None:
    app, ingress, _, status, reconciler, events = application()
    canonical = IngressRequest.new_debate(
        interaction_id="399",
        operation_id="399",
        application_id=MODERATOR_APPLICATION_ID,
        question="甘い朝ごはんは何がいい?",
        requester_id=REQUESTER_ID,
        requester_username="requester",
        requester_display_name="Requester",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        command_name="shittim",
        created_at=NOW,
    )
    ingress.replay = _enqueued(canonical, created=False)

    result = await app.accept(command(interaction_id="399", operation_id="399"))

    assert not result.created
    assert events == ["enqueue", "runtime-read", "status", "reconcile"]
    assert status.interaction_ids == ["399"]
    assert reconciler.interaction_ids == ["399"]


@pytest.mark.asyncio
async def test_component_semantic_replay_uses_persisted_identity_without_debate_reread() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    operation = component(debate_id=debate_id, attempt_id=attempt_id)
    canonical = _control_request(operation, interaction_id="399")
    debates = FakeDebates()
    app, ingress, _, status, reconciler, events = application(debates=debates)
    ingress.replay = _enqueued(canonical, created=False)

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_STARTING
    assert not result.created
    assert events == ["replay", "runtime-read", "status", "reconcile"]
    assert debates.calls == []
    assert status.interaction_ids == ["399"]
    assert reconciler.interaction_ids == ["399"]


@pytest.mark.asyncio
async def test_component_semantic_replay_rejects_changed_identity_without_debate_reread() -> None:
    debates = FakeDebates()
    app, ingress, _, _, _, events = application(debates=debates)
    ingress.replay_error = RepositoryIdentityConflict("immutable identity changed")

    result = await app.accept(component(requester_id="999"))

    assert result.outcome is IngressOutcome.NOT_ALLOWED
    assert events == ["replay"]
    assert debates.calls == []


@pytest.mark.asyncio
async def test_processed_replay_only_repairs_public_status() -> None:
    app, ingress, _, status, _, events = application()
    persisted = IngressRequest.new_debate(
        interaction_id="301",
        operation_id="301",
        application_id=MODERATOR_APPLICATION_ID,
        question="甘い朝ごはんは何がいい?",
        requester_id=REQUESTER_ID,
        requester_username="requester",
        requester_display_name="Requester",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        command_name="shittim",
        created_at=NOW,
    )
    terminal = replace(
        persisted,
        status=IngressStatus.REJECTED,
        completed_at=NOW,
        error_code="request_not_allowed",
    )
    ingress.replay = _enqueued(terminal, created=False)

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.REJECTED
    assert events == ["enqueue", "status"]
    assert status.interaction_ids == ["301"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"application_id": "202"},
        {"guild_id": "999"},
        {"channel_id": "999"},
        {"channel_type": 11},
    ],
)
async def test_command_policy_fails_closed(changes: dict[str, object]) -> None:
    app, _, _, _, _, events = application()

    result = await app.accept(command(**changes))

    assert result.outcome is IngressOutcome.NOT_ALLOWED
    assert events == []


@pytest.mark.asyncio
async def test_component_is_authorized_against_persisted_context_before_enqueue() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    operation = component(debate_id=debate_id, attempt_id=attempt_id)
    snapshot = _snapshot(debate_id=debate_id, attempt_id=attempt_id)
    app, ingress, _, _, _, events = application(debates=FakeDebates(snapshot))

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_STARTING
    assert events == ["replay", "enqueue", "runtime-read", "status", "reconcile"]
    assert ingress.saved is not None
    assert ingress.saved.target_debate_id == debate_id
    assert ingress.saved.expected_attempt_id == attempt_id
    assert ingress.saved.source_message_id == PANEL_ID
    assert ingress.saved.source_thread_id == THREAD_ID


@pytest.mark.asyncio
async def test_manage_messages_permission_authorizes_another_requester() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    operation = component(
        debate_id=debate_id,
        attempt_id=attempt_id,
        requester_id="999",
        can_manage_messages=True,
    )
    snapshot = _snapshot(debate_id=debate_id, attempt_id=attempt_id)
    app, ingress, _, _, _, _ = application(debates=FakeDebates(snapshot))

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_STARTING
    assert ingress.saved is not None
    assert ingress.saved.requester_id == "999"
    assert ingress.saved.requester_can_manage_messages


@pytest.mark.asyncio
async def test_component_rejects_context_actor_and_phase_mismatches() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    valid = component(debate_id=debate_id, attempt_id=attempt_id)
    snapshot = _snapshot(debate_id=debate_id, attempt_id=attempt_id)

    for operation, candidate in (
        (replace(valid, parent_channel_id="999"), snapshot),
        (replace(valid, source_message_id="999"), snapshot),
        (replace(valid, requester_id="999"), snapshot),
        (
            replace(
                component(
                    action=PanelAction.RETRY,
                    debate_id=debate_id,
                    attempt_id=attempt_id,
                )
            ),
            snapshot,
        ),
    ):
        app, _, _, _, _, events = application(debates=FakeDebates(candidate))
        result = await app.accept(operation)
        assert result.outcome is IngressOutcome.NOT_ALLOWED
        assert events == [] or events == ["replay"]


def _snapshot(
    *,
    debate_id: DebateId,
    attempt_id: AttemptId,
) -> DebateAuthorizationSnapshot:
    return DebateAuthorizationSnapshot(
        debate_id=debate_id,
        attempt_id=attempt_id,
        phase=DebatePhase.ACCEPTED,
        requester_id=REQUESTER_ID,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        thread_id=THREAD_ID,
        control_panel_message_id=PANEL_ID,
    )


def _ready_runtime() -> RuntimeState:
    return (
        RuntimeState.stopped(at=NOW - timedelta(seconds=3))
        .request_wake(at=NOW - timedelta(seconds=2))
        .mark_started(
            at=NOW - timedelta(seconds=1),
            runtime_instance_id="runtime-instance",
        )
        .transition(
            RuntimeStatus.READY,
            at=NOW,
            runtime_instance_id="runtime-instance",
        )
    )


def _enqueued(request: IngressRequest, *, created: bool) -> EnqueuedIngress:
    return EnqueuedIngress(
        request=request,
        operation=IngressOperationResult(
            operation_id=request.operation_id,
            interaction_id=request.interaction_id,
            request_sort_key="REQUEST#canonical",
            status=request.status,
            created_at=request.created_at,
            updated_at=request.updated_at,
            error_code=request.error_code,
        ),
        created=created,
    )


def _control_request(
    operation: DiscordHttpOperation,
    *,
    interaction_id: str,
) -> IngressRequest:
    assert operation.custom_id is not None
    assert operation.source_message_id is not None
    assert operation.source_thread_id is not None
    assert operation.parent_channel_id is not None
    assert operation.debate_id is not None
    assert operation.expected_attempt_id is not None
    return IngressRequest.control_operation(
        interaction_id=interaction_id,
        operation_id=operation.operation_id,
        kind=operation.kind,
        application_id=operation.application_id,
        requester_id=operation.requester_id,
        requester_username=operation.requester_username,
        requester_display_name=operation.requester_display_name,
        requester_can_manage_messages=operation.can_manage_messages,
        guild_id=operation.guild_id,
        channel_id=operation.channel_id,
        parent_channel_id=operation.parent_channel_id,
        custom_id=operation.custom_id,
        source_message_id=operation.source_message_id,
        source_thread_id=operation.source_thread_id,
        target_debate_id=operation.debate_id,
        expected_attempt_id=operation.expected_attempt_id,
        created_at=operation.received_at,
    )
