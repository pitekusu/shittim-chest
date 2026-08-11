"""Durability, authorization, and side-effect ordering for Discord HTTP ingress."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
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
    RepositoryIdentityConflict,
    RepositoryQueueFull,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    EnqueuedIngress,
    StatusMessageState,
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
        farewell_channel_id=CHANNEL_ID,
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
) -> tuple[
    DiscordIngressApplication,
    FakeIngress,
    list[str],
]:
    events: list[str] = []
    ingress = FakeIngress(events)
    app = DiscordIngressApplication(
        runtime_config=runtime_config(),
        ingress=cast(IngressRepository, ingress),
        debates=cast(DebateLookup, debates or FakeDebates()),
    )
    return app, ingress, events


@pytest.mark.asyncio
async def test_command_persists_pending_without_pre_response_accelerators() -> None:
    app, ingress, events = application()

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.PENDING
    assert result.created
    assert events == ["enqueue"]
    assert ingress.saved is not None
    assert ingress.saved.application_id == MODERATOR_APPLICATION_ID
    assert not ingress.saved.requester_can_manage_messages
    assert ingress.saved.question == "甘い朝ごはんは何がいい?"
    assert ingress.saved.status_message_state is StatusMessageState.PENDING


@pytest.mark.asyncio
async def test_required_persistence_failure_has_no_other_side_effect() -> None:
    app, ingress, events = application()
    ingress.enqueue_error = RepositoryUnavailable()

    with pytest.raises(RepositoryUnavailable):
        await app.accept(command())

    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_queue_full_is_ephemeral_result_without_side_effects() -> None:
    app, ingress, events = application()
    ingress.enqueue_error = RepositoryQueueFull()

    result = await app.accept(command())

    assert result.outcome is IngressOutcome.QUEUE_FULL
    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_component_uses_operation_specific_accepted_response() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    failed = replace(
        _snapshot(debate_id=debate_id, attempt_id=attempt_id),
        phase=DebatePhase.FAILED,
    )
    app, _, events = application(debates=FakeDebates(failed))

    result = await app.accept(
        component(
            action=PanelAction.RETRY,
            debate_id=debate_id,
            attempt_id=attempt_id,
        )
    )

    assert result.outcome is IngressOutcome.RETRY_ACCEPTED
    assert events == ["replay", "enqueue"]


@pytest.mark.asyncio
async def test_duplicate_uses_canonical_durable_result_without_accelerators() -> None:
    app, ingress, events = application()
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
    assert events == ["enqueue"]


@pytest.mark.asyncio
async def test_component_semantic_replay_uses_persisted_identity_without_debate_reread() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    operation = component(debate_id=debate_id, attempt_id=attempt_id)
    canonical = _control_request(operation, interaction_id="399")
    debates = FakeDebates()
    app, ingress, events = application(debates=debates)
    ingress.replay = _enqueued(canonical, created=False)

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_ACCEPTED
    assert not result.created
    assert events == ["replay"]
    assert debates.calls == []


@pytest.mark.asyncio
async def test_component_semantic_replay_rejects_changed_identity_without_debate_reread() -> None:
    debates = FakeDebates()
    app, ingress, events = application(debates=debates)
    ingress.replay_error = RepositoryIdentityConflict("immutable identity changed")

    result = await app.accept(component(requester_id="999"))

    assert result.outcome is IngressOutcome.NOT_ALLOWED
    assert events == ["replay"]
    assert debates.calls == []


@pytest.mark.asyncio
async def test_processed_replay_only_repairs_public_status() -> None:
    app, ingress, events = application()
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
    assert events == ["enqueue"]


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
    app, _, events = application()

    result = await app.accept(command(**changes))

    assert result.outcome is IngressOutcome.NOT_ALLOWED
    assert events == []


@pytest.mark.asyncio
async def test_component_is_authorized_against_persisted_context_before_enqueue() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    operation = component(debate_id=debate_id, attempt_id=attempt_id)
    snapshot = _snapshot(debate_id=debate_id, attempt_id=attempt_id)
    app, ingress, events = application(debates=FakeDebates(snapshot))

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_ACCEPTED
    assert events == ["replay", "enqueue"]
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
    app, ingress, _ = application(debates=FakeDebates(snapshot))

    result = await app.accept(operation)

    assert result.outcome is IngressOutcome.CANCEL_ACCEPTED
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
        app, _, events = application(debates=FakeDebates(candidate))
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
