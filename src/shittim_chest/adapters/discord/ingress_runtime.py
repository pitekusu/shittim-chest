"""Discord context provisioning and task ownership for durable HTTP ingress."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Protocol

import discord

from shittim_chest.adapters.discord.errors import (
    DiscordAdapterError,
    DiscordDeliveryConflict,
    DiscordDeliveryRejected,
    DiscordIdentityUnavailable,
    DiscordPermissionDenied,
    DiscordRateLimited,
    DiscordThreadLocked,
    DiscordThreadUnavailable,
    DiscordUnavailable,
)
from shittim_chest.adapters.discord.gateway import DiscordModeratorClient
from shittim_chest.adapters.discord.interactions import (
    HISTORY_LIMIT,
    _edit_panel,
    _panel_content,
    _panel_view,
)
from shittim_chest.adapters.discord.rate_limit_evidence import (
    discord_rate_limit_operation,
)
from shittim_chest.application.commands import AppliedIngressCommand
from shittim_chest.application.discord import (
    DiscordBotSlot,
    DiscordErrorCode,
    nonce_from_uuid7,
)
from shittim_chest.application.ingress_drain import (
    DiscordIngressOperation,
    IngressRetryableFailure,
    IngressTerminalFailure,
)
from shittim_chest.application.models import (
    BindDiscordContextCommand,
    DebateSnapshot,
    MetricEvent,
)
from shittim_chest.application.ports import (
    Clock,
    Metrics,
    PanelRefreshRepository,
    RepositoryConflict,
    StatusPublicationTrigger,
    StatusTriggerUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressRequest,
)
from shittim_chest.application.status_publication import (
    has_exact_status_publication_marker,
    status_publication_marker,
)
from shittim_chest.domain import AttemptId, DebateId

DEFAULT_SETUP_TIMEOUT_SECONDS = 45.0
DEFAULT_PANEL_REFRESH_RETRY_SECONDS = 30.0
DEFAULT_PANEL_REFRESH_DELIVERY_TIMEOUT_SECONDS = 30.0
MAX_PANEL_REFRESH_RETRY_SECONDS = 300.0
PANEL_REFRESH_COMPLETION_MARGIN_SECONDS = 15.0
PANEL_REFRESH_RECOVERY_LIMIT = 20
_LOGGER = logging.getLogger("shittim_chest")


class _DebateRuntimeApplication(Protocol):
    async def bind_discord_context(
        self,
        command: BindDiscordContextCommand,
    ) -> object: ...

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot: ...

    async def claim_recoverable(self) -> tuple[DebateSnapshot, ...]: ...

    async def run_debate(self, debate_id: DebateId) -> None: ...


class DiscordIngressRuntime:
    """Provision durable Discord context and own every production debate task."""

    def __init__(
        self,
        *,
        clients: Mapping[DiscordBotSlot, discord.Client],
        application: _DebateRuntimeApplication,
        panel_refresh: PanelRefreshRepository,
        clock: Clock,
        metrics: Metrics,
        status_trigger: StatusPublicationTrigger,
        claim_owner: str,
        setup_timeout_seconds: float = DEFAULT_SETUP_TIMEOUT_SECONDS,
        panel_refresh_retry_seconds: float = DEFAULT_PANEL_REFRESH_RETRY_SECONDS,
        panel_refresh_delivery_timeout_seconds: float = (
            DEFAULT_PANEL_REFRESH_DELIVERY_TIMEOUT_SECONDS
        ),
    ) -> None:
        if setup_timeout_seconds <= 0:
            raise ValueError("Discord setup timeout must be positive")
        if panel_refresh_retry_seconds <= 0:
            raise ValueError("panel refresh retry must be positive")
        if panel_refresh_delivery_timeout_seconds <= 0:
            raise ValueError("panel refresh delivery timeout must be positive")
        if not claim_owner.strip():
            raise ValueError("panel refresh claim owner must not be empty")
        moderator = clients.get(DiscordBotSlot.MODERATOR)
        if not isinstance(moderator, DiscordModeratorClient):
            raise ValueError("ingress runtime requires the dedicated moderator client")
        self._moderator = moderator
        self._application = application
        self._panel_refresh = panel_refresh
        self._clock = clock
        self._metrics = metrics
        self._status_trigger = status_trigger
        self._claim_owner = claim_owner
        self._setup_timeout_seconds = setup_timeout_seconds
        self._panel_refresh_retry = timedelta(seconds=panel_refresh_retry_seconds)
        self._panel_refresh_delivery_timeout_seconds = panel_refresh_delivery_timeout_seconds
        self._tasks: dict[DebateId, asyncio.Task[None]] = {}
        self._shutting_down = False

    @property
    def active_task_count(self) -> int:
        """Return process-owned debate tasks for later idle inspection."""

        return sum(not task.done() for task in self._tasks.values())

    def begin_shutdown(self) -> None:
        """Reject new context work and task starts before checkpointing."""

        self._shutting_down = True

    async def preflight(self, request: IngressRequest) -> None:
        """Validate persisted control context before any application mutation."""

        self._ensure_running()
        if request.kind is IngressKind.NEW_DEBATE:
            return
        try:
            if request.target_debate_id is None:
                raise DiscordDeliveryConflict
            snapshot = await self._application.get_debate(request.target_debate_id)
            self._validate_control_context(request, snapshot)
        except DiscordAdapterError as error:
            raise _ingress_failure(error) from error

    async def prepare(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        """Prepare idempotent Discord context before ingress settlement."""

        self._ensure_running()
        try:
            snapshot = await self._validated_snapshot(request, applied)
            if request.kind is not IngressKind.NEW_DEBATE:
                self._validate_control_context(request, snapshot)
                return
            async with asyncio.timeout(self._setup_timeout_seconds):
                await self._prepare_new_context(request, snapshot)
        except asyncio.CancelledError:
            raise
        except DiscordAdapterError as error:
            raise _ingress_failure(error) from error
        except TimeoutError as error:
            raise _ingress_failure(DiscordUnavailable()) from error
        except discord.RateLimited as error:
            raise IngressRetryableFailure(
                DiscordRateLimited().code,
                retry_after_seconds=max(1.0, error.retry_after),
            ) from error
        except discord.Forbidden as error:
            raise _ingress_failure(DiscordPermissionDenied()) from error
        except discord.NotFound as error:
            raise _ingress_failure(DiscordUnavailable()) from error
        except discord.HTTPException as error:
            if error.status == 429:
                raise IngressRetryableFailure(
                    DiscordRateLimited().code,
                    retry_after_seconds=_discord_retry_after(error),
                ) from error
            if error.status in {408, 409} or error.status >= 500:
                raise _ingress_failure(DiscordUnavailable()) from error
            raise _ingress_failure(DiscordDeliveryRejected()) from error
        except OSError as error:
            raise _ingress_failure(DiscordUnavailable()) from error

    async def activate(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        """Start or stop process work only after claim-fenced ingress settlement."""

        self._ensure_running()
        snapshot = await self._validated_snapshot(request, applied)
        if request.kind is IngressKind.CANCEL:
            await self._stop_debate(applied.debate_id)
            self._start_debate(snapshot.state.debate_id)
            return
        if request.kind is IngressKind.RETRY and not await self._converge_panel(
            snapshot.state.debate_id,
            snapshot.state.attempt_id,
        ):
            return
        self._start_debate(applied.debate_id)

    async def recover_once(self) -> int:
        """Claim recoverable bound debates and start them in the shared registry."""

        self._ensure_running()
        for _attempt in range(PANEL_REFRESH_RECOVERY_LIMIT):
            claim = await self._panel_refresh.claim_next_due_panel_refresh(
                claim_owner=self._claim_owner,
                at=self._clock.now(),
            )
            if claim is None:
                break
            await self._deliver_panel_claim(claim)
        snapshots = await self._application.claim_recoverable()
        started = 0
        for snapshot in snapshots:
            if not _has_bound_context(snapshot):
                continue
            if self._start_debate(snapshot.state.debate_id):
                started += 1
        return started

    async def checkpoint_active(self) -> None:
        """Cancel all owned tasks and await application-level checkpoints."""

        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise RuntimeError("a debate task failed during checkpoint") from result

    async def close(self) -> None:
        """Permanently stop task creation and checkpoint active debates."""

        self.begin_shutdown()
        await self.checkpoint_active()

    async def _validated_snapshot(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> DebateSnapshot:
        snapshot = await self._application.get_debate(applied.debate_id)
        if (
            snapshot.state.debate_id != applied.debate_id
            or snapshot.state.attempt_id != applied.attempt_id
            or snapshot.guild_id != request.guild_id
        ):
            raise DiscordDeliveryConflict
        return snapshot

    async def _prepare_new_context(
        self,
        request: IngressRequest,
        snapshot: DebateSnapshot,
    ) -> None:
        if request.status_message_id is None:
            raise DiscordUnavailable
        bound = _bound_context_state(snapshot)
        if bound is _BoundContext.PARTIAL:
            raise DiscordDeliveryConflict
        if bound is _BoundContext.COMPLETE:
            if snapshot.starter_message_id != request.status_message_id:
                raise DiscordDeliveryConflict
            return

        channel = await self._resolve_status_channel(request)
        starter = await _await_discord_operation(
            DiscordIngressOperation.STATUS_MESSAGE_FETCH,
            channel.fetch_message(int(request.status_message_id)),
        )
        self._validate_status_message(request, channel, starter)
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

    async def _resolve_status_channel(self, request: IngressRequest) -> discord.TextChannel:
        channel = self._moderator.get_channel(int(request.status_channel_id))
        if channel is None:
            channel = await _await_discord_operation(
                DiscordIngressOperation.STATUS_CHANNEL_FETCH,
                self._moderator.fetch_channel(int(request.status_channel_id)),
            )
        if (
            not isinstance(channel, discord.TextChannel)
            or str(channel.id) != request.status_channel_id
            or str(channel.guild.id) != request.guild_id
        ):
            raise DiscordThreadUnavailable
        return channel

    def _validate_status_message(
        self,
        request: IngressRequest,
        channel: discord.TextChannel,
        message: discord.Message,
    ) -> None:
        user = self._moderator.user
        if user is None:
            raise DiscordIdentityUnavailable
        if (
            request.status_message_id is None
            or str(message.id) != request.status_message_id
            or message.channel.id != channel.id
            or message.author.id != user.id
            or not has_exact_status_publication_marker(
                message.content,
                status_publication_marker(request.interaction_id),
            )
        ):
            raise DiscordDeliveryConflict

    async def _resolve_or_create_thread(
        self,
        starter: discord.Message,
        snapshot: DebateSnapshot,
    ) -> discord.Thread:
        thread = starter.thread
        if thread is None:
            cached = self._moderator.get_channel(starter.id)
            thread = cached if isinstance(cached, discord.Thread) else None
        if thread is None:
            try:
                fetched = await _await_discord_operation(
                    DiscordIngressOperation.THREAD_LOOKUP,
                    self._moderator.fetch_channel(starter.id),
                )
            except discord.NotFound:
                fetched = None
            thread = fetched if isinstance(fetched, discord.Thread) else None
        if thread is None:
            thread = await _await_discord_operation(
                DiscordIngressOperation.THREAD_CREATE,
                starter.create_thread(
                    name=f"Shittim {str(snapshot.state.debate_id)[:8]}",
                    auto_archive_duration=1440,
                ),
            )
        if str(thread.guild.id) != snapshot.guild_id:
            raise DiscordThreadUnavailable
        if thread.locked:
            raise DiscordThreadLocked
        return thread

    async def _resolve_or_create_panel(
        self,
        thread: discord.Thread,
        snapshot: DebateSnapshot,
    ) -> discord.Message:
        content = _panel_content(snapshot)
        nonce = nonce_from_uuid7(snapshot.state.attempt_id.value)
        panel = await _await_discord_operation(
            DiscordIngressOperation.PANEL_LOOKUP,
            self._find_message(
                thread,
                nonce=nonce,
                content=content,
                after=snapshot.attempt_created_at,
            ),
        )
        if panel is not None:
            return panel
        return await _await_discord_operation(
            DiscordIngressOperation.PANEL_CREATE,
            thread.send(
                content,
                nonce=nonce,
                allowed_mentions=discord.AllowedMentions.none(),
                view=_panel_view(snapshot),
            ),
        )

    async def _find_message(
        self,
        channel: discord.Thread,
        *,
        nonce: str,
        content: str,
        after: datetime,
    ) -> discord.Message | None:
        user = self._moderator.user
        if user is None:
            raise DiscordIdentityUnavailable
        history: AsyncIterator[discord.Message] = channel.history(
            limit=HISTORY_LIMIT,
            after=after,
            oldest_first=True,
        )
        async for message in history:
            if message.author.id != user.id or str(message.nonce) != nonce:
                continue
            if message.content != content:
                raise DiscordDeliveryConflict
            return message
        return None

    def _validate_control_context(
        self,
        request: IngressRequest,
        snapshot: DebateSnapshot,
    ) -> None:
        if (
            request.target_debate_id != snapshot.state.debate_id
            or request.guild_id != snapshot.guild_id
            or request.parent_channel_id != snapshot.channel_id
            or request.source_thread_id != snapshot.thread_id
            or request.source_message_id != snapshot.control_panel_message_id
        ):
            raise DiscordDeliveryConflict

    def _start_debate(self, debate_id: DebateId) -> bool:
        current = self._tasks.get(debate_id)
        if current is not None and not current.done():
            return False
        if self._shutting_down:
            return False
        task = asyncio.create_task(
            self._run_and_refresh(debate_id),
            name=f"debate:{debate_id}",
        )
        self._tasks[debate_id] = task
        task.add_done_callback(lambda completed: self._task_done(debate_id, completed))
        return True

    async def _run_and_refresh(self, debate_id: DebateId) -> None:
        await self._application.run_debate(debate_id)
        snapshot = await self._application.get_debate(debate_id)
        if snapshot.state.phase.is_terminal and snapshot.origin_ingress_interaction_id is not None:
            with suppress(StatusTriggerUnavailable):
                await self._status_trigger.request_publication(
                    snapshot.origin_ingress_interaction_id
                )
        await self._converge_panel(debate_id, snapshot.state.attempt_id)

    async def _converge_panel(self, debate_id: DebateId, attempt_id: AttemptId) -> bool:
        claim = await self._panel_refresh.claim_panel_refresh(
            debate_id=debate_id,
            attempt_id=attempt_id,
            claim_owner=self._claim_owner,
            at=self._clock.now(),
        )
        if claim is None:
            snapshot = await self._application.get_debate(debate_id)
            return not snapshot.panel_refresh_pending
        return await self._deliver_panel_claim(claim)

    async def _deliver_panel_claim(self, snapshot: DebateSnapshot) -> bool:
        delivery_timeout = self._remaining_panel_delivery_timeout(snapshot)
        if delivery_timeout <= 0:
            return False
        try:
            async with asyncio.timeout(delivery_timeout):
                await self._refresh_panel(snapshot)
        except asyncio.CancelledError:
            raise
        except (
            DiscordAdapterError,
            TimeoutError,
            discord.RateLimited,
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
            OSError,
        ) as error:
            return await self._settle_panel_delivery_failure(snapshot, error)
        try:
            await self._panel_refresh.complete_panel_refresh(
                expected=snapshot,
                claim_owner=self._claim_owner,
                at=self._clock.now(),
            )
            return True
        except asyncio.CancelledError:
            raise
        except RepositoryConflict:
            return False

    async def _settle_panel_delivery_failure(
        self,
        snapshot: DebateSnapshot,
        error: Exception,
    ) -> bool:
        retryable, error_code = _panel_failure_disposition(error)
        at = self._clock.now()
        try:
            if retryable:
                await self._panel_refresh.reschedule_panel_refresh(
                    expected=snapshot,
                    claim_owner=self._claim_owner,
                    at=at,
                    next_attempt_at=at + self._panel_retry_delay(snapshot),
                )
            else:
                abandoned = await self._panel_refresh.abandon_panel_refresh(
                    expected=snapshot,
                    claim_owner=self._claim_owner,
                    at=at,
                    error_code=error_code,
                )
                self._metrics.increment(
                    MetricEvent.PANEL_REFRESH_FAILED,
                    debate_id=abandoned.state.debate_id,
                )
        except RepositoryConflict:
            return False
        return False

    def _panel_retry_delay(self, snapshot: DebateSnapshot) -> timedelta:
        """Return capped exponential backoff for a transient delivery failure."""

        exponent = min(max(snapshot.panel_refresh_delivery_attempt - 1, 0), 16)
        seconds = min(
            self._panel_refresh_retry.total_seconds() * (2**exponent),
            MAX_PANEL_REFRESH_RETRY_SECONDS,
        )
        return timedelta(seconds=seconds)

    def _remaining_panel_delivery_timeout(self, snapshot: DebateSnapshot) -> float:
        claim_expiry = snapshot.panel_refresh_claim_expires_at
        if claim_expiry is None:
            return 0.0
        remaining = (
            claim_expiry - self._clock.now()
        ).total_seconds() - PANEL_REFRESH_COMPLETION_MARGIN_SECONDS
        return min(self._panel_refresh_delivery_timeout_seconds, max(0.0, remaining))

    async def _refresh_panel(self, snapshot: DebateSnapshot) -> None:
        if snapshot.thread_id is None or snapshot.control_panel_message_id is None:
            raise DiscordDeliveryConflict
        channel = self._moderator.get_channel(int(snapshot.thread_id))
        if channel is None:
            channel = await self._moderator.fetch_channel(int(snapshot.thread_id))
        if not isinstance(channel, discord.Thread) or str(channel.guild.id) != snapshot.guild_id:
            raise DiscordThreadUnavailable
        message = await channel.fetch_message(int(snapshot.control_panel_message_id))
        user = self._moderator.user
        if (
            user is None
            or str(message.id) != snapshot.control_panel_message_id
            or message.author.id != user.id
        ):
            raise DiscordDeliveryConflict
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
            error = task.exception()
            if error is not None:
                _LOGGER.error(
                    json.dumps(
                        {
                            "severity": "ERROR",
                            "event": "debate_task_failed",
                            "debate_id": str(debate_id),
                            "error_type": type(error).__name__,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )

    def _ensure_running(self) -> None:
        if self._shutting_down:
            from shittim_chest.application.errors import RuntimeNotReady

            raise RuntimeNotReady("the Discord runtime is shutting down")


class _BoundContext:
    NONE = 0
    COMPLETE = 1
    PARTIAL = 2


def _bound_context_state(snapshot: DebateSnapshot) -> int:
    values = (
        snapshot.starter_message_id,
        snapshot.thread_id,
        snapshot.control_panel_message_id,
    )
    present = sum(value is not None for value in values)
    if present == 0:
        return _BoundContext.NONE
    if present == len(values):
        return _BoundContext.COMPLETE
    return _BoundContext.PARTIAL


def _has_bound_context(snapshot: DebateSnapshot) -> bool:
    return _bound_context_state(snapshot) == _BoundContext.COMPLETE


def _ingress_failure(
    error: DiscordAdapterError,
) -> IngressRetryableFailure | IngressTerminalFailure:
    if error.retryable:
        return IngressRetryableFailure(error.code)
    return IngressTerminalFailure(error.code)


async def _await_discord_operation[T](
    operation: DiscordIngressOperation,
    awaitable: Awaitable[T],
) -> T:
    """Attach one safe operation label only to Discord rate-limit retries."""

    with discord_rate_limit_operation(operation):
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except discord.RateLimited as error:
            raise IngressRetryableFailure(
                DiscordRateLimited().code,
                retry_after_seconds=max(1.0, error.retry_after),
                discord_operation=operation,
            ) from error
        except discord.HTTPException as error:
            if error.status != 429:
                raise
            raise IngressRetryableFailure(
                DiscordRateLimited().code,
                retry_after_seconds=_discord_retry_after(error),
                discord_operation=operation,
            ) from error


def _discord_retry_after(error: discord.HTTPException) -> float:
    raw = error.response.headers.get("Retry-After")
    if isinstance(raw, bool) or not isinstance(raw, str | int | float):
        return DEFAULT_PANEL_REFRESH_RETRY_SECONDS
    try:
        parsed = float(raw)
    except TypeError, ValueError:
        return DEFAULT_PANEL_REFRESH_RETRY_SECONDS
    if not math.isfinite(parsed) or parsed <= 0:
        return DEFAULT_PANEL_REFRESH_RETRY_SECONDS
    return max(1.0, parsed)


def _panel_failure_disposition(error: Exception) -> tuple[bool, str]:
    if isinstance(error, DiscordAdapterError):
        return error.retryable, error.code
    if isinstance(error, discord.RateLimited):
        return True, DiscordErrorCode.RATE_LIMITED.value
    if isinstance(error, TimeoutError | OSError):
        return True, DiscordErrorCode.UNAVAILABLE.value
    if isinstance(error, discord.Forbidden):
        return False, DiscordErrorCode.PERMISSION_DENIED.value
    if isinstance(error, discord.NotFound):
        return False, DiscordErrorCode.THREAD_UNAVAILABLE.value
    if isinstance(error, discord.HTTPException):
        if error.status == 429:
            return True, DiscordErrorCode.RATE_LIMITED.value
        if error.status in {408, 409} or error.status >= 500:
            return True, DiscordErrorCode.UNAVAILABLE.value
        return False, DiscordErrorCode.DELIVERY_REJECTED.value
    raise TypeError("unsupported panel delivery failure")


__all__ = (
    "DEFAULT_PANEL_REFRESH_DELIVERY_TIMEOUT_SECONDS",
    "DEFAULT_PANEL_REFRESH_RETRY_SECONDS",
    "DEFAULT_SETUP_TIMEOUT_SECONDS",
    "DiscordIngressRuntime",
)
