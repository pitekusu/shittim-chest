"""Offline contracts for Discord context preparation and task ownership."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shittim_chest.adapters.discord import build_discord_clients
from shittim_chest.adapters.discord.ingress_runtime import (
    DiscordIngressRuntime,
    _discord_retry_after,
    _panel_failure_disposition,
)
from shittim_chest.application.commands import AppliedIngressCommand
from shittim_chest.application.discord import (
    DISCORD_BOT_SLOTS,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
)
from shittim_chest.application.ingress_drain import (
    DiscordIngressOperation,
    IngressRetryableFailure,
    IngressTerminalFailure,
)
from shittim_chest.application.models import (
    BindDiscordContextCommand,
    DebateSnapshot,
    LeaseGrant,
    MetricEvent,
)
from shittim_chest.application.scale_to_zero import IngressKind, IngressRequest, IngressStatus
from shittim_chest.application.status_publication import status_publication_marker
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
GUILD_ID = "101"
CHANNEL_ID = "102"
MODERATOR_APPLICATION_ID = 201
MODERATOR_USER_ID = 301
STARTER_ID = 401
THREAD_ID = 402
PANEL_ID = 403


@dataclass(slots=True)
class FakeApplication:
    current: DebateSnapshot
    recoverable: tuple[DebateSnapshot, ...] = ()
    binds: list[BindDiscordContextCommand] = field(default_factory=list)
    run_started: asyncio.Event = field(default_factory=asyncio.Event)
    run_calls: list[DebateId] = field(default_factory=list)
    panel_claim_calls: int = 0
    panel_events: list[str] = field(default_factory=list)
    abandoned_panel_refreshes: int = 0

    async def bind_discord_context(self, command: BindDiscordContextCommand) -> object:
        self.binds.append(command)
        self.current = replace(
            self.current,
            starter_message_id=command.starter_message_id,
            thread_id=command.thread_id,
            control_panel_message_id=command.control_panel_message_id,
        )
        return object()

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot:
        assert debate_id == self.current.state.debate_id
        return self.current

    async def claim_recoverable(self) -> tuple[DebateSnapshot, ...]:
        return self.recoverable

    async def run_debate(self, debate_id: DebateId) -> None:
        self.run_calls.append(debate_id)
        self.run_started.set()
        await asyncio.Event().wait()

    async def claim_panel_refresh(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        if (
            self.current.state.debate_id != debate_id
            or self.current.state.attempt_id != attempt_id
            or not self.current.panel_refresh_pending
        ):
            return None
        self.current = replace(
            self.current,
            panel_refresh_claim_owner=claim_owner,
            panel_refresh_claim_expires_at=at + timedelta(seconds=60),
            panel_refresh_next_attempt_at=None,
            panel_refresh_delivery_attempt=self.current.panel_refresh_delivery_attempt + 1,
        )
        return self.current

    async def claim_next_due_panel_refresh(
        self,
        *,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot | None:
        self.panel_claim_calls += 1
        due_at = (
            self.current.panel_refresh_claim_expires_at
            or self.current.panel_refresh_next_attempt_at
            or self.current.panel_refresh_required_at
        )
        if due_at is None or due_at > at:
            self.panel_events.append("empty")
            return None
        claimed = await self.claim_panel_refresh(
            debate_id=self.current.state.debate_id,
            attempt_id=self.current.state.attempt_id,
            claim_owner=claim_owner,
            at=at,
        )
        self.panel_events.append("empty" if claimed is None else "claim")
        return claimed

    async def complete_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
    ) -> DebateSnapshot:
        assert expected == self.current
        assert expected.panel_refresh_claim_owner == claim_owner
        self.current = replace(
            expected,
            panel_refreshed_at=at,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
        )
        self.panel_events.append("complete")
        return self.current

    async def reschedule_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
    ) -> DebateSnapshot:
        del at
        assert expected == self.current
        assert expected.panel_refresh_claim_owner == claim_owner
        self.current = replace(
            expected,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=next_attempt_at,
        )
        return self.current

    async def pending_panel_refresh_count(self) -> int:
        return int(self.current.panel_refresh_pending)

    async def abandon_panel_refresh(
        self,
        *,
        expected: DebateSnapshot,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> DebateSnapshot:
        assert expected == self.current
        assert expected.panel_refresh_claim_owner == claim_owner
        self.current = replace(
            expected,
            panel_refresh_claim_owner=None,
            panel_refresh_claim_expires_at=None,
            panel_refresh_next_attempt_at=None,
            panel_refresh_failed_at=at,
            panel_refresh_error_code=error_code,
        )
        self.abandoned_panel_refreshes += 1
        return self.current

    async def abandoned_panel_refresh_count(self) -> int:
        return self.abandoned_panel_refreshes


class FailingApplication(FakeApplication):
    async def run_debate(self, debate_id: DebateId) -> None:
        self.run_calls.append(debate_id)
        raise RuntimeError("sensitive exception text must not be logged")


@dataclass(slots=True)
class BlockingSnapshotApplication(FakeApplication):
    snapshot_started: asyncio.Event = field(default_factory=asyncio.Event)
    snapshot_release: asyncio.Event = field(default_factory=asyncio.Event)

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot:
        self.snapshot_started.set()
        await self.snapshot_release.wait()
        return await super().get_debate(debate_id)


@dataclass(slots=True)
class BlockingRecoveryApplication(FakeApplication):
    recovery_started: asyncio.Event = field(default_factory=asyncio.Event)
    recovery_release: asyncio.Event = field(default_factory=asyncio.Event)

    async def claim_recoverable(self) -> tuple[DebateSnapshot, ...]:
        self.recovery_started.set()
        await self.recovery_release.wait()
        return await super().claim_recoverable()


@dataclass(slots=True)
class FakeMetrics:
    events: list[tuple[MetricEvent, DebateId]] = field(default_factory=list)

    def increment(self, event: MetricEvent, *, debate_id: DebateId) -> None:
        self.events.append((event, debate_id))


@dataclass(frozen=True, slots=True)
class FakeClock:
    value: datetime = NOW

    def now(self) -> datetime:
        return self.value


def runtime(
    application: FakeApplication,
    *,
    client_set: dict[DiscordBotSlot, discord.Client] | None = None,
    now: datetime = NOW,
    metrics: FakeMetrics | None = None,
) -> DiscordIngressRuntime:
    return DiscordIngressRuntime(
        clients=clients() if client_set is None else client_set,
        application=application,
        panel_refresh=application,
        clock=FakeClock(now),
        metrics=metrics or FakeMetrics(),
        claim_owner="runtime-1",
    )


def config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=tuple(
            DiscordIdentityConfig(slot, str(MODERATOR_APPLICATION_ID + index))
            for index, slot in enumerate(DISCORD_BOT_SLOTS)
        ),
        schema_version="runtime-v1",
    )


def snapshot(*, bound: bool = False, partial: bool = False) -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    starter_message_id: str | None = None
    thread_id: str | None = None
    control_panel_message_id: str | None = None
    if bound:
        starter_message_id = str(STARTER_ID)
        thread_id = str(THREAD_ID)
        control_panel_message_id = str(PANEL_ID)
    elif partial:
        starter_message_id = str(STARTER_ID)
    return DebateSnapshot(
        state=DebateState.accepted(debate_id, attempt_id, at=NOW),
        question="A public-safe fixture question",
        requester_id="requester",
        requester_username="fixture-user",
        requester_display_name="Fixture User",
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id=starter_message_id,
        thread_id=thread_id,
        control_panel_message_id=control_panel_message_id,
    )


def claimed_request(current: DebateSnapshot) -> IngressRequest:
    pending = IngressRequest.new_debate(
        interaction_id="interaction-1",
        operation_id="operation-1",
        application_id=str(MODERATOR_APPLICATION_ID),
        question=current.question,
        requester_id=current.requester_id,
        requester_username=current.requester_username,
        requester_display_name=current.requester_display_name,
        guild_id=current.guild_id,
        channel_id=current.channel_id,
        command_name="shittim",
        created_at=NOW,
    )
    return replace(
        pending,
        status=IngressStatus.CLAIMED,
        status_message_id=str(STARTER_ID),
        status_message_updated_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        claim_owner="runtime-1",
        claim_expires_at=NOW + timedelta(minutes=2),
        delivery_attempt=1,
    )


def claimed_control_request(
    current: DebateSnapshot,
    *,
    kind: IngressKind = IngressKind.CANCEL,
) -> IngressRequest:
    pending = IngressRequest.control_operation(
        interaction_id="interaction-control",
        operation_id="operation-control",
        kind=kind,
        application_id=str(MODERATOR_APPLICATION_ID),
        requester_id=current.requester_id,
        requester_username=current.requester_username,
        requester_display_name=current.requester_display_name,
        requester_can_manage_messages=False,
        guild_id=current.guild_id,
        channel_id=str(THREAD_ID),
        parent_channel_id=current.channel_id,
        source_message_id=str(PANEL_ID),
        source_thread_id=str(THREAD_ID),
        target_debate_id=current.state.debate_id,
        expected_attempt_id=current.state.attempt_id,
        custom_id=f"shittim:v1:{kind.value}:operation-control",
        created_at=NOW,
    )
    return replace(
        pending,
        status=IngressStatus.CLAIMED,
        updated_at=NOW + timedelta(seconds=1),
        claim_owner="runtime-1",
        claim_expires_at=NOW + timedelta(minutes=2),
        delivery_attempt=1,
    )


def application_result(current: DebateSnapshot) -> AppliedIngressCommand:
    return AppliedIngressCommand(
        IngressKind.NEW_DEBATE,
        current.state.debate_id,
        current.state.attempt_id,
    )


def clients() -> dict[DiscordBotSlot, discord.Client]:
    result = build_discord_clients(config())
    moderator = result[DiscordBotSlot.MODERATOR]
    cast(Any, moderator)._connection.user = SimpleNamespace(id=MODERATOR_USER_ID)
    return result


def empty_history() -> Any:
    async def history(**kwargs: object) -> AsyncIterator[discord.Message]:
        del kwargs
        if False:
            yield cast(discord.Message, object())

    return history


def discord_context(
    request: IngressRequest,
) -> tuple[discord.TextChannel, discord.Message, discord.Thread, discord.Message]:
    panel = MagicMock(spec=discord.Message)
    panel.id = PANEL_ID
    panel.author = SimpleNamespace(id=MODERATOR_USER_ID)
    panel.edit = AsyncMock()

    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.guild = SimpleNamespace(id=int(GUILD_ID))
    thread.locked = False
    thread.history.side_effect = empty_history()
    thread.send = AsyncMock(return_value=panel)
    thread.fetch_message = AsyncMock(return_value=panel)

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = int(CHANNEL_ID)
    channel.guild = SimpleNamespace(id=int(GUILD_ID))

    starter = MagicMock(spec=discord.Message)
    starter.id = STARTER_ID
    starter.channel = channel
    starter.author = SimpleNamespace(id=MODERATOR_USER_ID)
    starter.content = f"状態: STARTING\n識別子: {status_publication_marker(request.interaction_id)}"
    starter.thread = thread
    starter.create_thread = AsyncMock(return_value=thread)
    channel.fetch_message = AsyncMock(return_value=starter)
    return (
        cast(discord.TextChannel, channel),
        cast(discord.Message, starter),
        cast(discord.Thread, thread),
        cast(discord.Message, panel),
    )


@pytest.mark.asyncio
async def test_prepare_reuses_durable_status_message_as_starter_and_binds_context() -> None:
    current = snapshot()
    request = claimed_request(current)
    application = FakeApplication(current)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    channel, starter, thread, panel = discord_context(request)
    cast(Any, moderator).get_channel = MagicMock(return_value=channel)

    ingress_runtime = runtime(application, client_set=client_set)
    await ingress_runtime.prepare(request, application_result(current))

    assert application.binds == [
        BindDiscordContextCommand(
            debate_id=current.state.debate_id,
            starter_message_id=str(starter.id),
            thread_id=str(thread.id),
            control_panel_message_id=str(panel.id),
        )
    ]
    starter.create_thread.assert_not_awaited()
    cast(AsyncMock, thread.send).assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_fails_terminally_for_partial_persisted_context() -> None:
    current = snapshot(partial=True)
    application = FakeApplication(current)
    ingress_runtime = runtime(application)

    with pytest.raises(IngressTerminalFailure) as caught:
        await ingress_runtime.prepare(claimed_request(current), application_result(current))

    assert caught.value.code == "DISCORD_OUTBOX_CONFLICT"
    assert application.binds == []


@pytest.mark.asyncio
async def test_prepare_maps_transport_failure_to_stable_retryable_error() -> None:
    current = snapshot()
    application = FakeApplication(current)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    cast(Any, moderator).get_channel = MagicMock(return_value=None)
    cast(Any, moderator).fetch_channel = AsyncMock(side_effect=OSError("offline"))
    ingress_runtime = runtime(application, client_set=client_set)

    with pytest.raises(IngressRetryableFailure) as caught:
        await ingress_runtime.prepare(claimed_request(current), application_result(current))

    assert caught.value.code == "DISCORD_UNAVAILABLE"


@pytest.mark.asyncio
async def test_prepare_preserves_discord_rate_limit_retry_after() -> None:
    current = snapshot()
    application = FakeApplication(current)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    cast(Any, moderator).get_channel = MagicMock(return_value=None)
    cast(Any, moderator).fetch_channel = AsyncMock(side_effect=discord.RateLimited(74.5))
    ingress_runtime = runtime(application, client_set=client_set)

    with pytest.raises(IngressRetryableFailure) as caught:
        await ingress_runtime.prepare(claimed_request(current), application_result(current))

    assert caught.value.code == "DISCORD_RATE_LIMITED"
    assert caught.value.retry_after_seconds == 74.5
    assert caught.value.discord_operation is DiscordIngressOperation.STATUS_CHANNEL_FETCH


@pytest.mark.asyncio
async def test_prepare_identifies_thread_creation_rate_limit_without_provider_content() -> None:
    current = snapshot()
    request = claimed_request(current)
    application = FakeApplication(current)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    channel, starter, _, _ = discord_context(request)
    starter.thread = None
    starter.create_thread = AsyncMock(side_effect=discord.RateLimited(87.25))
    cast(Any, moderator).get_channel = MagicMock(
        side_effect=lambda channel_id: channel if channel_id == int(CHANNEL_ID) else None
    )
    cast(Any, moderator).fetch_channel = AsyncMock(return_value=None)
    ingress_runtime = runtime(application, client_set=client_set)

    with pytest.raises(IngressRetryableFailure) as caught:
        await ingress_runtime.prepare(request, application_result(current))

    assert caught.value.code == "DISCORD_RATE_LIMITED"
    assert caught.value.retry_after_seconds == 87.25
    assert caught.value.discord_operation is DiscordIngressOperation.THREAD_CREATE
    assert application.binds == []


@pytest.mark.asyncio
async def test_control_preflight_accepts_exact_persisted_context_without_mutation() -> None:
    current = snapshot(bound=True)
    application = FakeApplication(current)
    ingress_runtime = runtime(application)

    await ingress_runtime.preflight(claimed_control_request(current))

    assert application.current == current
    assert application.binds == []
    assert application.run_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_change",
    [
        {"guild_id": "999"},
        {"parent_channel_id": "999"},
        {"source_thread_id": "999"},
        {"source_message_id": "999"},
    ],
)
async def test_control_preflight_rejects_context_mismatch_before_mutation(
    request_change: dict[str, str],
) -> None:
    current = snapshot(bound=True)
    application = FakeApplication(current)
    ingress_runtime = runtime(application)
    request = replace(claimed_control_request(current), **request_change)

    with pytest.raises(IngressTerminalFailure) as caught:
        await ingress_runtime.preflight(request)

    assert caught.value.code == "DISCORD_OUTBOX_CONFLICT"
    assert application.current == current
    assert application.binds == []
    assert application.run_calls == []


@pytest.mark.asyncio
async def test_recovery_registers_each_bound_debate_once_in_shared_task_registry() -> None:
    current = snapshot(bound=True)
    application = FakeApplication(current, recoverable=(current,))
    ingress_runtime = runtime(application)

    assert await ingress_runtime.recover_once() == 1
    await asyncio.wait_for(application.run_started.wait(), timeout=1)
    assert await ingress_runtime.recover_once() == 0
    assert ingress_runtime.active_task_count == 1
    assert application.run_calls == [current.state.debate_id]

    await ingress_runtime.checkpoint_active()
    assert ingress_runtime.active_task_count == 0


@pytest.mark.asyncio
async def test_background_debate_failure_logs_only_structural_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    current = snapshot(bound=True)
    application = FailingApplication(current, recoverable=(current,))
    ingress_runtime = runtime(application)

    with caplog.at_level(logging.ERROR, logger="shittim_chest"):
        assert await ingress_runtime.recover_once() == 1
        for _ in range(10):
            await asyncio.sleep(0)
            if ingress_runtime.active_task_count == 0 and any(
                record.name == "shittim_chest" for record in caplog.records
            ):
                break

    diagnostic_records = [record for record in caplog.records if record.name == "shittim_chest"]
    event = json.loads(diagnostic_records[-1].message)
    assert event == {
        "debate_id": str(current.state.debate_id),
        "error_type": "RuntimeError",
        "event": "debate_task_failed",
        "severity": "ERROR",
    }
    assert "sensitive exception text" not in caplog.text


@pytest.mark.asyncio
async def test_activate_does_not_start_after_shutdown_begins_during_snapshot_load() -> None:
    current = snapshot(bound=True)
    application = BlockingSnapshotApplication(current)
    ingress_runtime = runtime(application)
    activation = asyncio.create_task(
        ingress_runtime.activate(claimed_request(current), application_result(current))
    )

    await asyncio.wait_for(application.snapshot_started.wait(), timeout=1)
    ingress_runtime.begin_shutdown()
    application.snapshot_release.set()

    await asyncio.wait_for(activation, timeout=1)
    assert ingress_runtime.active_task_count == 0
    assert application.run_calls == []


@pytest.mark.asyncio
async def test_recovery_keeps_claim_but_does_not_start_after_shutdown_begins() -> None:
    current = replace(
        snapshot(bound=True),
        lease=LeaseGrant("runtime-1", 0, 1, NOW + timedelta(minutes=10)),
    )
    application = BlockingRecoveryApplication(current, recoverable=(current,))
    ingress_runtime = runtime(application)
    recovery = asyncio.create_task(ingress_runtime.recover_once())

    await asyncio.wait_for(application.recovery_started.wait(), timeout=1)
    ingress_runtime.begin_shutdown()
    application.recovery_release.set()

    assert await asyncio.wait_for(recovery, timeout=1) == 0
    assert ingress_runtime.active_task_count == 0
    assert application.run_calls == []
    assert application.current.lease == current.lease


@pytest.mark.asyncio
async def test_recovery_skips_unbound_debate_without_creating_discord_objects() -> None:
    current = snapshot()
    application = FakeApplication(current, recoverable=(current,))
    ingress_runtime = runtime(application)

    assert await ingress_runtime.recover_once() == 0
    assert application.run_calls == []


@pytest.mark.asyncio
async def test_recovery_completes_terminal_panel_without_restarting_debate() -> None:
    active = snapshot(bound=True)
    terminal_at = NOW + timedelta(seconds=1)
    terminal = replace(
        active,
        state=active.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        panel_refresh_required_at=terminal_at,
    )
    application = FakeApplication(terminal)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    _, _, thread, panel = discord_context(claimed_request(terminal))
    cast(AsyncMock, panel.edit).side_effect = lambda **_kwargs: application.panel_events.append(
        "edit"
    )
    cast(Any, moderator).get_channel = MagicMock(return_value=thread)
    ingress_runtime = runtime(
        application,
        client_set=client_set,
        now=terminal_at + timedelta(seconds=1),
    )

    assert await ingress_runtime.recover_once() == 0

    cast(AsyncMock, panel.edit).assert_awaited_once()
    assert application.current.panel_refresh_pending is False
    assert application.current.panel_refreshed_at == terminal_at + timedelta(seconds=1)
    assert application.run_calls == []
    assert application.panel_claim_calls == 2
    assert application.panel_events == ["claim", "edit", "complete", "empty"]


@pytest.mark.asyncio
async def test_terminal_panel_transport_failure_is_durably_rescheduled() -> None:
    active = snapshot(bound=True)
    terminal_at = NOW + timedelta(seconds=1)
    terminal = replace(
        active,
        state=active.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        panel_refresh_required_at=terminal_at,
    )
    application = FakeApplication(terminal)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    _, _, thread, panel = discord_context(claimed_request(terminal))
    cast(AsyncMock, panel.edit).side_effect = OSError("offline")
    cast(Any, moderator).get_channel = MagicMock(return_value=thread)
    delivery_at = terminal_at + timedelta(seconds=1)
    metrics = FakeMetrics()
    ingress_runtime = runtime(
        application,
        client_set=client_set,
        now=delivery_at,
        metrics=metrics,
    )

    assert await ingress_runtime.recover_once() == 0

    assert application.current.panel_refresh_pending is True
    assert application.current.panel_refresh_claim_owner is None
    assert application.current.panel_refresh_next_attempt_at == delivery_at + timedelta(seconds=30)
    assert application.current.panel_refresh_failed_at is None
    assert application.current.panel_refresh_error_code is None
    assert metrics.events == []


@pytest.mark.asyncio
async def test_terminal_panel_transport_retry_uses_capped_exponential_backoff() -> None:
    active = snapshot(bound=True)
    terminal_at = NOW + timedelta(seconds=1)
    terminal = replace(
        active,
        state=active.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        panel_refresh_required_at=terminal_at,
        panel_refresh_delivery_attempt=4,
    )
    application = FakeApplication(terminal)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    _, _, thread, panel = discord_context(claimed_request(terminal))
    cast(AsyncMock, panel.edit).side_effect = OSError("offline")
    cast(Any, moderator).get_channel = MagicMock(return_value=thread)
    delivery_at = terminal_at + timedelta(seconds=1)
    ingress_runtime = runtime(
        application,
        client_set=client_set,
        now=delivery_at,
    )

    assert await ingress_runtime.recover_once() == 0

    assert application.current.panel_refresh_delivery_attempt == 5
    assert application.current.panel_refresh_next_attempt_at == delivery_at + timedelta(seconds=300)


@pytest.mark.asyncio
async def test_terminal_panel_permanent_failure_is_abandoned_and_counted_once() -> None:
    active = snapshot(bound=True)
    terminal_at = NOW + timedelta(seconds=1)
    terminal = replace(
        active,
        state=active.state.transition_to(DebatePhase.CANCELLED, at=terminal_at),
        panel_refresh_required_at=terminal_at,
    )
    application = FakeApplication(terminal)
    client_set = clients()
    moderator = client_set[DiscordBotSlot.MODERATOR]
    cast(Any, moderator).get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
    delivery_at = terminal_at + timedelta(seconds=1)
    metrics = FakeMetrics()
    ingress_runtime = runtime(
        application,
        client_set=client_set,
        now=delivery_at,
        metrics=metrics,
    )

    assert await ingress_runtime.recover_once() == 0
    assert await ingress_runtime.recover_once() == 0

    assert application.current.panel_refresh_pending is False
    assert application.current.panel_refresh_failed_at == delivery_at
    assert application.current.panel_refresh_error_code == "DISCORD_THREAD_UNAVAILABLE"
    assert application.abandoned_panel_refreshes == 1
    assert metrics.events == [(MetricEvent.PANEL_REFRESH_FAILED, terminal.state.debate_id)]


@pytest.mark.parametrize(
    ("status", "retryable", "code"),
    [
        (408, True, "DISCORD_UNAVAILABLE"),
        (409, True, "DISCORD_UNAVAILABLE"),
        (429, True, "DISCORD_RATE_LIMITED"),
        (500, True, "DISCORD_UNAVAILABLE"),
        (400, False, "DISCORD_DELIVERY_REJECTED"),
    ],
)
def test_panel_http_failure_classification_is_explicit(
    status: int,
    retryable: bool,
    code: str,
) -> None:
    response = SimpleNamespace(status=status, reason="fixture", headers={})
    error = discord.HTTPException(cast(Any, response), "fixture")

    assert _panel_failure_disposition(error) == (retryable, code)


def test_ingress_http_rate_limit_uses_retry_after_header() -> None:
    response = SimpleNamespace(status=429, reason="fixture", headers={"Retry-After": "91.25"})
    error = discord.HTTPException(cast(Any, response), "fixture")

    assert _discord_retry_after(error) == 91.25


def test_panel_delivery_timeout_preserves_claim_completion_margin() -> None:
    current = snapshot(bound=True)
    application = FakeApplication(current)
    ingress_runtime = runtime(application, now=NOW)
    fresh_claim = replace(
        current,
        panel_refresh_required_at=NOW,
        panel_refresh_claim_owner="runtime-1",
        panel_refresh_claim_expires_at=NOW + timedelta(seconds=60),
        panel_refresh_delivery_attempt=1,
    )
    expiring_claim = replace(
        fresh_claim,
        panel_refresh_claim_expires_at=NOW + timedelta(seconds=10),
    )

    assert ingress_runtime._remaining_panel_delivery_timeout(fresh_claim) == 30
    assert ingress_runtime._remaining_panel_delivery_timeout(expiring_claim) == 0


def test_runtime_requires_the_dedicated_moderator_client() -> None:
    current = snapshot()
    client_set = clients()
    client_set[DiscordBotSlot.MODERATOR] = discord.Client(intents=discord.Intents.none())

    with pytest.raises(ValueError, match="dedicated moderator"):
        runtime(FakeApplication(current), client_set=client_set)
