"""Thin HTTP ingress Lambda boundary behavior."""

import asyncio
import inspect
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.adapters.aws.clients import current_ingress_sdk_cancellation_gate
from shittim_chest.adapters.discord_http import (
    DiscordHttpAuthenticationError,
    DiscordHttpBoundary,
    DiscordHttpReception,
    error_response,
    pong_response,
)
from shittim_chest.application import DiscordHttpOperation, IngressKind
from shittim_chest.application.ingress import (
    DiscordIngressApplication,
    IngressAcceptance,
    IngressOutcome,
)
from shittim_chest.application.ports import Clock, RepositoryUnavailable
from shittim_chest.lambda_handlers import discord_ingress as ingress_module
from shittim_chest.lambda_handlers.discord_ingress import (
    DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS,
    DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS,
    DISCORD_INGRESS_SDK_GATE_LEAD_SECONDS,
    DISCORD_INGRESS_SOFT_DEADLINE_SECONDS,
    DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS,
    DiscordIngressLambda,
    DiscordVerifiedIngressFailure,
)
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


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def now(self) -> datetime:
        return next(self._values)


class SlowApplication:
    def __init__(self) -> None:
        self.cancelled = False

    async def accept(self, operation: DiscordHttpOperation) -> IngressAcceptance:
        del operation
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("slow application must be cancelled")


class ActiveSdkRoundApplication:
    """Model one in-flight transaction followed by a forbidden serial SDK call."""

    def __init__(self) -> None:
        self.active_rounds = 0
        self.durable_round_finished = False
        self.next_rounds = 0
        self.worker_finished = False

    async def accept(self, operation: DiscordHttpOperation) -> IngressAcceptance:
        del operation
        await asyncio.to_thread(self._durable_then_next_round)
        raise AssertionError("deadline gate must reject the next SDK round")

    def _durable_then_next_round(self) -> None:
        gate = current_ingress_sdk_cancellation_gate()
        assert gate is not None
        try:
            self.active_rounds += 1
            time.sleep(0.04)
            self.durable_round_finished = True
            gate.raise_if_cancelled()
            self.next_rounds += 1
        finally:
            self.worker_finished = True


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


def test_invalid_signature_returns_before_application_construction() -> None:
    constructed = 0

    def load() -> DiscordIngressApplication:
        nonlocal constructed
        constructed += 1
        raise AssertionError("invalid signatures must not construct AWS application adapters")

    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(
                DiscordHttpReception(response=error_response(DiscordHttpAuthenticationError()))
            ),
        ),
        application=load,
        clock=SystemClock(),
    )

    response = handler.handle({})

    assert response["statusCode"] == 401
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
        clock=cast(Clock, FixedClock(NOW)),
    )

    handler.handle({}, received_at=NOW)

    assert boundary.received_at == NOW


def test_deadline_budget_reserves_sdk_unwind_and_api_response_time() -> None:
    assert pytest.approx(1.2) == DISCORD_INGRESS_SOFT_DEADLINE_SECONDS
    assert pytest.approx(0.4) == DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS
    assert pytest.approx(1.4) == DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS
    assert pytest.approx(0.1) == DISCORD_INGRESS_SDK_GATE_LEAD_SECONDS
    assert pytest.approx(DISCORD_INITIAL_RESPONSE_DEADLINE_SECONDS) == (
        DISCORD_INGRESS_SOFT_DEADLINE_SECONDS
        + DISCORD_INGRESS_MAX_ACTIVE_SDK_CALL_SECONDS
        + DISCORD_INGRESS_RESPONSE_MARGIN_SECONDS
    )


def test_bootstrap_elapsed_time_exhausts_budget_before_application_call() -> None:
    application = FakeApplication(IngressOutcome.STARTING)
    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(DiscordHttpReception(interaction=operation())),
        ),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=cast(Clock, FixedClock(NOW + timedelta(seconds=1.21))),
    )

    with pytest.raises(DiscordVerifiedIngressFailure) as caught:
        handler.handle({}, received_at=NOW)

    assert caught.value.category == "deadline_exceeded"
    assert application.calls == 0


