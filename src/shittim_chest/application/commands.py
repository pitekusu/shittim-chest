"""Apply durable Discord ingress through the existing debate use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from shittim_chest.application.errors import InvalidApplicationOperation
from shittim_chest.application.models import (
    AcceptDebateRequest,
    CancelDebateCommand,
    RetryDebateCommand,
)
from shittim_chest.application.ports import DebateCommandUseCases, RepositoryConflict
from shittim_chest.application.scale_to_zero import IngressKind, IngressRequest, IngressStatus
from shittim_chest.domain import AttemptId, DebateId

IngressDebateCommand = AcceptDebateRequest | CancelDebateCommand | RetryDebateCommand


@dataclass(frozen=True, slots=True)
class AppliedIngressCommand:
    """Uniform identity returned after one existing debate use case succeeds."""

    kind: IngressKind
    debate_id: DebateId
    attempt_id: AttemptId


class IngressCommandAdapter:
    """Bridge claimed, token-free ingress to the existing SDK-free use cases."""

    __slots__ = ("_application",)

    def __init__(self, application: DebateCommandUseCases) -> None:
        self._application = application

    async def apply(
        self,
        request: IngressRequest,
        *,
        claim_owner: str,
        at: datetime,
    ) -> AppliedIngressCommand:
        """Apply exactly one claimed request without settling its ingress record."""

        _require_current_claim(request, claim_owner=claim_owner, at=at)

        command = command_from_ingress(request)
        if isinstance(command, AcceptDebateRequest):
            result = await self._application.accept_debate(command)
            return AppliedIngressCommand(request.kind, result.debate_id, result.attempt_id)
        if isinstance(command, RetryDebateCommand):
            result = await self._application.retry_debate(command)
            return AppliedIngressCommand(request.kind, result.debate_id, result.attempt_id)
        result = await self._application.cancel_debate(command)
        return AppliedIngressCommand(request.kind, result.debate_id, result.attempt_id)


def command_from_ingress(request: IngressRequest) -> IngressDebateCommand:
    """Convert one durable request without importing Discord or AWS SDK values."""

    if request.kind is IngressKind.NEW_DEBATE:
        if request.question is None:
            raise InvalidApplicationOperation("new debate ingress is missing its question")
        return AcceptDebateRequest(
            question=request.question,
            requester_id=request.requester_id,
            requester_username=request.requester_username,
            requester_display_name=request.requester_display_name,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            operation_id=request.operation_id,
        )

    _validate_control_source(request)
    if request.target_debate_id is None or request.expected_attempt_id is None:
        raise InvalidApplicationOperation("control ingress is missing its target identity")
    if request.kind is IngressKind.RETRY:
        return RetryDebateCommand(
            debate_id=request.target_debate_id,
            actor_id=request.requester_id,
            operation_id=request.operation_id,
            can_manage_messages=request.requester_can_manage_messages,
            expected_attempt_id=request.expected_attempt_id,
        )
    if request.kind is IngressKind.CANCEL:
        return CancelDebateCommand(
            debate_id=request.target_debate_id,
            actor_id=request.requester_id,
            operation_id=request.operation_id,
            can_manage_messages=request.requester_can_manage_messages,
            expected_attempt_id=request.expected_attempt_id,
        )
    raise InvalidApplicationOperation("unsupported ingress command kind")


def _validate_control_source(request: IngressRequest) -> None:
    """Fail closed if persisted source context no longer has its canonical shape."""

    if (
        request.parent_channel_id is None
        or request.source_message_id is None
        or request.source_thread_id is None
        or request.channel_id != request.source_thread_id
        or request.status_channel_id != request.source_thread_id
    ):
        raise InvalidApplicationOperation("control ingress source context is inconsistent")


def _require_current_claim(
    request: IngressRequest,
    *,
    claim_owner: str,
    at: datetime,
) -> None:
    """Reject a foreign or expired claim before an existing use case is invoked."""

    if not claim_owner.strip():
        raise ValueError("claim owner must not be empty")
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError("claim validation timestamp must be timezone-aware UTC")
    if (
        request.status is not IngressStatus.CLAIMED
        or request.claim_owner != claim_owner
        or request.claim_expires_at is None
        or request.claim_expires_at <= at
    ):
        raise RepositoryConflict("ingress claim is no longer owned by this runtime")


__all__ = (
    "AppliedIngressCommand",
    "IngressCommandAdapter",
    "IngressDebateCommand",
    "command_from_ingress",
)
