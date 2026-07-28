from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

import shittim_chest.bootstrap as bootstrap
from shittim_chest.bootstrap import (
    DEFAULT_CLIENT_CLOSE_TIMEOUT_SECONDS,
    ProductionRuntime,
    RuntimeClientCloseTimeout,
    build_production_runtime,
)
from shittim_chest.config import load_bootstrap_config
from shittim_chest.runtime.lifecycle import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_production_composition_builds_and_closes_without_external_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "local-placeholder")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "local-placeholder")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(bootstrap, "ecs_task_instance_id", lambda: "runtime-owner")
    config = load_bootstrap_config(_environment())

    runtime = build_production_runtime(config)

    assert isinstance(runtime, ProductionRuntime)
    lifecycle = cast(Any, runtime.lifecycle)
    runtime_instance = lifecycle._runtime_instance
    drainer = lifecycle._drainer
    ingress_runtime = lifecycle._ingress_runtime
    assert runtime_instance.runtime_instance_id == "runtime-owner"
    assert drainer._runtime_instance_id == "runtime-owner"
    assert drainer._runtime_session is runtime_instance
    assert drainer._runtime_state is runtime_instance._repository
    assert drainer._context is ingress_runtime
    assert ingress_runtime._application is drainer._commands._application
    assert lifecycle._interactions._application is ingress_runtime._application
    await runtime.aclose()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_run_from_environment_keeps_third_party_root_logging_at_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    basic_config: dict[str, object] = {}
    application_levels: list[int] = []
    runtime_runs: list[str] = []

    class _ApplicationLogger:
        def setLevel(self, level: int) -> None:
            application_levels.append(level)

    class _Runtime:
        async def run(self) -> None:
            runtime_runs.append("run")

    def _configure_logging(*, level: int, format: str) -> None:
        basic_config.update(level=level, format=format)

    def _build(config: object) -> _Runtime:
        del config
        return _Runtime()

    environment = _environment()
    environment["SHITTIM_LOG_LEVEL"] = "DEBUG"
    monkeypatch.setattr(logging, "basicConfig", _configure_logging)
    monkeypatch.setattr(bootstrap, "_LOGGER", _ApplicationLogger())
    monkeypatch.setattr(bootstrap, "build_production_runtime", _build)

    await bootstrap.run_from_environment(environment)

    assert basic_config == {"level": logging.WARNING, "format": "%(message)s"}
    assert application_levels == [logging.DEBUG]
    assert runtime_runs == ["run"]


@dataclass(slots=True)
class _BlockingCloser:
    release: asyncio.Event
    calls: int = 0

    async def close(self) -> None:
        self.calls += 1
        await self.release.wait()


@dataclass(slots=True)
class _AsyncCloser:
    calls: int = 0

    async def close(self) -> None:
        self.calls += 1


@dataclass(slots=True)
class _SyncCloser:
    calls: int = 0

    def close(self) -> None:
        self.calls += 1


@dataclass(slots=True)
class _BlockingSyncCloser:
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    calls: int = 0

    def close(self) -> None:
        self.calls += 1
        self.started.set()
        self.release.wait()
        self.finished.set()


@dataclass(slots=True)
class _CancelledCloser:
    calls: int = 0

    async def close(self) -> None:
        self.calls += 1
        raise asyncio.CancelledError


@dataclass(slots=True)
class _FailingCloser:
    calls: int = 0

    async def close(self) -> None:
        self.calls += 1
        raise RuntimeError("client close failed")


@dataclass(slots=True)
class _Telemetry:
    events: list[str] = field(default_factory=list)

    def runtime_event(self, event: str, **fields: str | int) -> None:
        del fields
        self.events.append(event)


@pytest.mark.asyncio
async def test_process_client_close_is_concurrent_bounded_and_idempotent() -> None:
    supervisor = _BlockingCloser(asyncio.Event())
    openai_client = _AsyncCloser()
    dynamodb_client = _SyncCloser()
    runtime = ProductionRuntime(
        lifecycle=cast(Any, object()),
        supervisor=cast(Any, supervisor),
        openai_client=cast(Any, openai_client),
        dynamodb_client=cast(Any, dynamodb_client),
        telemetry=cast(Any, _Telemetry()),
        client_close_timeout_seconds=0.01,
    )

    with pytest.raises(RuntimeClientCloseTimeout, match="safety deadline"):
        await runtime.aclose()
    await runtime.aclose()

    assert supervisor.calls == 1
    assert openai_client.calls == 1
    assert dynamodb_client.calls == 1
    assert [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ] == []


