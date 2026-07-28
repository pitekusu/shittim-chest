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
from shittim_chest.application.scale_to_zero import (
    IngressClaimFence,
    IngressKind,
    IngressRequest,
    IngressStatus,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

IngressDebateCommand = AcceptDebateRequest | CancelDebateCommand | RetryDebateCommand


@dataclass(frozen=True, slots=True)
class AppliedIngressCommand:
    """Uniform identity returned after one existing debate use case succeeds."""

    kind: IngressKind
    debate_id: DebateId
    attempt_id: AttemptId
    terminal_error_code: str | None = None

    def __post_init__(self) -> None:
        if self.terminal_error_code is None:
            return
        if self.kind not in {IngressKind.NEW_DEBATE, IngressKind.RETRY}:
            raise ValueError("only startable ingress may replay a pre-activation failure")
        if not self.terminal_error_code.strip() or len(self.terminal_error_code) > 100:
            raise ValueError("terminal ingress error code must contain at most 100 characters")


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

        ingress_claim = _current_claim_fence(request, claim_owner=claim_owner, at=at)

        command = command_from_ingress(request)
        if isinstance(command, AcceptDebateRequest):
            result = await self._application.accept_debate(command, ingress_claim=ingress_claim)
            return await self._applied(request.kind, result.debate_id, result.attempt_id)
        if isinstance(command, RetryDebateCommand):
            result = await self._application.retry_debate(command, ingress_claim=ingress_claim)
            return await self._applied(request.kind, result.debate_id, result.attempt_id)
        result = await self._application.cancel_debate(command, ingress_claim=ingress_claim)
        return AppliedIngressCommand(request.kind, result.debate_id, result.attempt_id)

    async def abort_pre_activation(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
        *,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> str:
        """Fail one startable attempt before its ingress is durably accepted."""

        if applied.kind is not request.kind or request.kind not in {
            IngressKind.NEW_DEBATE,
            IngressKind.RETRY,
        }:
            raise InvalidApplicationOperation("only matching startable ingress may be aborted")
        ingress_claim = _current_claim_fence(request, claim_owner=claim_owner, at=at)
        return await self._application.fail_pre_activation(
            debate_id=applied.debate_id,
            attempt_id=applied.attempt_id,
            kind=applied.kind,
            ingress_claim=ingress_claim,
            error_code=error_code,
        )

    async def _applied(
        self,
        kind: IngressKind,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> AppliedIngressCommand:
        snapshot = await self._application.get_debate(debate_id)
        if snapshot.state.debate_id != debate_id or snapshot.state.attempt_id != attempt_id:
            raise InvalidApplicationOperation("command result no longer names the current attempt")
        terminal_error_code = None
        if snapshot.state.phase.is_terminal:
            if (
                kind not in {IngressKind.NEW_DEBATE, IngressKind.RETRY}
                or snapshot.state.phase is not DebatePhase.FAILED
                or snapshot.error_code is None
            ):
                raise InvalidApplicationOperation(
                    "command replay reached an unexpected terminal state"
                )
            terminal_error_code = snapshot.error_code
        return AppliedIngressCommand(kind, debate_id, attempt_id, terminal_error_code)


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


def _current_claim_fence(
    request: IngressRequest,
    *,
    claim_owner: str,
    at: datetime,
) -> IngressClaimFence:
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
    return IngressClaimFence.from_claimed_request(
        request,
        claim_owner=claim_owner,
        write_at=at,
    )


__all__ = (
    "AppliedIngressCommand",
    "IngressCommandAdapter",
    "IngressDebateCommand",
    "command_from_ingress",
)
