"""Content-free Status Publisher Lambda boundary tests."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

import shittim_chest.lambda_handlers.discord_status_publisher as module
from shittim_chest.adapters.aws import SsmParameterReader
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.application.scale_to_zero import IngressRequest
from shittim_chest.application.status_publication import (
    DiscordStatusGateway,
    PublicStatusPublisher,
    StatusDeliveryError,
    StatusGatewayFactory,
    StatusPublicationOutcome,
)
from shittim_chest.config.status_publisher import (
    MODERATOR_TOKEN_PARAMETER,
    StatusPublisherSettings,
)
from shittim_chest.lambda_handlers.discord_status_publisher import (
    DiscordStatusPublisherLambda,
    StatusPublisherInvocationError,
)

TOKEN = "local-moderator-value"  # noqa: S105 - offline fixture value.
RUNTIME_PARAMETER = "/shittim-chest/production/runtime/v0001"
RUNTIME_JSON = json.dumps(
    {
        "schema_version": "2",
        "config_version": "v0001",
        "guild_id": "100",
        "allowed_channel_ids": ["101"],
        "farewell_channel_id": "101",
        "identities": [
            {"slot": "moderator", "application_id": "200"},
            {"slot": "participant-a", "application_id": "201"},
            {"slot": "participant-b", "application_id": "202"},
            {"slot": "participant-c", "application_id": "203"},
        ],
    }
)


def status_request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="300",
        operation_id="300",
        application_id="200",
        question="question",
        requester_id="400",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="100",
        channel_id="101",
        command_name="shittim",
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
    )


@dataclass(slots=True)
class FakeReader:
    calls: list[tuple[str, bool]] = field(default_factory=list)

    async def get_parameter(self, name: str, *, with_decryption: bool = True) -> str:
        self.calls.append((name, with_decryption))
        return RUNTIME_JSON if name == RUNTIME_PARAMETER else TOKEN


@dataclass(slots=True)
class FakePublisher:
    outcome: StatusPublicationOutcome
    request: IngressRequest = field(default_factory=status_request)
    request_gateway: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def publish(
        self,
        *,
        interaction_id: str,
        claim_owner: str,
        gateway_factory: StatusGatewayFactory,
    ) -> StatusPublicationOutcome:
        self.calls.append((interaction_id, claim_owner))
        if self.request_gateway:
            gateway: DiscordStatusGateway = await gateway_factory(self.request)
            assert gateway is not None
        return self.outcome


def handler(
    publisher: FakePublisher,
    reader: FakeReader,
) -> DiscordStatusPublisherLambda:
    return DiscordStatusPublisherLambda(
        publisher=cast(PublicStatusPublisher, publisher),
        reader=cast(SsmParameterReader, reader),
        settings=StatusPublisherSettings(
            aws_region="ap-northeast-1",
            table_name="test-table",
            runtime_config_parameter=RUNTIME_PARAMETER,
            moderator_token_parameter=MODERATOR_TOKEN_PARAMETER,
        ),
        http_client=httpx.Client(
            base_url="https://discord.com/api/v10",
            transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
        ),
    )


def test_exact_event_reads_only_the_moderator_token_after_claim() -> None:
    publisher = FakePublisher(StatusPublicationOutcome.DELIVERED)
    reader = FakeReader()

    result = handler(publisher, reader).handle(
        {"schema_version": 1, "interaction_id": "300"},
        claim_owner="request-id",
    )

    assert result == {"outcome": "delivered"}
    assert publisher.calls == [("300", "request-id")]
    assert reader.calls == [
        (RUNTIME_PARAMETER, True),
        (MODERATOR_TOKEN_PARAMETER, True),
    ]


def test_gateway_identity_comes_from_the_validated_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_gateway(
        *,
        client: httpx.Client,
        bot_token: str,
        expected_application_id: str,
        expected_guild_id: str,
    ) -> object:
        captured.update(
            client=client,
            bot_token=bot_token,
            expected_application_id=expected_application_id,
            expected_guild_id=expected_guild_id,
        )
        return object()

    monkeypatch.setattr(module, "DiscordRestStatusGateway", capture_gateway)
    publisher = FakePublisher(StatusPublicationOutcome.DELIVERED)
    reader = FakeReader()

    result = handler(publisher, reader).handle(
        {"schema_version": 1, "interaction_id": "300"},
        claim_owner="request-id",
    )

    assert result == {"outcome": "delivered"}
    assert captured["bot_token"] == TOKEN
    assert captured["expected_application_id"] == "200"
    assert captured["expected_guild_id"] == "100"


def test_no_work_does_not_read_any_secret() -> None:
    publisher = FakePublisher(StatusPublicationOutcome.NO_WORK, request_gateway=False)
    reader = FakeReader()

    result = handler(publisher, reader).handle(
        {"schema_version": 1, "interaction_id": "300"},
        claim_owner="request-id",
    )

    assert result == {"outcome": "no_work"}
    assert reader.calls == []


def test_persisted_boundary_mismatch_fails_before_token_read() -> None:
    publisher = FakePublisher(StatusPublicationOutcome.DELIVERED)
    publisher.request = replace(publisher.request, application_id="999")
    reader = FakeReader()

    with pytest.raises(StatusDeliveryError):
        handler(publisher, reader).handle(
            {"schema_version": 1, "interaction_id": "300"},
            claim_owner="request-id",
        )
    assert reader.calls == [(RUNTIME_PARAMETER, True)]


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"schema_version": True, "interaction_id": "300"},
        {"schema_version": 1, "interaction_id": "not-a-snowflake"},
        {"schema_version": 1, "interaction_id": "300", "token": "forbidden"},
    ],
)
def test_invalid_event_fails_before_token_read(event: object) -> None:
    publisher = FakePublisher(StatusPublicationOutcome.DELIVERED)
    reader = FakeReader()

    with pytest.raises(ValueError):
        handler(publisher, reader).handle(event, claim_owner="request-id")
    assert publisher.calls == []
    assert reader.calls == []


def test_top_level_failure_log_and_exception_are_content_free(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingHandler:
        def handle(self, event: object, *, claim_owner: str) -> dict[str, str]:
            del event, claim_owner
            raise RepositoryUnavailable

    monkeypatch.setattr(module, "_handler", FailingHandler())
    event = {"schema_version": 1, "interaction_id": "300", "question": "private-question"}
    context = type("Context", (), {"aws_request_id": "request-id"})()

    with caplog.at_level(logging.ERROR), pytest.raises(StatusPublisherInvocationError) as caught:
        module.lambda_handler(event, context)

    assert str(caught.value) == "status_publisher_invocation_failed"
    assert "private-question" not in caplog.text
    assert TOKEN not in caplog.text
    assert "repository_unavailable" in caplog.text