def test_synchronous_application_factory_time_is_inside_entry_budget() -> None:
    application = FakeApplication(IngressOutcome.STARTING)
    constructed = 0

    def load() -> DiscordIngressApplication:
        nonlocal constructed
        constructed += 1
        return cast(DiscordIngressApplication, application)

    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(DiscordHttpReception(interaction=operation())),
        ),
        application=load,
        clock=cast(
            Clock,
            SequenceClock(NOW, NOW + timedelta(seconds=1.21)),
        ),
    )

    with pytest.raises(DiscordVerifiedIngressFailure) as caught:
        handler.handle({}, received_at=NOW)

    assert caught.value.category == "deadline_exceeded"
    assert constructed == 1
    assert application.calls == 0


def test_deadline_waits_for_one_active_round_then_blocks_the_next_round() -> None:
    application = ActiveSdkRoundApplication()
    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(DiscordHttpReception(interaction=operation())),
        ),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=cast(Clock, FixedClock(NOW)),
        soft_deadline_seconds=0.02,
    )

    with pytest.raises(DiscordVerifiedIngressFailure) as caught:
        handler.handle({}, received_at=NOW)

    assert caught.value.category == "deadline_exceeded"
    assert application.active_rounds == 1
    assert application.durable_round_finished
    assert application.next_rounds == 0
    assert application.worker_finished


def test_slow_accept_is_cancelled_and_returns_content_free_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Context:
        aws_request_id = "deadline-request"

    application = SlowApplication()
    handler = DiscordIngressLambda(
        boundary=cast(
            DiscordHttpBoundary,
            FakeBoundary(DiscordHttpReception(interaction=operation())),
        ),
        application=lambda: cast(DiscordIngressApplication, application),
        clock=cast(Clock, FixedClock(NOW)),
        soft_deadline_seconds=0.01,
    )
    monkeypatch.setattr(ingress_module, "SystemClock", lambda: FixedClock(NOW))
    monkeypatch.setattr(ingress_module, "_get_handler", lambda: handler)
    caplog.set_level(logging.ERROR)

    response = ingress_module.lambda_handler(
        {"body": "private question and token"},
        Context(),
    )

    assert application.cancelled
    assert response == {
        "statusCode": 200,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": (
            '{"data":{"allowed_mentions":{"parse":[]},"content":'
            '"受付結果を時間内に確認できませんでした。\\nチャンネルに状態が表示されない場合のみ、'
            'もう一度実行してください。","flags":64},"type":4}'
        ),
        "isBase64Encoded": False,
    }
    assert "category=deadline_exceeded" in caplog.text
    assert "request_id=deadline-request" in caplog.text
    assert "private question" not in caplog.text
    assert "token" not in caplog.text


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


def test_snapstart_restore_and_warm_invocations_emit_content_free_timing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Handler:
        def handle(
            self,
            event: object,
            *,
            received_at: datetime,
        ) -> dict[str, object]:
            del event, received_at
            return {"statusCode": 200}

    timestamps = iter((1_000_000_000, 1_125_000_000, 2_000_000_000, 2_050_000_000))
    monkeypatch.setattr(ingress_module, "_first_invocation", True)
    monkeypatch.setattr(ingress_module, "_get_handler", Handler)
    monkeypatch.setattr(ingress_module.time, "monotonic_ns", lambda: next(timestamps))
    monkeypatch.setenv("AWS_LAMBDA_INITIALIZATION_TYPE", "snap-start")
    caplog.set_level(logging.INFO)

    private_event = {"body": "private question, signature, and token"}
    assert ingress_module.lambda_handler(private_event, object()) == {"statusCode": 200}
    assert ingress_module.lambda_handler(private_event, object()) == {"statusCode": 200}

    assert "invocation_kind=restore duration_ms=125" in caplog.text
    assert "invocation_kind=warm duration_ms=50" in caplog.text
    assert "private question" not in caplog.text
    assert "signature" not in caplog.text
    assert "token" not in caplog.text


def test_ingress_composition_uses_direct_lazy_adapter_imports() -> None:
    source = inspect.getsource(ingress_module)

    assert "from shittim_chest.adapters.aws import" not in source
    assert "from shittim_chest.adapters.dynamodb import" not in source
    assert "create_ssm_client" not in source
    assert "create_lambda_client" not in source
    assert "LambdaStatusPublicationTrigger" not in source
    assert "LambdaRuntimeReconciliationTrigger" not in source
    assert "_handler: DiscordIngressLambda | None = None" in source
    assert source.index("def lambda_handler") < source.index("def _build_handler")


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
