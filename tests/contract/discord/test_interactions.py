"""Offline interaction contracts for `/shittim` and its control panel."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shittim_chest.adapters.discord import (
    DiscordInteractionController,
    build_discord_clients,
)
from shittim_chest.application import (
    DISCORD_BOT_SLOTS,
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    BindDiscordContextCommand,
    CancelDebateCommand,
    CancelledDebate,
    DebateSnapshot,
    DiscordBotSlot,
    DiscordIdentityConfig,
    DiscordRuntimeConfig,
    PanelAction,
    PanelCustomId,
    RetryDebateCommand,
    nonce_from_uuid7,
)
from shittim_chest.application.errors import InvalidApplicationOperation
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
GUILD_ID = "101"
CHANNEL_ID = "102"
MODERATOR_APPLICATION_ID = 201
REQUESTER_ID = 301
INTERACTION_ID = 401
STARTER_ID = 501
THREAD_ID = 502
PANEL_ID = 503


def config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=tuple(
            DiscordIdentityConfig(slot, str(201 + index))
            for index, slot in enumerate(DISCORD_BOT_SLOTS)
        ),
        schema_version="runtime-v1",
    )


def snapshot(*, phase: DebatePhase = DebatePhase.ACCEPTED) -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    state = DebateState.accepted(debate_id, attempt_id, at=NOW)
    if phase is not DebatePhase.ACCEPTED:
        state = state.transition_to(phase, at=NOW + timedelta(seconds=1))
    return DebateSnapshot(
        state=state,
        question="今日の朝ごはんは何がいい? 甘いものが食べたい",
        requester_id=str(REQUESTER_ID),
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        created_at=NOW,
        attempt_created_at=NOW,
        error_code="test_failure" if phase is DebatePhase.FAILED else None,
    )


@dataclass(slots=True)
class FakeApplication:
    current: DebateSnapshot
    events: list[str] = field(default_factory=list)
    accept_requests: list[AcceptDebateRequest] = field(default_factory=list)
    cancel_commands: list[CancelDebateCommand] = field(default_factory=list)
    retry_commands: list[RetryDebateCommand] = field(default_factory=list)
    run_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def accept_debate(self, request: AcceptDebateRequest) -> AcceptedDebate:
        self.events.append("accept")
        self.accept_requests.append(request)
        return AcceptedDebate(self.current.state.debate_id, self.current.state.attempt_id)

    async def bind_discord_context(self, command: BindDiscordContextCommand) -> object:
        self.events.append("bind")
        self.current = replace(
            self.current,
            starter_message_id=command.starter_message_id,
            thread_id=command.thread_id,
            control_panel_message_id=command.control_panel_message_id,
        )
        return object()

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot:
        if debate_id != self.current.state.debate_id:
            raise InvalidApplicationOperation("unknown debate")
        return self.current

    async def run_debate(self, debate_id: DebateId) -> None:
        assert debate_id == self.current.state.debate_id
        self.events.append("run")
        self.run_started.set()
        await asyncio.Event().wait()

    async def cancel_debate(self, command: CancelDebateCommand) -> CancelledDebate:
        self.cancel_commands.append(command)
        if command.expected_attempt_id != self.current.state.attempt_id:
            raise InvalidApplicationOperation("panel operation is bound to another attempt")
        if self.current.state.phase is not DebatePhase.CANCELLED:
            self.current = replace(
                self.current,
                state=self.current.state.transition_to(
                    DebatePhase.CANCELLED,
                    at=NOW + timedelta(seconds=2),
                ),
            )
        return CancelledDebate(command.debate_id, self.current.state.attempt_id)

    async def retry_debate(self, command: RetryDebateCommand) -> AcceptedRetry:
        self.retry_commands.append(command)
        if command.expected_attempt_id != self.current.state.attempt_id:
            raise InvalidApplicationOperation("panel operation is bound to another attempt")
        if self.current.state.phase is not DebatePhase.FAILED:
            raise InvalidApplicationOperation("only a failed debate may be retried")
        source = self.current.state.attempt_id
        new_attempt = AttemptId.new()
        retry_state = self.current.state.new_retry_attempt(
            new_attempt,
            at=NOW + timedelta(seconds=2),
        )
        self.current = replace(
            self.current,
            state=retry_state,
            attempt_created_at=retry_state.updated_at,
            error_code=None,
        )
        return AcceptedRetry(command.debate_id, new_attempt, source)


def clients() -> dict[DiscordBotSlot, discord.Client]:
    result = build_discord_clients(config())
    moderator = result[DiscordBotSlot.MODERATOR]
    cast(Any, moderator)._connection.user = SimpleNamespace(id=MODERATOR_APPLICATION_ID)
    cast(Any, moderator).get_channel = MagicMock(return_value=None)
    cast(Any, moderator).fetch_channel = AsyncMock(return_value=None)
    return result


def empty_history(events: list[str] | None = None) -> Any:
    messages: tuple[discord.Message, ...] = ()

    async def history(**kwargs: object) -> AsyncIterator[discord.Message]:
        del kwargs
        if events is not None:
            events.append("history")
        for message in messages:
            yield message

    return history


def text_channel(
    *,
    events: list[str],
) -> tuple[discord.TextChannel, discord.Message, discord.Thread, discord.Message]:
    channel_mock = MagicMock(spec=discord.TextChannel)
    channel_mock.history.side_effect = empty_history(events)

    panel_mock = MagicMock(spec=discord.Message)
    panel_mock.id = PANEL_ID
    panel_mock.edit = AsyncMock()

    thread_mock = MagicMock(spec=discord.Thread)
    thread_mock.id = THREAD_ID
    thread_mock.guild = SimpleNamespace(id=int(GUILD_ID))
    thread_mock.locked = False
    thread_mock.history.side_effect = empty_history()
    thread_mock.send = AsyncMock(return_value=panel_mock)
    thread_mock.fetch_message = AsyncMock(return_value=panel_mock)

    starter_mock = MagicMock(spec=discord.Message)
    starter_mock.id = STARTER_ID
    starter_mock.thread = None
    starter_mock.create_thread = AsyncMock(return_value=thread_mock)
    channel_mock.send = AsyncMock(return_value=starter_mock)
    return (
        cast(discord.TextChannel, channel_mock),
        cast(discord.Message, starter_mock),
        cast(discord.Thread, thread_mock),
        cast(discord.Message, panel_mock),
    )


def interaction(
    *,
    channel: discord.TextChannel | discord.Thread,
    interaction_type: discord.InteractionType = discord.InteractionType.application_command,
    custom_id: str | None = None,
    message: discord.Message | None = None,
) -> discord.Interaction[discord.Client]:
    interaction_mock = MagicMock(spec=discord.Interaction)
    interaction_mock.id = INTERACTION_ID
    interaction_mock.type = interaction_type
    interaction_mock.application_id = MODERATOR_APPLICATION_ID
    interaction_mock.guild_id = int(GUILD_ID)
    interaction_mock.channel_id = int(CHANNEL_ID if message is None else THREAD_ID)
    interaction_mock.channel = channel
    interaction_mock.user = SimpleNamespace(id=REQUESTER_ID)
    interaction_mock.permissions = discord.Permissions(manage_messages=False)
    interaction_mock.created_at = NOW
    interaction_mock.message = message
    interaction_mock.data = {"custom_id": custom_id} if custom_id is not None else {}
    interaction_mock.response = SimpleNamespace(defer=AsyncMock())
    interaction_mock.edit_original_response = AsyncMock()
    return cast(discord.Interaction[discord.Client], interaction_mock)


def bind_context(current: DebateSnapshot) -> DebateSnapshot:
    return replace(
        current,
        starter_message_id=str(STARTER_ID),
        thread_id=str(THREAD_ID),
        control_panel_message_id=str(PANEL_ID),
    )


@pytest.mark.asyncio
async def test_command_schema_is_guild_scoped_bounded_and_synced_only_when_changed() -> None:
    client_set = clients()
    application = FakeApplication(snapshot())
    controller = DiscordInteractionController(
        clients=client_set,
        config=config(),
        application=application,
    )
    guild = discord.Object(id=int(GUILD_ID))
    command = controller.command_tree.get_command("shittim", guild=guild)
    sync = AsyncMock(return_value=[])
    cast(Any, controller.command_tree).sync = sync

    assert command is not None
    payload = command.to_dict(controller.command_tree)
    assert payload["name"] == "shittim"
    assert payload["options"][0]["min_length"] == 1
    assert payload["options"][0]["max_length"] == 1000
    assert not await controller.sync_command_if_changed(
        previous_schema_hash=controller.command_schema_hash
    )
    assert await controller.sync_command_if_changed(previous_schema_hash=None)
    sync.assert_awaited_once_with(guild=guild)
    await controller.close()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_command_before_acceptance_or_task_creation() -> None:
    current = snapshot()
    application = FakeApplication(current)
    channel, _, _, _ = text_channel(events=application.events)
    current_interaction = interaction(channel=channel)
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    controller.begin_shutdown()
    await controller._command_callback(current_interaction, current.question)

    assert application.accept_requests == []
    response = cast(Any, current_interaction).edit_original_response
    response.assert_awaited_once()
    assert "runtime_not_ready" in response.await_args.kwargs["content"]
    await controller.close()


@pytest.mark.asyncio
async def test_checkpoint_surfaces_owned_debate_cleanup_failure() -> None:
    current = snapshot()
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=FakeApplication(current),
    )
    started = asyncio.Event()

    async def fail_during_checkpoint() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("checkpoint write failed") from None

    task = asyncio.create_task(fail_during_checkpoint())
    controller._tasks[current.state.debate_id] = task
    await started.wait()

    with pytest.raises(RuntimeError, match="failed during checkpoint"):
        await controller.checkpoint_active()

    assert task.done()
    await controller.close()


@pytest.mark.asyncio
async def test_command_defers_first_then_creates_and_binds_public_context() -> None:
    current = snapshot()
    application = FakeApplication(current)
    channel, starter, thread, panel = text_channel(events=application.events)
    current_interaction = interaction(channel=channel)
    response = cast(Any, current_interaction.response)
    response.defer.side_effect = lambda **kwargs: application.events.append("defer")
    client_set = clients()
    controller = DiscordInteractionController(
        clients=client_set,
        config=config(),
        application=application,
    )

    await controller._command_callback(current_interaction, current.question)
    await asyncio.wait_for(application.run_started.wait(), timeout=1)

    assert application.events[0] == "defer"
    assert application.accept_requests[0].operation_id == str(INTERACTION_ID)
    cast(Any, channel).send.assert_awaited_once()
    starter_kwargs = cast(Any, channel).send.await_args.kwargs
    assert starter_kwargs["nonce"] == str(INTERACTION_ID)
    assert starter_kwargs["allowed_mentions"].to_dict() == {"parse": []}
    cast(Any, starter).create_thread.assert_awaited_once()
    cast(Any, thread).send.assert_awaited_once()
    panel_kwargs = cast(Any, thread).send.await_args.kwargs
    assert panel_kwargs["allowed_mentions"].to_dict() == {"parse": []}
    view = cast(discord.ui.View, panel_kwargs["view"])
    assert len(view.children) == 2
    assert application.current.thread_id == str(THREAD_ID)
    assert application.current.control_panel_message_id == str(PANEL_ID)
    cast(Any, current_interaction).edit_original_response.assert_awaited_once()
    assert not cast(Any, panel).edit.await_count
    await controller.close()


@pytest.mark.asyncio
async def test_command_replay_reuses_bound_context_without_another_send() -> None:
    current = bind_context(snapshot())
    application = FakeApplication(current)
    channel, _, _, _ = text_channel(events=application.events)
    current_interaction = interaction(channel=channel)
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._command_callback(current_interaction, current.question)
    await asyncio.wait_for(application.run_started.wait(), timeout=1)

    cast(Any, channel).send.assert_not_awaited()
    response_content = cast(Any, current_interaction).edit_original_response.await_args.kwargs[
        "content"
    ]
    assert f"<#{THREAD_ID}>" in response_content
    await controller.close()


@pytest.mark.asyncio
async def test_unbound_command_replay_recovers_starter_thread_and_panel_from_history() -> None:
    current = snapshot()
    application = FakeApplication(current)
    channel, starter, thread, panel = text_channel(events=application.events)
    cast(Any, starter).thread = thread
    cast(Any, starter).author = SimpleNamespace(id=MODERATOR_APPLICATION_ID)
    cast(Any, starter).nonce = str(INTERACTION_ID)
    cast(Any, starter).content = f"**質問**\n{current.question}\n\n3つの視点で合議を開始します。"
    cast(Any, panel).author = SimpleNamespace(id=MODERATOR_APPLICATION_ID)
    cast(Any, panel).nonce = nonce_from_uuid7(current.state.attempt_id.value)
    cast(Any, panel).content = "\n".join(
        (
            "**操作パネル**",
            f"状態: `{current.state.phase.value}`",
            f"試行: `{current.state.attempt_id}`",
        )
    )

    async def starter_history(**kwargs: object) -> AsyncIterator[discord.Message]:
        del kwargs
        yield starter

    async def panel_history(**kwargs: object) -> AsyncIterator[discord.Message]:
        del kwargs
        yield panel

    cast(Any, channel).history.side_effect = starter_history
    cast(Any, thread).history.side_effect = panel_history
    current_interaction = interaction(channel=channel)
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._command_callback(current_interaction, current.question)
    await asyncio.wait_for(application.run_started.wait(), timeout=1)

    cast(Any, channel).send.assert_not_awaited()
    cast(Any, starter).create_thread.assert_not_awaited()
    cast(Any, thread).send.assert_not_awaited()
    assert application.current.control_panel_message_id == str(PANEL_ID)
    await controller.close()


@pytest.mark.asyncio
async def test_setup_failure_releases_the_unbound_attempt_and_returns_safe_error() -> None:
    current = snapshot()
    application = FakeApplication(current)
    channel, _, _, _ = text_channel(events=application.events)
    cast(Any, channel).send.side_effect = OSError("private transport detail")
    current_interaction = interaction(channel=channel)
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._command_callback(current_interaction, current.question)

    assert application.current.state.phase is DebatePhase.CANCELLED
    content = cast(Any, current_interaction).edit_original_response.await_args.kwargs["content"]
    assert content == "Discordとの通信に失敗しました。しばらくしてからお試しください。"
    assert "private transport detail" not in content
    await controller.close()


@pytest.mark.asyncio
async def test_command_rejects_a_thread_invocation_after_ephemeral_defer() -> None:
    current = snapshot()
    application = FakeApplication(current)
    thread_mock = MagicMock(spec=discord.Thread)
    current_interaction = interaction(channel=cast(discord.Thread, thread_mock))
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._command_callback(current_interaction, current.question)

    cast(Any, current_interaction.response).defer.assert_awaited_once_with(
        ephemeral=True,
        thinking=True,
    )
    assert application.accept_requests == []
    content = cast(Any, current_interaction).edit_original_response.await_args.kwargs["content"]
    assert content == "操作を完了できませんでした。"
    await controller.close()


@pytest.mark.asyncio
async def test_cancel_panel_checks_context_attempt_and_actor_then_disables_panel() -> None:
    current = bind_context(snapshot())
    application = FakeApplication(current)
    panel_id = PanelCustomId.for_attempt(
        debate_id=current.state.debate_id,
        attempt_id=current.state.attempt_id,
        action=PanelAction.CANCEL,
    )
    thread_mock = MagicMock(spec=discord.Thread)
    message_mock = MagicMock(spec=discord.Message)
    message_mock.id = PANEL_ID
    message_mock.edit = AsyncMock()
    current_interaction = interaction(
        channel=cast(discord.Thread, thread_mock),
        interaction_type=discord.InteractionType.component,
        custom_id=panel_id.encode(),
        message=cast(discord.Message, message_mock),
    )
    client_set = clients()
    controller = DiscordInteractionController(
        clients=client_set,
        config=config(),
        application=application,
    )

    moderator = client_set[DiscordBotSlot.MODERATOR]
    await cast(Any, moderator).on_interaction(current_interaction)

    command = application.cancel_commands[0]
    assert command.expected_attempt_id == current.state.attempt_id
    assert command.actor_id == str(REQUESTER_ID)
    assert application.current.state.phase is DebatePhase.CANCELLED
    cast(Any, message_mock).edit.assert_awaited_once()
    edited_view = cast(discord.ui.View, cast(Any, message_mock).edit.await_args.kwargs["view"])
    assert all(cast(discord.ui.Button[Any], child).disabled for child in edited_view.children)
    await controller.close()


@pytest.mark.asyncio
async def test_retry_panel_creates_new_attempt_updates_ids_and_starts_owned_task() -> None:
    current = bind_context(snapshot(phase=DebatePhase.FAILED))
    source_attempt = current.state.attempt_id
    application = FakeApplication(current)
    panel_id = PanelCustomId.for_attempt(
        debate_id=current.state.debate_id,
        attempt_id=source_attempt,
        action=PanelAction.RETRY,
    )
    thread_mock = MagicMock(spec=discord.Thread)
    message_mock = MagicMock(spec=discord.Message)
    message_mock.id = PANEL_ID
    message_mock.edit = AsyncMock()
    current_interaction = interaction(
        channel=cast(discord.Thread, thread_mock),
        interaction_type=discord.InteractionType.component,
        custom_id=panel_id.encode(),
        message=cast(discord.Message, message_mock),
    )
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._on_interaction(current_interaction)
    await asyncio.wait_for(application.run_started.wait(), timeout=1)

    command = application.retry_commands[0]
    assert command.expected_attempt_id == source_attempt
    assert application.current.state.attempt_id != source_attempt
    edited_view = cast(discord.ui.View, cast(Any, message_mock).edit.await_args.kwargs["view"])
    for child in edited_view.children:
        parsed = PanelCustomId.parse(cast(discord.ui.Button[Any], child).custom_id or "")
        assert parsed.expected_attempt_id() == application.current.state.attempt_id
    await controller.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["application", "guild", "thread", "message"])
async def test_panel_context_mismatch_fails_closed_without_calling_use_case(
    mismatch: str,
) -> None:
    current = bind_context(snapshot())
    application = FakeApplication(current)
    panel_id = PanelCustomId.for_attempt(
        debate_id=current.state.debate_id,
        attempt_id=current.state.attempt_id,
        action=PanelAction.CANCEL,
    )
    thread_mock = MagicMock(spec=discord.Thread)
    message_mock = MagicMock(spec=discord.Message)
    message_mock.id = 999
    current_interaction = interaction(
        channel=cast(discord.Thread, thread_mock),
        interaction_type=discord.InteractionType.component,
        custom_id=panel_id.encode(),
        message=cast(discord.Message, message_mock),
    )
    if mismatch == "application":
        cast(Any, current_interaction).application_id = 999
        message_mock.id = PANEL_ID
    elif mismatch == "guild":
        cast(Any, current_interaction).guild_id = 999
        message_mock.id = PANEL_ID
    elif mismatch == "thread":
        cast(Any, current_interaction).channel_id = 999
        message_mock.id = PANEL_ID
    controller = DiscordInteractionController(
        clients=clients(),
        config=config(),
        application=application,
    )

    await controller._on_interaction(current_interaction)

    assert application.cancel_commands == []
    content = cast(Any, current_interaction).edit_original_response.await_args.kwargs["content"]
    assert content == "操作を完了できませんでした。"
    await controller.close()
