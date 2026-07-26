"""Thin HTTP ingress Lambda boundary behavior."""

import logging
from datetime import UTC, datetime
from typing import cast

import pytest

from shittim_chest.adapters.discord_http import (
    DiscordHttpBoundary,
    DiscordHttpReception,
    pong_response,
)
from shittim_chest.application import DiscordHttpOperation, IngressKind
from shittim_chest.application.ingress import (
    DiscordIngressApplication,
    IngressAcceptance,
    IngressOutcome,
)
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.lambda_handlers import discord_ingress as ingress_module
from shittim_chest.lambda_handlers.discord_ingress import DiscordIngressLambda
from shittim_chest.runtime.primitives import SystemClock

NOW = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)


class FakeBoundary:
    def __init__(self, reception: DiscordHttpReception) -> None:
        self.reception = reception
        self.received_at: datetime | None = None

    def receive(self, event: object, *, now: datetime) -> DiscordHttpReception:
        del event
        self.received_at = now
        return self.reception


class FakeApplication:
    def __init__(self, outcome: IngressOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def accept(self, operation: DiscordHttpOperation) -> IngressAcceptance:
        del operation
        self.calls += 1
        return IngressAcceptance(self.outcome, created=True)


def operation() -> DiscordHttpOperation:
    return DiscordHttpOperation(
        interaction_id="301",
        operation_id="301",
        kind=IngressKind.NEW_DEBATE,
        application_id="201",
        guild_id="101",
        channel_id="102",
        channel_type=0,
        parent_channel_id=None,
        requester_id="105",
        requester_username="requester",
        requester_display_name="Requester",
        can_manage_messages=False,
        received_at=NOW,
        command_name="shittim",
        question="question",
    )


def test_ping_returns_before_application_construction() -> None:
    constructed = 0

    def load() -> DiscordIngressApplication:
        nonlocal constructed
        constructed += 1
        raise AssertionError("PING must not construct AWS application adapters")

    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary, FakeBoundary(DiscordHttpReception(response=pong_response()))
        ),
        application=load,
        clock=SystemClock(),
    )

    response = handler.handle({})

    assert response["statusCode"] == 200
    assert response["body"] == '{"type":1}'
    assert constructed == 0


def test_operation_returns_inline_type_four_ephemeral_response() -> None:
    application = FakeApplication(IngressOutcome.STARTING)
    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(DiscordHttpReception(interaction=operation())),
        ),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=SystemClock(),
    )

    response = handler.handle({})

    assert response["statusCode"] == 200
    body = str(response["body"])
    assert '"type":4' in body
    assert '"flags":64' in body
    assert '"allowed_mentions":{"parse":[]}' in body
    assert '"type":5' not in body
    assert application.calls == 1


def test_explicit_entry_timestamp_includes_bootstrap_time_in_budget() -> None:
    application = FakeApplication(IngressOutcome.STARTING)
    boundary = FakeBoundary(DiscordHttpReception(interaction=operation()))
    handler = DiscordIngressLambda(
        boundary=cast(DiscordHttpBoundary, boundary),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=SystemClock(),
    )

    handler.handle({}, received_at=NOW)

    assert boundary.received_at == NOW


def test_lambda_entry_captures_time_before_handler_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class EntryClock:
        def now(self) -> datetime:
            events.append("entry-time")
            return NOW

    class Handler:
        def handle(
            self,
            event: object,
            *,
            received_at: datetime,
        ) -> dict[str, object]:
            del event
            events.append("handle")
            assert received_at == NOW
            return {"statusCode": 200}

    def get_handler() -> Handler:
        events.append("build")
        return Handler()

    monkeypatch.setattr(ingress_module, "SystemClock", EntryClock)
    monkeypatch.setattr(ingress_module, "_get_handler", get_handler)

    assert ingress_module.lambda_handler({}, object()) == {"statusCode": 200}
    assert events == ["entry-time", "build", "handle"]


def test_failure_log_contains_only_category_and_request_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Context:
        aws_request_id = "request-id"

    class FailingHandler:
        def handle(self, event: object, *, received_at: datetime) -> dict[str, object]:
            del event, received_at
            raise RepositoryUnavailable()

    monkeypatch.setattr(
        ingress_module,
        "_get_handler",
        lambda: FailingHandler(),
    )
    caplog.set_level(logging.ERROR)

    response = ingress_module.lambda_handler(
        {"body": "private question and token"},
        Context(),
    )

    assert response["statusCode"] == 503
    log_text = caplog.text
    assert "category=repository_unavailable" in log_text
    assert "request_id=request-id" in log_text
    assert "private question" not in log_text
    assert "token" not in log_text
