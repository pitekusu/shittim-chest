"""Durable Discord HTTP ingress orchestration without SDK-specific values."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum, unique

from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.discord_http import DiscordHttpOperation
from shittim_chest.application.ports import (
    Clock,
    DebateLookup,
    IngressRepository,
    ReconciliationTriggerUnavailable,
    RepositoryConflict,
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
    IngressKind,
    IngressRequest,
    IngressStatus,
    RuntimeStatus,
)
from shittim_chest.domain import DebatePhase

GUILD_TEXT_CHANNEL = 0
GUILD_PUBLIC_THREAD = 11
POST_PERSISTENCE_ACCELERATOR_BUDGET = timedelta(milliseconds=1500)


@unique
class IngressOutcome(StrEnum):
    """Stable, content-free result mapped to one ephemeral initial response."""

    STARTING = "starting"
    ACCEPTED = "accepted"
    RETRY_STARTING = "retry_starting"
    RETRY_ACCEPTED = "retry_accepted"
    CANCEL_STARTING = "cancel_starting"
    CANCEL_ACCEPTED = "cancel_accepted"
    COMPLETED = "completed"
    REJECTED = "rejected"
    TERMINAL_FAILED = "terminal_failed"
    QUEUE_FULL = "queue_full"
    NOT_ALLOWED = "not_allowed"


@dataclass(frozen=True, slots=True)
class IngressAcceptance:
    """Result of the durable acceptance gate without request content or SDK objects."""

    outcome: IngressOutcome
    created: bool = False


class DiscordIngressApplication:
    """Validate, persist, then best-effort accelerate one signed operation."""

    __slots__ = (
        "_clock",
        "_debates",
        "_ingress",
        "_moderator_application_id",
        "_reconciler_trigger",
        "_runtime",
        "_runtime_state",
        "_status_trigger",
    )

    def __init__(
        self,
        *,
        runtime_config: DiscordRuntimeConfig,
        clock: Clock,
        ingress: IngressRepository,
        runtime_state: RuntimeStateRepository,
        status_trigger: StatusPublicationTrigger,
        reconciler_trigger: RuntimeReconciliationTrigger,
        debates: DebateLookup,
    ) -> None:
        self._runtime = runtime_config
        self._clock = clock
        self._moderator_application_id = runtime_config.application_id_for(DiscordBotSlot.MODERATOR)
        self._ingress = ingress
        self._runtime_state = runtime_state
        self._status_trigger = status_trigger
        self._reconciler_trigger = reconciler_trigger
        self._debates = debates

    async def accept(self, operation: DiscordHttpOperation) -> IngressAcceptance:
        """Persist before all wake effects and return only deterministic public state."""

        if not self._boundary_allows(operation):
            return IngressAcceptance(IngressOutcome.NOT_ALLOWED)
        request = _request_from_operation(operation)
        replay = None
        if operation.kind is not IngressKind.NEW_DEBATE:
            try:
                replay = await self._ingress.get_replay(request)
            except RepositoryIdentityConflict:
                return IngressAcceptance(IngressOutcome.NOT_ALLOWED)
        if (
            replay is None
            and operation.kind is not IngressKind.NEW_DEBATE
            and not await self._component_is_authorized(operation)
        ):
            return IngressAcceptance(IngressOutcome.NOT_ALLOWED)
        try:
            enqueued = replay or await self._ingress.enqueue(request)
        except RepositoryQueueFull:
            return IngressAcceptance(IngressOutcome.QUEUE_FULL)
        except RepositoryIdentityConflict:
            return IngressAcceptance(IngressOutcome.NOT_ALLOWED)

        runtime_status = await self._accelerate(enqueued, at=operation.received_at)
        return IngressAcceptance(
            _outcome_for_status(
                enqueued.request.status,
                kind=enqueued.request.kind,
                runtime_status=runtime_status,
            ),
            created=enqueued.created,
        )

    def _boundary_allows(self, operation: DiscordHttpOperation) -> bool:
        if (
            operation.application_id != self._moderator_application_id
            or operation.guild_id != self._runtime.guild_id
        ):
            return False
        if operation.kind is IngressKind.NEW_DEBATE:
            return (
                operation.channel_type == GUILD_TEXT_CHANNEL
                and operation.channel_id in self._runtime.allowed_channel_ids
            )
        return (
            operation.channel_type == GUILD_PUBLIC_THREAD
            and operation.parent_channel_id in self._runtime.allowed_channel_ids
            and operation.source_thread_id == operation.channel_id
        )

    async def _component_is_authorized(self, operation: DiscordHttpOperation) -> bool:
        debate_id = operation.debate_id
        expected_attempt_id = operation.expected_attempt_id
        source_message_id = operation.source_message_id
        if debate_id is None or expected_attempt_id is None or source_message_id is None:
            return False
        snapshot = await self._debates.get(debate_id, expected_attempt_id)
        if snapshot is None:
            return False
        if (
            snapshot.guild_id != operation.guild_id
            or snapshot.channel_id != operation.parent_channel_id
            or snapshot.thread_id != operation.channel_id
            or snapshot.control_panel_message_id != source_message_id
            or snapshot.attempt_id != expected_attempt_id
        ):
            return False
        if snapshot.requester_id != operation.requester_id and not operation.can_manage_messages:
            return False
        if operation.kind is IngressKind.RETRY:
            return snapshot.phase is DebatePhase.FAILED
        if operation.kind is IngressKind.CANCEL:
            return not snapshot.phase.is_terminal
        return False

    async def _accelerate(
        self,
        enqueued: EnqueuedIngress,
        *,
        at: datetime,
    ) -> RuntimeStatus | None:
        request = enqueued.request
        if not self._has_accelerator_budget(at):
            return None
        if not request.status.counts_toward_queue_limit:
            await self._kick_status(request.interaction_id)
            return None
        runtime_status, _, _ = await asyncio.gather(
            self._read_runtime_status(),
            self._kick_status(request.interaction_id),
            self._kick_reconciler(request.interaction_id),
        )
        return runtime_status

    async def _read_runtime_status(self) -> RuntimeStatus | None:
        try:
            state = await self._runtime_state.get()
        except RepositoryConflict, RepositoryUnavailable:
            return None
        return None if state is None else state.status

    async def _kick_status(self, interaction_id: str) -> None:
        with suppress(StatusTriggerUnavailable):
            await self._status_trigger.request_publication(interaction_id)

    async def _kick_reconciler(self, interaction_id: str) -> None:
        with suppress(ReconciliationTriggerUnavailable):
            await self._reconciler_trigger.request_reconciliation(interaction_id)

    def _has_accelerator_budget(self, received_at: datetime) -> bool:
        return self._clock.now() - received_at < POST_PERSISTENCE_ACCELERATOR_BUDGET


def _request_from_operation(operation: DiscordHttpOperation) -> IngressRequest:
    if operation.kind is IngressKind.NEW_DEBATE:
        if operation.question is None or operation.command_name is None:
            raise ValueError("validated debate operation lost required command input")
        return IngressRequest.new_debate(
            interaction_id=operation.interaction_id,
            operation_id=operation.operation_id,
            application_id=operation.application_id,
            question=operation.question,
            requester_id=operation.requester_id,
            requester_username=operation.requester_username,
            requester_display_name=operation.requester_display_name,
            guild_id=operation.guild_id,
            channel_id=operation.channel_id,
            command_name=operation.command_name,
            created_at=operation.received_at,
        )
    if (
        operation.custom_id is None
        or operation.source_message_id is None
        or operation.source_thread_id is None
        or operation.parent_channel_id is None
        or operation.debate_id is None
        or operation.expected_attempt_id is None
    ):
        raise ValueError("validated component operation lost required context")
    return IngressRequest.control_operation(
        interaction_id=operation.interaction_id,
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


def _outcome_for_status(
    status: IngressStatus,
    *,
    kind: IngressKind,
    runtime_status: RuntimeStatus | None,
) -> IngressOutcome:
    if status in {IngressStatus.PENDING, IngressStatus.CLAIMED, IngressStatus.RETRYING}:
        ready = runtime_status in {RuntimeStatus.READY, RuntimeStatus.BUSY}
        if kind is IngressKind.RETRY:
            return IngressOutcome.RETRY_ACCEPTED if ready else IngressOutcome.RETRY_STARTING
        if kind is IngressKind.CANCEL:
            return IngressOutcome.CANCEL_ACCEPTED if ready else IngressOutcome.CANCEL_STARTING
        return IngressOutcome.ACCEPTED if ready else IngressOutcome.STARTING
    if status is IngressStatus.ACCEPTED:
        if kind is IngressKind.RETRY:
            return IngressOutcome.RETRY_ACCEPTED
        if kind is IngressKind.CANCEL:
            return IngressOutcome.CANCEL_ACCEPTED
        return IngressOutcome.ACCEPTED
    if status is IngressStatus.COMPLETED:
        return IngressOutcome.COMPLETED
    if status is IngressStatus.REJECTED:
        return IngressOutcome.REJECTED
    if status is IngressStatus.FAILED:
        return IngressOutcome.TERMINAL_FAILED
    raise AssertionError("unsupported ingress status")


__all__ = (
    "GUILD_PUBLIC_THREAD",
    "GUILD_TEXT_CHANNEL",
    "POST_PERSISTENCE_ACCELERATOR_BUDGET",
    "DiscordIngressApplication",
    "IngressAcceptance",
    "IngressOutcome",
)
