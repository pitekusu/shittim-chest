"""Tests for the token-free ingress to existing-use-case boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import (
    AcceptDebateRequest,
    AcceptedDebate,
    AcceptedRetry,
    AppliedIngressCommand,
    CancelDebateCommand,
    CancelledDebate,
    IngressCommandAdapter,
    IngressKind,
    IngressRequest,
    IngressStatus,
    RetryDebateCommand,
    command_from_ingress,
)
from shittim_chest.application.errors import InvalidApplicationOperation
from shittim_chest.application.ports import RepositoryConflict
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FakeUseCases:
    debate_id: DebateId = field(default_factory=DebateId.new)
    attempt_id: AttemptId = field(default_factory=AttemptId.new)
    commands: list[AcceptDebateRequest | CancelDebateCommand | RetryDebateCommand] = field(
        default_factory=list
    )

    async def accept_debate(self, request: AcceptDebateRequest) -> AcceptedDebate:
        self.commands.append(request)
        return AcceptedDebate(self.debate_id, self.attempt_id)

    async def cancel_debate(self, command: CancelDebateCommand) -> CancelledDebate:
        self.commands.append(command)
        return CancelledDebate(self.debate_id, self.attempt_id)

    async def retry_debate(self, command: RetryDebateCommand) -> AcceptedRetry:
        self.commands.append(command)
        if command.expected_attempt_id is None:
            raise AssertionError("test retry command must preserve its source attempt")
        return AcceptedRetry(self.debate_id, self.attempt_id, command.expected_attempt_id)


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
    assert not use_cases.commands[0].__class__.__module__.startswith("discord")


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
