"""Durable Discord HTTP ingress orchestration without SDK-specific values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum, unique

from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.discord_http import DiscordHttpOperation
from shittim_chest.application.ports import (
    DebateLookup,
    IngressRepository,
    RepositoryIdentityConflict,
    RepositoryQueueFull,
)
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressRequest,
    IngressStatus,
    StatusMessageState,
)
from shittim_chest.domain import DebatePhase

GUILD_TEXT_CHANNEL = 0
GUILD_PUBLIC_THREAD = 11


@unique
class IngressOutcome(StrEnum):
    """Stable, content-free result mapped to one ephemeral initial response."""

    STARTING = "starting"
    ACCEPTED = "accepted"
    PENDING = "pending"
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
    """Validate and durably persist one signed operation before responding."""

    __slots__ = (
        "_debates",
        "_ingress",
        "_moderator_application_id",
        "_runtime",
    )

    def __init__(
        self,
        *,
        runtime_config: DiscordRuntimeConfig,
        ingress: IngressRepository,
        debates: DebateLookup,
    ) -> None:
        self._runtime = runtime_config
        self._moderator_application_id = runtime_config.application_id_for(DiscordBotSlot.MODERATOR)
        self._ingress = ingress
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
        if replay is None:
            request = replace(
                request,
                status_message_state=StatusMessageState.PENDING,
            )
        try:
            enqueued = replay or await self._ingress.enqueue(request)
        except RepositoryQueueFull:
            return IngressAcceptance(IngressOutcome.QUEUE_FULL)
        except RepositoryIdentityConflict:
            return IngressAcceptance(IngressOutcome.NOT_ALLOWED)

        return IngressAcceptance(
            _outcome_for_status(
                enqueued.request.status,
                kind=enqueued.request.kind,
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
) -> IngressOutcome:
    if status in {IngressStatus.PENDING, IngressStatus.CLAIMED, IngressStatus.RETRYING}:
        if kind is IngressKind.RETRY:
            return IngressOutcome.RETRY_ACCEPTED
        if kind is IngressKind.CANCEL:
            return IngressOutcome.CANCEL_ACCEPTED
        return IngressOutcome.PENDING
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
    "DiscordIngressApplication",
    "IngressAcceptance",
    "IngressOutcome",
)
