"""Local cross-workstream path from signed HTTP ingress to runtime acceptance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from mypy_boto3_dynamodb.client import DynamoDBClient
from nacl.signing import SigningKey

from shittim_chest.adapters.discord_http import DiscordHttpBoundary, DiscordRequestVerifier
from shittim_chest.adapters.dynamodb import (
    DynamoDbDebateAuthorizationLookup,
    DynamoDbIngressRepository,
    DynamoDbRuntimeActivityInspector,
    DynamoDbRuntimeStateRepository,
)
from shittim_chest.application import (
    AppliedIngressCommand,
    DiscordBotSlot,
    DiscordHttpOperation,
    DiscordIdentityConfig,
    DiscordIngressApplication,
    DiscordRuntimeConfig,
    DiscordStatusGateway,
    DiscordStatusMessage,
    EcsRuntimeSnapshot,
    IngressKind,
    IngressOutcome,
    IngressRequest,
    IngressStatus,
    PublicStatusPublisher,
    RuntimeReconciler,
    RuntimeStatus,
    StatusPublicationOutcome,
)
from shittim_chest.application.ingress_drain import (
    IngressDrainer,
    IngressDrainStop,
    RuntimeIngressDrainGate,
)
from shittim_chest.application.runtime_instance import RuntimeInstanceState
from shittim_chest.application.scale_to_zero import StatusHistoryCheckpoint
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
INTERACTION_ID = "301"
APPLICATION_ID = "201"
GUILD_ID = "101"
CHANNEL_ID = "102"
REQUESTER_ID = "103"
RUNTIME_INSTANCE_ID = "task-runtime-1"


@dataclass(slots=True)
class MutableClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


class NoopTriggers:
    def __init__(self) -> None:
        self.status_ids: list[str] = []
        self.reconciliation_ids: list[str] = []

    async def request_publication(self, interaction_id: str) -> None:
        self.status_ids.append(interaction_id)

    async def request_reconciliation(self, interaction_id: str) -> None:
        self.reconciliation_ids.append(interaction_id)


class RecordingEcs:
    def __init__(self) -> None:
        self.snapshot = EcsRuntimeSnapshot(
            desired_count=0,
            running_count=0,
            pending_count=0,
        )
        self.desired_counts: list[int] = []

    async def describe(self) -> EcsRuntimeSnapshot:
        return self.snapshot

    async def set_desired_count(self, desired_count: int) -> EcsRuntimeSnapshot:
        self.desired_counts.append(desired_count)
        self.snapshot = EcsRuntimeSnapshot(
            desired_count=desired_count,
            running_count=0,
            pending_count=desired_count,
        )
        return self.snapshot


class RecordingStatusGateway:
    def __init__(self) -> None:
        self.messages: dict[str, DiscordStatusMessage] = {}

    async def current_bot_user_id(self) -> str:
        return APPLICATION_ID

    async def fetch_message(
        self,
        *,
        channel_id: str,
        message_id: str,
    ) -> DiscordStatusMessage:
        message = self.messages[message_id]
        assert message.channel_id == channel_id
        return message

    async def find_by_nonce(
        self,
        *,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        after_message_id: str,
        checkpoint: StatusHistoryCheckpoint | None,
    ) -> DiscordStatusMessage | None:
        del channel_id, author_id, nonce, operation_marker, after_message_id, checkpoint
        return None

    async def create_message(
        self,
        *,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> DiscordStatusMessage:
        message = DiscordStatusMessage(
            message_id="501",
            channel_id=channel_id,
            author_id=APPLICATION_ID,
            content=content,
            nonce=nonce,
        )
        self.messages[message.message_id] = message
        return message

    async def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> DiscordStatusMessage:
        previous = await self.fetch_message(channel_id=channel_id, message_id=message_id)
        updated = DiscordStatusMessage(
            message_id=previous.message_id,
            channel_id=previous.channel_id,
            author_id=previous.author_id,
            content=content,
            nonce=previous.nonce,
        )
        self.messages[message_id] = updated
        return updated


class OpenAdmission:
    @property
    def is_accepting(self) -> bool:
        return True

    async def all_identities_ready(self) -> bool:
        return True


class RecordingCommands:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.result = AppliedIngressCommand(
            kind=IngressKind.NEW_DEBATE,
            debate_id=DebateId.new(),
            attempt_id=AttemptId.new(),
        )

    async def apply(
        self,
        request: IngressRequest,
        *,
        claim_owner: str,
        at: datetime,
    ) -> AppliedIngressCommand:
        assert request.claim_owner == claim_owner
        assert request.claim_expires_at is not None and at < request.claim_expires_at
        self.calls.append(request.interaction_id)
        return self.result

    async def abort_pre_activation(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
        *,
        claim_owner: str,
        at: datetime,
        error_code: str,
    ) -> str:
        del request, applied, claim_owner, at, error_code
        raise AssertionError("successful E2E acceptance must not abort")


class RecordingContext:
    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.activated: list[str] = []

    async def preflight(self, request: IngressRequest) -> None:
        del request
        raise AssertionError("new debate must not run component preflight")

    async def prepare(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        del applied
        self.prepared.append(request.interaction_id)

    async def activate(
        self,
        request: IngressRequest,
        applied: AppliedIngressCommand,
    ) -> None:
        del applied
        self.activated.append(request.interaction_id)


def _runtime_config() -> DiscordRuntimeConfig:
    return DiscordRuntimeConfig(
        guild_id=GUILD_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
        identities=tuple(
            DiscordIdentityConfig(slot=slot, application_id=str(201 + index))
            for index, slot in enumerate(DiscordBotSlot)
        ),
        schema_version="1",
    )


def _signed_command(signing_key: SigningKey) -> dict[str, object]:
    payload = {
        "version": 1,
        "id": INTERACTION_ID,
        "application_id": APPLICATION_ID,
        "type": 2,
        "token": "handler-only-value",
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "channel": {"id": CHANNEL_ID, "type": 0, "parent_id": None},
        "member": {
            "nick": "Requester",
            "permissions": "0",
            "user": {
                "id": REQUESTER_ID,
                "username": "requester",
                "global_name": "Requester",
            },
        },
        "data": {
            "type": 1,
            "name": "shittim",
            "options": [
                {
                    "name": "question",
                    "type": 3,
                    "value": "今日の朝ごはんは何がいい?甘いものが食べたい",
                }
            ],
        },
    }
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(NOW.timestamp()))
    signature = signing_key.sign(timestamp.encode("ascii") + raw_body).signature.hex()
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": "POST"}},
        "headers": {
            "x-signature-ed25519": signature,
            "x-signature-timestamp": timestamp,
        },
        "body": raw_body.decode(),
        "isBase64Encoded": False,
    }


@pytest.mark.asyncio
async def test_signed_http_ingress_wakes_recovers_and_drains_from_dynamodb_local(
    dynamodb_client: DynamoDBClient,
    dynamodb_table: str,
) -> None:
    clock = MutableClock(NOW)
    ingress = DynamoDbIngressRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    runtime = DynamoDbRuntimeStateRepository(
        client=dynamodb_client,
        table_name=dynamodb_table,
    )
    triggers = NoopTriggers()
    signing_key = SigningKey.generate()
    reception = DiscordHttpBoundary(
        DiscordRequestVerifier(signing_key.verify_key.encode().hex())
    ).receive(_signed_command(signing_key), now=NOW)
    assert reception.response is None
    assert isinstance(reception.interaction, DiscordHttpOperation)

    acceptance = await DiscordIngressApplication(
        runtime_config=_runtime_config(),
        clock=clock,
        ingress=ingress,
        runtime_state=runtime,
        status_trigger=triggers,
        reconciler_trigger=triggers,
        debates=DynamoDbDebateAuthorizationLookup(
            client=dynamodb_client,
            table_name=dynamodb_table,
        ),
    ).accept(reception.interaction)

    assert acceptance.outcome is IngressOutcome.STARTING
    assert acceptance.created
    assert triggers.status_ids == [INTERACTION_ID]
    assert triggers.reconciliation_ids == [INTERACTION_ID]

    status_gateway = RecordingStatusGateway()

    async def status_gateway_factory(request: IngressRequest) -> DiscordStatusGateway:
        assert request.interaction_id == INTERACTION_ID
        return status_gateway

    status_outcome = await PublicStatusPublisher(
        repository=ingress,
        clock=clock,
    ).publish(
        interaction_id=INTERACTION_ID,
        claim_owner="status-worker",
        gateway_factory=status_gateway_factory,
    )
    assert status_outcome is StatusPublicationOutcome.DELIVERED

    clock.current = NOW + timedelta(seconds=1)
    ecs = RecordingEcs()
    report = await RuntimeReconciler(
        clock=clock,
        ingress=ingress,
        activity=DynamoDbRuntimeActivityInspector(
            client=dynamodb_client,
            table_name=dynamodb_table,
            ingress=ingress,
        ),
        runtime_state=runtime,
        ecs=ecs,
        status_publications=ingress,
        status_trigger=triggers,
    ).reconcile()
    assert report.wake_candidates == 1
    assert report.ecs_scaled_up
    assert ecs.desired_counts == [1]
    persisted_starting = await runtime.get()
    assert persisted_starting is not None
    assert persisted_starting.status is RuntimeStatus.STARTING

    clock.current = NOW + timedelta(seconds=2)
    runtime_instance = RuntimeInstanceState(
        clock=clock,
        repository=runtime,
        runtime_instance_id=RUNTIME_INSTANCE_ID,
    )
    await runtime_instance.mark_started()
    await runtime_instance.mark_ready(active=False)
    gate = RuntimeIngressDrainGate(OpenAdmission())
    gate.mark_supervisor_started()
    gate.mark_local_command_schema_checked()
    gate.begin_recovery()
    assert not await gate.ready_to_drain()
    gate.mark_recovery_complete()

    commands = RecordingCommands()
    context = RecordingContext()
    drain_report = await IngressDrainer(
        clock=clock,
        ingress=ingress,
        runtime_state=runtime,
        commands=commands,
        context=context,
        gate=gate,
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        runtime_session=runtime_instance,
    ).drain_once()

    assert drain_report.stop is IngressDrainStop.QUEUE_DRAINED
    assert (drain_report.claimed, drain_report.accepted) == (1, 1)
    assert commands.calls == [INTERACTION_ID]
    assert context.prepared == [INTERACTION_ID]
    assert context.activated == [INTERACTION_ID]
    result = await ingress.get_operation_result(INTERACTION_ID)
    assert result is not None
    assert result.status is IngressStatus.ACCEPTED
    assert result.accepted_debate_id == commands.result.debate_id
    assert result.accepted_attempt_id == commands.result.attempt_id
    assert await ingress.list_active_wake_candidates() == ()
    persisted_busy = await runtime.get()
    assert persisted_busy is not None
    assert persisted_busy.status is RuntimeStatus.BUSY

    table = dynamodb_client.scan(TableName=dynamodb_table, ConsistentRead=True)
    assert "handler-only-value" not in json.dumps(table["Items"], sort_keys=True)