@pytest.mark.asyncio
async def test_blocked_synchronous_client_close_cannot_extend_exit_budget() -> None:
    dynamodb_client = _BlockingSyncCloser()
    runtime = ProductionRuntime(
        lifecycle=cast(Any, object()),
        supervisor=cast(Any, _AsyncCloser()),
        openai_client=cast(Any, _AsyncCloser()),
        dynamodb_client=cast(Any, dynamodb_client),
        telemetry=cast(Any, _Telemetry()),
        client_close_timeout_seconds=0.02,
    )

    with pytest.raises(RuntimeClientCloseTimeout, match="safety deadline"):
        await runtime.aclose()

    assert dynamodb_client.started.wait(timeout=1)
    assert dynamodb_client.calls == 1
    dynamodb_client.release.set()
    assert dynamodb_client.finished.wait(timeout=1)


@pytest.mark.asyncio
async def test_process_client_close_does_not_swallow_cancellation() -> None:
    cancelled = _CancelledCloser()
    runtime = ProductionRuntime(
        lifecycle=cast(Any, object()),
        supervisor=cast(Any, cancelled),
        openai_client=cast(Any, _AsyncCloser()),
        dynamodb_client=cast(Any, _SyncCloser()),
        telemetry=cast(Any, _Telemetry()),
    )

    with pytest.raises(asyncio.CancelledError):
        await runtime.aclose()

    assert cancelled.calls == 1


@pytest.mark.asyncio
async def test_process_client_close_reports_non_cancellation_failures() -> None:
    failing = _FailingCloser()
    runtime = ProductionRuntime(
        lifecycle=cast(Any, object()),
        supervisor=cast(Any, failing),
        openai_client=cast(Any, _AsyncCloser()),
        dynamodb_client=cast(Any, _SyncCloser()),
        telemetry=cast(Any, _Telemetry()),
    )

    with pytest.raises(ExceptionGroup, match="process client shutdown failed") as captured:
        await runtime.aclose()

    assert [str(error) for error in captured.value.exceptions] == ["client close failed"]


def test_default_exit_budget_stays_below_fargate_stop_timeout() -> None:
    assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS + DEFAULT_CLIENT_CLOSE_TIMEOUT_SECONDS == 110
    assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS + DEFAULT_CLIENT_CLOSE_TIMEOUT_SECONDS < 120


def test_process_client_close_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        ProductionRuntime(
            lifecycle=cast(Any, object()),
            supervisor=cast(Any, object()),
            openai_client=cast(Any, object()),
            dynamodb_client=cast(Any, object()),
            telemetry=cast(Any, object()),
            client_close_timeout_seconds=0,
        )


def _environment() -> dict[str, str]:
    values = {
        "SHITTIM_ENVIRONMENT": "production",
        "AWS_REGION": "ap-northeast-1",
        "SHITTIM_DYNAMODB_TABLE": "local-table",
        "OPENAI_API_KEY": "openai-key-placeholder",
        "DISCORD_TOKEN_MODERATOR": "token-moderator-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_A": "token-a-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_B": "token-b-placeholder",
        "DISCORD_TOKEN_PARTICIPANT_C": "token-c-placeholder",
        "SHITTIM_RUNTIME_CONFIG_JSON": json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "guild_id": "11",
                "allowed_channel_ids": ["21"],
                "identities": [
                    {"slot": "moderator", "application_id": "31"},
                    {"slot": "participant-a", "application_id": "32"},
                    {"slot": "participant-b", "application_id": "33"},
                    {"slot": "participant-c", "application_id": "34"},
                ],
            }
        ),
    }
    persona_env = {
        "moderator": "SHITTIM_PERSONA_MODERATOR_JSON",
        "participant-a": "SHITTIM_PERSONA_PARTICIPANT_A_JSON",
        "participant-b": "SHITTIM_PERSONA_PARTICIPANT_B_JSON",
        "participant-c": "SHITTIM_PERSONA_PARTICIPANT_C_JSON",
    }
    for slot, name in persona_env.items():
        values[name] = json.dumps(
            {
                "schema_version": "1",
                "config_version": "v0001",
                "slot": slot,
                "display_name": f"Generic {slot}",
                "system_prompt": f"Generic instructions for {slot}.",
            }
        )
    return values
