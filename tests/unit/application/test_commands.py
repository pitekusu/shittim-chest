"""Tests for the token-free ingress to existing-use-case boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from shittim_chest.application import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    AppliedIngressCommand,
    CancelDebateCommand,
    CancelledDebate,
    DebateSnapshot,
    IngressClaimFence,
    IngressCommandAdapter,
    IngressKind,
    IngressRequest,
    IngressStatus,
    RetryDebateCommand,
    command_from_ingress,
)
from shittim_chest.application.errors import InvalidApplicationOperation
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import AttemptId, DebateId, DebatePhase, DebateState, RecoveryState

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FakeUseCases:
    debate_id: DebateId = field(default_factory=DebateId.new)
    attempt_id: AttemptId = field(default_factory=AttemptId.new)
    commands: list[AcceptDebateRequest | CancelDebateCommand | RetryDebateCommand] = field(
        default_factory=list
    )
    claims: list[IngressClaimFence | None] = field(default_factory=list)
    terminal_error_code: str | None = None
    snapshot_debate_id: DebateId | None = None
    snapshot_attempt_id: AttemptId | None = None
    snapshot_phase: DebatePhase | None = None
    aborts: list[tuple[IngressKind, str]] = field(default_factory=list)

    async def accept_debate(
        self,
        request: AcceptDebateRequest,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedDebate:
        self.commands.append(request)
        self.claims.append(ingress_claim)
        return AcceptedDebate(self.debate_id, self.attempt_id)

    async def cancel_debate(
        self,
        command: CancelDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> CancelledDebate:
        self.commands.append(command)
        self.claims.append(ingress_claim)
        return CancelledDebate(self.debate_id, self.attempt_id)

    async def retry_debate(
        self,
        command: RetryDebateCommand,
        *,
        ingress_claim: IngressClaimFence | None = None,
    ) -> AcceptedRetry:
        self.commands.append(command)
        self.claims.append(ingress_claim)
        if command.expected_attempt_id is None:
            raise AssertionError("test retry command must preserve its source attempt")
        return AcceptedRetry(self.debate_id, self.attempt_id, command.expected_attempt_id)

    async def get_debate(self, debate_id: DebateId) -> DebateSnapshot:
        assert debate_id == self.debate_id
        command = self.commands[-1]
        retry_of = command.expected_attempt_id if isinstance(command, RetryDebateCommand) else None
        phase = self.snapshot_phase or (
            DebatePhase.FAILED if self.terminal_error_code is not None else DebatePhase.ACCEPTED
        )
        state = DebateState(
            debate_id=self.snapshot_debate_id or self.debate_id,
            attempt_id=self.snapshot_attempt_id or self.attempt_id,
            phase=phase,
            recovery_state=RecoveryState.NONE,
            updated_at=NOW,
            failed_from_phase=(DebatePhase.ACCEPTED if phase is DebatePhase.FAILED else None),
            retry_of=retry_of,
        )
        return DebateSnapshot(
            state=state,
            question="question",
            requester_id="requester-id",
            requester_username="username",
            requester_display_name="display name",
            guild_id="guild-id",
            channel_id="channel-id",
            created_at=NOW,
            attempt_created_at=NOW,
            error_code=self.terminal_error_code,
        )

    async def fail_pre_activation(
        self,
        *,
        debate_id: DebateId,
        attempt_id: AttemptId,
        kind: IngressKind,
        ingress_claim: IngressClaimFence,
        error_code: str,
    ) -> str:
        assert debate_id == self.debate_id
        assert attempt_id == self.attempt_id
        assert ingress_claim.kind is kind
        self.aborts.append((kind, error_code))
        self.terminal_error_code = error_code
        return error_code


def new_request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="interaction-new",
        operation_id="operation-new",
        application_id="application-id",
        question="今日の朝ごはんは何がいい? 甘いものが食べたい",
        requester_id="requester-id",
        requester_username="display-only-username",
        requester_display_name="表示専用名",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW,
    )


def control_request(kind: IngressKind) -> IngressRequest:
    return IngressRequest.control_operation(
        interaction_id=f"interaction-{kind.value}",
        operation_id=f"operation-{kind.value}",
        kind=kind,
        application_id="application-id",
        requester_id="requester-id",
        requester_username="display-only-username",
        requester_display_name="表示専用名",
        requester_can_manage_messages=False,
        guild_id="guild-id",
        channel_id="thread-id",
        parent_channel_id="channel-id",
        source_message_id="panel-message-id",
        source_thread_id="thread-id",
        target_debate_id=DebateId.new(),
        expected_attempt_id=AttemptId.new(),
        custom_id=f"shittim:v1:{kind.value}:operation",
        created_at=NOW,
    )


def claimed(request: IngressRequest) -> IngressRequest:
    return replace(
        request,
        status=IngressStatus.CLAIMED,
        claim_owner="runtime-instance",
        claim_expires_at=NOW + timedelta(minutes=1),
        delivery_attempt=1,
    )


def test_new_debate_conversion_preserves_display_metadata_and_operation_id() -> None:
    command = command_from_ingress(new_request())

    assert isinstance(command, AcceptDebateRequest)
    assert command.operation_id == "operation-new"
    assert command.requester_id == "requester-id"
    assert command.requester_username == "display-only-username"
    assert command.requester_display_name == "表示専用名"


@pytest.mark.parametrize(
    ("kind", "error_code", "message"),
    [
        (IngressKind.CANCEL, "failed", "only startable"),
        (IngressKind.NEW_DEBATE, " ", "at most 100"),
        (IngressKind.RETRY, "x" * 101, "at most 100"),
    ],
)
def test_applied_ingress_rejects_invalid_terminal_error_binding(
    kind: IngressKind,
    error_code: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AppliedIngressCommand(kind, DebateId.new(), AttemptId.new(), error_code)


@pytest.mark.parametrize(
    ("kind", "command_type"),
    [
        (IngressKind.RETRY, RetryDebateCommand),
        (IngressKind.CANCEL, CancelDebateCommand),
    ],
)
def test_control_conversion_uses_requester_id_and_never_display_names(
    kind: IngressKind,
    command_type: type[RetryDebateCommand] | type[CancelDebateCommand],
) -> None:
    request = control_request(kind)
    command = command_from_ingress(request)

    assert isinstance(command, command_type)
    assert command.actor_id == request.requester_id
    assert command.operation_id == request.operation_id
    assert command.expected_attempt_id == request.expected_attempt_id
    assert not hasattr(command, "requester_username")
    assert not hasattr(command, "requester_display_name")


def test_control_conversion_fails_closed_on_inconsistent_source_context() -> None:
    with pytest.raises(InvalidApplicationOperation, match="source context"):
        command_from_ingress(replace(control_request(IngressKind.CANCEL), channel_id="other"))


@pytest.mark.parametrize(
    "changes",
    [
        {"parent_channel_id": None},
        {"source_message_id": None},
        {"source_thread_id": None},
        {"status_channel_id": "other"},
    ],
)
def test_control_conversion_rejects_each_incomplete_source_identity(
    changes: dict[str, object],
) -> None:
    request = control_request(IngressKind.CANCEL)
    for attribute, value in changes.items():
        object.__setattr__(request, attribute, value)
    with pytest.raises(InvalidApplicationOperation, match="source context"):
        command_from_ingress(request)


def test_command_conversion_rejects_missing_required_identity() -> None:
    request = new_request()
    object.__setattr__(request, "question", None)
    with pytest.raises(InvalidApplicationOperation, match="missing its question"):
        command_from_ingress(request)

    request = control_request(IngressKind.RETRY)
    object.__setattr__(request, "target_debate_id", None)
    with pytest.raises(InvalidApplicationOperation, match="missing its target identity"):
        command_from_ingress(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(IngressKind))
async def test_adapter_applies_each_claimed_request_through_existing_typed_use_case(
    kind: IngressKind,
) -> None:
    use_cases = FakeUseCases()
    request = claimed(new_request() if kind is IngressKind.NEW_DEBATE else control_request(kind))

    result = await IngressCommandAdapter(use_cases).apply(
        request,
        claim_owner="runtime-instance",
        at=NOW,
    )

    assert result == AppliedIngressCommand(kind, use_cases.debate_id, use_cases.attempt_id)
    assert len(use_cases.commands) == 1
    assert use_cases.claims == [
        IngressClaimFence.from_claimed_request(
            request,
            claim_owner="runtime-instance",
            write_at=NOW,
        )
    ]
    assert not use_cases.commands[0].__class__.__module__.startswith("discord")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [IngressKind.NEW_DEBATE, IngressKind.RETRY])
async def test_adapter_surfaces_persisted_pre_activation_failure_without_hiding_ids(
    kind: IngressKind,
) -> None:
    use_cases = FakeUseCases(terminal_error_code="discord_context_invalid")
    request = claimed(new_request() if kind is IngressKind.NEW_DEBATE else control_request(kind))

    result = await IngressCommandAdapter(use_cases).apply(
        request,
        claim_owner="runtime-instance",
        at=NOW,
    )

    assert result == AppliedIngressCommand(
        kind,
        use_cases.debate_id,
        use_cases.attempt_id,
        "discord_context_invalid",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["debate", "attempt"])
async def test_adapter_rejects_a_result_that_is_no_longer_current(mismatch: str) -> None:
    use_cases = (
        FakeUseCases(snapshot_debate_id=DebateId.new())
        if mismatch == "debate"
        else FakeUseCases(snapshot_attempt_id=AttemptId.new())
    )

    with pytest.raises(InvalidApplicationOperation, match="current attempt"):
        await IngressCommandAdapter(use_cases).apply(
            claimed(new_request()),
            claim_owner="runtime-instance",
            at=NOW,
        )


@pytest.mark.asyncio
async def test_adapter_rejects_unexpected_terminal_replay() -> None:
    use_cases = FakeUseCases(snapshot_phase=DebatePhase.COMPLETED)

    with pytest.raises(InvalidApplicationOperation, match="unexpected terminal state"):
        await IngressCommandAdapter(use_cases).apply(
            claimed(new_request()),
            claim_owner="runtime-instance",
            at=NOW,
        )


@pytest.mark.asyncio
async def test_adapter_compensates_only_matching_startable_ingress_with_exact_claim() -> None:
    use_cases = FakeUseCases()
    adapter = IngressCommandAdapter(use_cases)
    request = claimed(new_request())
    applied = AppliedIngressCommand(
        IngressKind.NEW_DEBATE,
        use_cases.debate_id,
        use_cases.attempt_id,
    )

    error_code = await adapter.abort_pre_activation(
        request,
        applied,
        claim_owner="runtime-instance",
        at=NOW,
        error_code="discord_context_invalid",
    )

    assert error_code == "discord_context_invalid"
    assert use_cases.aborts == [(IngressKind.NEW_DEBATE, "discord_context_invalid")]

    with pytest.raises(InvalidApplicationOperation, match="matching startable"):
        await adapter.abort_pre_activation(
            request,
            AppliedIngressCommand(IngressKind.CANCEL, use_cases.debate_id, use_cases.attempt_id),
            claim_owner="runtime-instance",
            at=NOW,
            error_code="must_not_run",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_update", "claim_owner", "at"),
    [
        (
            {"status": IngressStatus.PENDING, "claim_owner": None, "claim_expires_at": None},
            "runtime-instance",
            NOW,
        ),
        ({}, "another-runtime", NOW),
        ({"claim_expires_at": NOW}, "runtime-instance", NOW),
        ({"claim_expires_at": NOW - timedelta(microseconds=1)}, "runtime-instance", NOW),
    ],
)
async def test_adapter_refuses_to_bypass_or_reuse_a_lost_claim(
    request_update: dict[str, object],
    claim_owner: str,
    at: datetime,
) -> None:
    use_cases = FakeUseCases()
    request = replace(claimed(new_request()), **request_update)

    with pytest.raises(RepositoryConflict, match="no longer owned"):
        await IngressCommandAdapter(use_cases).apply(
            request,
            claim_owner=claim_owner,
            at=at,
        )

    assert use_cases.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_owner", "at", "message"),
    [
        (" ", NOW, "claim owner"),
        ("runtime-instance", NOW.replace(tzinfo=None), "timezone-aware UTC"),
        (
            "runtime-instance",
            NOW.astimezone(timezone(timedelta(hours=9))),
            "timezone-aware UTC",
        ),
    ],
)
async def test_adapter_rejects_invalid_claim_validation_inputs(
    claim_owner: str,
    at: datetime,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await IngressCommandAdapter(FakeUseCases()).apply(
            claimed(new_request()),
            claim_owner=claim_owner,
            at=at,
        )
