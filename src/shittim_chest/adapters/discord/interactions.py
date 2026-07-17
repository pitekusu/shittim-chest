"""Guild-scoped slash command and control-panel interaction runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Protocol

import discord
from discord import app_commands

from shittim_chest.adapters.discord.errors import DiscordAdapterError
from shittim_chest.adapters.discord.gateway import DiscordModeratorClient
from shittim_chest.application import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    BindDiscordContextCommand,
    CancelDebateCommand,
    CancelledDebate,
    DebateSnapshot,
    DiscordBotSlot,
    DiscordRuntimeConfig,
    PanelAction,
    PanelCustomId,
    RetryDebateCommand,
    nonce_from_uuid7,
)
from shittim_chest.application.errors import ApplicationError
from shittim_chest.application.ports import (
    RepositoryBusy,
    RepositoryConflict,
    RepositoryQuotaExceeded,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

COMMAND_NAME = "shittim"
COMMAND_DESCRIPTION = "3つの視点で質問を合議します"
QUESTION_DESCRIPTION = "合議したい質問"
HISTORY_LIMIT = 100


class DebateInteractionUseCases(Protocol):
    """Application operations consumed by the Discord interaction boundary."""

    async def accept_debate(self, request: AcceptDebateRequest) -> AcceptedDebate: ...

    async def bind_discord_context(
        self,
        command: BindDiscordContextCommand,
    ) -> object: ...

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot: ...

    async def run_debate(self, debate_id: DebateId) -> None: ...

    async def cancel_debate(self, command: CancelDebateCommand) -> CancelledDebate: ...

    async def retry_debate(self, command: RetryDebateCommand) -> AcceptedRetry: ...


class DiscordInteractionController:
    """Own one Guild command, component dispatch, and debate background tasks."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        config: DiscordRuntimeConfig,
        application: DebateInteractionUseCases,
    ) -> None:
        if set(clients) != set(DiscordBotSlot):
            raise ValueError("interaction controller requires every Discord Bot slot")
        self._clients = dict(clients)
        self._config = config
        self._application = application
        moderator = self._clients[DiscordBotSlot.MODERATOR]
        if not isinstance(moderator, DiscordModeratorClient):
            raise ValueError("interaction controller requires a dedicated moderator client")
        self._moderator = moderator
        self._guild = discord.Object(id=int(config.guild_id))
        self._tree = app_commands.CommandTree(self._moderator)
        self._tasks: dict[DebateId, asyncio.Task[None]] = {}
        self._register_command()
        self._register_component_listener()

    @property
    def command_tree(self) -> app_commands.CommandTree[discord.Client]:
        """Expose the registered tree for deploy-time schema inspection only."""

        return self._tree

    @property
    def command_schema_hash(self) -> str:
        """Return a stable hash used to avoid unconditional command synchronization."""

        encoded = json.dumps(
            _command_schema(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def sync_command_if_changed(self, *, previous_schema_hash: str | None) -> bool:
        """Explicitly sync the Guild command only when a deploy-provided hash differs."""

        if previous_schema_hash == self.command_schema_hash:
            return False
        await self._tree.sync(guild=self._guild)
        return True

    async def close(self) -> None:
        """Cancel and await every debate task owned by this controller."""

        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._moderator.clear_interaction_handler()

    def _register_command(self) -> None:
        command: app_commands.Command[app_commands.Group, ..., None] = app_commands.Command(
            name=COMMAND_NAME,
            description=COMMAND_DESCRIPTION,
            callback=self._command_callback,
        )
        self._tree.add_command(command, guild=self._guild)

    def _register_component_listener(self) -> None:
        self._moderator.set_interaction_handler(self._on_interaction)

    @app_commands.describe(question=QUESTION_DESCRIPTION)
    async def _command_callback(
        self,
        interaction: discord.Interaction[discord.Client],
        question: app_commands.Range[str, 1, 1000],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        accepted: AcceptedDebate | None = None
        try:
            request, channel = self._accept_request(interaction, question)
            accepted = await self._application.accept_debate(request)
            snapshot = await self._application.get_debate(accepted.debate_id)
            if _has_bound_context(snapshot):
                await self._finish_accept(interaction, snapshot)
                self._start_debate(snapshot.state.debate_id)
                return
            bound = await self._provision_context(
                interaction=interaction,
                channel=channel,
                question=question,
                snapshot=snapshot,
            )
            await self._finish_accept(interaction, bound)
            self._start_debate(bound.state.debate_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if accepted is not None:
                await self._cancel_unbound(interaction, accepted)
            await self._respond_with_error(interaction, error)

    async def _on_interaction(self, interaction: discord.Interaction[discord.Client]) -> None:
        custom_id = _component_custom_id(interaction)
        if custom_id is None or not custom_id.startswith("shittim:v1:"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            panel_id = PanelCustomId.parse(custom_id)
            expected_attempt = panel_id.expected_attempt_id()
            snapshot, message = await self._panel_context(interaction, panel_id)
            can_manage_messages = interaction.permissions.manage_messages
            if panel_id.action is PanelAction.CANCEL:
                await self._handle_cancel(
                    interaction,
                    message,
                    snapshot,
                    expected_attempt,
                    can_manage_messages,
                    panel_id.operation_id,
                )
            else:
                await self._handle_retry(
                    interaction,
                    message,
                    snapshot,
                    expected_attempt,
                    can_manage_messages,
                    panel_id.operation_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._respond_with_error(interaction, error)

    def _accept_request(
        self,
        interaction: discord.Interaction[discord.Client],
        question: str,
    ) -> tuple[AcceptDebateRequest, discord.TextChannel]:
        channel = interaction.channel
        if (
            interaction.guild_id is None
            or interaction.channel_id is None
            or interaction.user is None
            or not isinstance(channel, discord.TextChannel)
        ):
            raise ValueError("the command requires a Guild text channel")
        return (
            AcceptDebateRequest(
                question=question,
                requester_id=str(interaction.user.id),
                guild_id=str(interaction.guild_id),
                channel_id=str(interaction.channel_id),
                operation_id=str(interaction.id),
            ),
            channel,
        )

    async def _provision_context(
        self,
        *,
        interaction: discord.Interaction[discord.Client],
        channel: discord.TextChannel,
        question: str,
        snapshot: DebateSnapshot,
    ) -> DebateSnapshot:
        starter_content = _starter_content(question)
        starter = await self._find_message(
            channel,
            nonce=str(interaction.id),
            content=starter_content,
            after=interaction.created_at,
        )
        if starter is None:
            starter = await channel.send(
                starter_content,
                nonce=str(interaction.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        thread = await self._resolve_or_create_thread(starter, snapshot)
        panel = await self._resolve_or_create_panel(thread, snapshot)
        await self._application.bind_discord_context(
            BindDiscordContextCommand(
                debate_id=snapshot.state.debate_id,
                starter_message_id=str(starter.id),
                thread_id=str(thread.id),
                control_panel_message_id=str(panel.id),
            )
        )
        return await self._application.get_debate(snapshot.state.debate_id)

    async def _resolve_or_create_thread(
        self,
        starter: discord.Message,
        snapshot: DebateSnapshot,
    ) -> discord.Thread:
        thread: discord.Thread | None
        if starter.thread is not None:
            thread = starter.thread
        else:
            cached = self._moderator.get_channel(starter.id)
            thread = cached if isinstance(cached, discord.Thread) else None
        if thread is None:
            try:
                fetched = await self._moderator.fetch_channel(starter.id)
            except discord.NotFound:
                fetched = None
            thread = fetched if isinstance(fetched, discord.Thread) else None
        if thread is None:
            thread = await starter.create_thread(
                name=f"Shittim {str(snapshot.state.debate_id)[:8]}",
                auto_archive_duration=1440,
            )
        if str(thread.guild.id) != snapshot.guild_id or thread.locked:
            raise ValueError("the created thread is unavailable")
        return thread

    async def _resolve_or_create_panel(
        self,
        thread: discord.Thread,
        snapshot: DebateSnapshot,
    ) -> discord.Message:
        content = _panel_content(snapshot)
        nonce = nonce_from_uuid7(snapshot.state.attempt_id.value)
        panel = await self._find_message(
            thread,
            nonce=nonce,
            content=content,
            after=snapshot.attempt_created_at,
        )
        if panel is not None:
            return panel
        return await thread.send(
            content,
            nonce=nonce,
            allowed_mentions=discord.AllowedMentions.none(),
            view=_panel_view(snapshot),
        )

    async def _find_message(
        self,
        channel: discord.TextChannel | discord.Thread,
        *,
        nonce: str,
        content: str,
        after: datetime,
    ) -> discord.Message | None:
        user = self._moderator.user
        if user is None:
            raise ValueError("moderator identity is not ready")
        history: AsyncIterator[discord.Message] = channel.history(
            limit=HISTORY_LIMIT,
            after=after,
            oldest_first=True,
        )
        async for message in history:
            if message.author.id != user.id or str(message.nonce) != nonce:
                continue
            if message.content != content:
                raise ValueError("Discord setup message conflicts with the expected content")
            return message
        return None

    async def _panel_context(
        self,
        interaction: discord.Interaction[discord.Client],
        panel_id: PanelCustomId,
    ) -> tuple[DebateSnapshot, discord.Message]:
        message = interaction.message
        if (
            interaction.application_id
            != int(self._config.application_id_for(DiscordBotSlot.MODERATOR))
            or interaction.guild_id is None
            or interaction.channel_id is None
            or message is None
        ):
            raise ValueError("panel interaction context is incomplete")
        snapshot = await self._application.get_debate(panel_id.debate_id)
        if (
            str(interaction.guild_id) != snapshot.guild_id
            or snapshot.thread_id != str(interaction.channel_id)
            or snapshot.control_panel_message_id != str(message.id)
        ):
            raise ValueError("panel interaction does not match the persisted context")
        return snapshot, message

    async def _handle_cancel(
        self,
        interaction: discord.Interaction[discord.Client],
        message: discord.Message,
        snapshot: DebateSnapshot,
        expected_attempt: AttemptId,
        can_manage_messages: bool,
        operation_id: str,
    ) -> None:
        result = await self._application.cancel_debate(
            CancelDebateCommand(
                debate_id=snapshot.state.debate_id,
                actor_id=str(interaction.user.id),
                operation_id=operation_id,
                can_manage_messages=can_manage_messages,
                expected_attempt_id=expected_attempt,
            )
        )
        await self._stop_debate(result.debate_id)
        current = await self._application.get_debate(result.debate_id)
        await _edit_panel(message, current)
        await self._edit_response(interaction, "討論を中止しました。")

    async def _handle_retry(
        self,
        interaction: discord.Interaction[discord.Client],
        message: discord.Message,
        snapshot: DebateSnapshot,
        expected_attempt: AttemptId,
        can_manage_messages: bool,
        operation_id: str,
    ) -> None:
        result = await self._application.retry_debate(
            RetryDebateCommand(
                debate_id=snapshot.state.debate_id,
                actor_id=str(interaction.user.id),
                operation_id=operation_id,
                can_manage_messages=can_manage_messages,
                expected_attempt_id=expected_attempt,
            )
        )
        current = await self._application.get_debate(result.debate_id)
        if current.state.attempt_id != result.attempt_id:
            raise ValueError("retry operation is no longer the current attempt")
        await _edit_panel(message, current)
        self._start_debate(result.debate_id)
        await self._edit_response(interaction, "討論を再試行します。")

    async def _finish_accept(
        self,
        interaction: discord.Interaction[discord.Client],
        snapshot: DebateSnapshot,
    ) -> None:
        if snapshot.thread_id is None:
            raise ValueError("accepted debate is missing its thread")
        await self._edit_response(
            interaction,
            f"受付しました。討論スレッド: <#{snapshot.thread_id}>",
        )

    async def _cancel_unbound(
        self,
        interaction: discord.Interaction[discord.Client],
        accepted: AcceptedDebate,
    ) -> None:
        try:
            snapshot = await self._application.get_debate(accepted.debate_id)
            if _has_bound_context(snapshot) or snapshot.state.phase.is_terminal:
                return
            await self._application.cancel_debate(
                CancelDebateCommand(
                    debate_id=accepted.debate_id,
                    actor_id=str(interaction.user.id),
                    operation_id=f"{interaction.id}f",
                    expected_attempt_id=accepted.attempt_id,
                )
            )
        except Exception:
            return

    def _start_debate(self, debate_id: DebateId) -> None:
        current = self._tasks.get(debate_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_and_refresh(debate_id),
            name=f"debate:{debate_id}",
        )
        self._tasks[debate_id] = task
        task.add_done_callback(lambda completed: self._task_done(debate_id, completed))

    async def _run_and_refresh(self, debate_id: DebateId) -> None:
        await self._application.run_debate(debate_id)
        await self._refresh_panel(debate_id)

    async def _refresh_panel(self, debate_id: DebateId) -> None:
        snapshot = await self._application.get_debate(debate_id)
        if snapshot.thread_id is None or snapshot.control_panel_message_id is None:
            return
        channel = self._moderator.get_channel(int(snapshot.thread_id))
        if channel is None:
            channel = await self._moderator.fetch_channel(int(snapshot.thread_id))
        if not isinstance(channel, discord.Thread):
            raise ValueError("persisted panel thread is unavailable")
        message = await channel.fetch_message(int(snapshot.control_panel_message_id))
        await _edit_panel(message, snapshot)

    async def _stop_debate(self, debate_id: DebateId) -> None:
        task = self._tasks.get(debate_id)
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _task_done(self, debate_id: DebateId, task: asyncio.Task[None]) -> None:
        if self._tasks.get(debate_id) is task:
            self._tasks.pop(debate_id, None)
        if not task.cancelled():
            task.exception()

    async def _respond_with_error(
        self,
        interaction: discord.Interaction[discord.Client],
        error: Exception,
    ) -> None:
        await self._edit_response(interaction, _safe_error_message(error))

    @staticmethod
    async def _edit_response(
        interaction: discord.Interaction[discord.Client],
        content: str,
    ) -> None:
        await interaction.edit_original_response(
            content=content,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _command_schema() -> dict[str, object]:
    return {
        "name": COMMAND_NAME,
        "description": COMMAND_DESCRIPTION,
        "type": 1,
        "options": [
            {
                "name": "question",
                "description": QUESTION_DESCRIPTION,
                "type": 3,
                "required": True,
                "min_length": 1,
                "max_length": 1000,
            }
        ],
    }


def _component_custom_id(interaction: discord.Interaction[discord.Client]) -> str | None:
    if interaction.type is not discord.InteractionType.component or interaction.data is None:
        return None
    value = interaction.data.get("custom_id")
    return value if isinstance(value, str) else None


def _has_bound_context(snapshot: DebateSnapshot) -> bool:
    values = (
        snapshot.starter_message_id,
        snapshot.thread_id,
        snapshot.control_panel_message_id,
    )
    if all(value is None for value in values):
        return False
    if any(value is None for value in values):
        raise ValueError("persisted Discord context is partially bound")
    return True


def _starter_content(question: str) -> str:
    return f"**質問**\n{question}\n\n3つの視点で合議を開始します。"


def _panel_content(snapshot: DebateSnapshot) -> str:
    return "\n".join(
        (
            "**操作パネル**",
            f"状態: `{snapshot.state.phase.value}`",
            f"試行: `{snapshot.state.attempt_id}`",
        )
    )


def _panel_view(snapshot: DebateSnapshot) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    cancel_id = PanelCustomId.for_attempt(
        debate_id=snapshot.state.debate_id,
        attempt_id=snapshot.state.attempt_id,
        action=PanelAction.CANCEL,
    ).encode()
    retry_id = PanelCustomId.for_attempt(
        debate_id=snapshot.state.debate_id,
        attempt_id=snapshot.state.attempt_id,
        action=PanelAction.RETRY,
    ).encode()
    view.add_item(
        discord.ui.Button(
            label="中止",
            style=discord.ButtonStyle.danger,
            custom_id=cancel_id,
            disabled=snapshot.state.phase.is_terminal,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="再試行",
            style=discord.ButtonStyle.primary,
            custom_id=retry_id,
            disabled=snapshot.state.phase is not DebatePhase.FAILED,
        )
    )
    return view


async def _edit_panel(message: discord.Message, snapshot: DebateSnapshot) -> None:
    await message.edit(
        content=_panel_content(snapshot),
        allowed_mentions=discord.AllowedMentions.none(),
        view=_panel_view(snapshot),
    )


def _safe_error_message(error: Exception) -> str:
    if isinstance(error, RepositoryBusy):
        return "現在3件の討論を処理中です。完了後にもう一度お試しください。"
    if isinstance(error, RepositoryQuotaExceeded):
        return "本日の受付上限に達しました。"
    if isinstance(error, RepositoryConflict):
        return "状態が更新されました。最新の操作パネルでもう一度お試しください。"
    if isinstance(error, DiscordAdapterError):
        return "Discordへの反映に失敗しました。しばらくしてからお試しください。"
    if isinstance(error, ApplicationError):
        return f"操作を受け付けられませんでした ({error.code})。"
    if isinstance(error, discord.Forbidden):
        return "Discordの必要な権限がありません。管理者へ確認してください。"
    if isinstance(error, discord.HTTPException | OSError):
        return "Discordとの通信に失敗しました。しばらくしてからお試しください。"
    return "操作を完了できませんでした。"
