from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest

from shittim_chest.adapters.discord.bootstrap_api import (
    DiscordBootstrapInspection,
    DiscordBootstrapService,
)
from shittim_chest.adapters.discord.command_schema import (
    CommandInventoryState,
    command_schema_hash,
)
from shittim_chest.lambda_handlers.discord_bootstrap import DiscordBootstrapLambda

COMMIT = "a" * 40


@dataclass(slots=True)
class FakeService:
    state: CommandInventoryState = CommandInventoryState.EMPTY
    endpoint: bool = False
    endpoint_calls: int = 0
    command_calls: int = 0

    def inspect(self) -> DiscordBootstrapInspection:
        return self._inspection()

    def reconcile_endpoint(self) -> bool:
        self.endpoint_calls += 1
        changed = not self.endpoint
        self.endpoint = True
        return changed

    def assess_command(self) -> CommandInventoryState:
        if not self.endpoint:
            raise ValueError("endpoint missing")
        return self.state

    def reconcile_command(self) -> bool:
        self.command_calls += 1
        changed = self.state is not CommandInventoryState.MATCH
        self.state = CommandInventoryState.MATCH
        return changed

    def verify(self) -> DiscordBootstrapInspection:
        if not self.endpoint or self.state is not CommandInventoryState.MATCH:
            raise ValueError("not verified")
        return self._inspection()

    def _inspection(self) -> DiscordBootstrapInspection:
        return DiscordBootstrapInspection(
            endpoint_sha256="b" * 64 if self.endpoint else None,
            endpoint_matches=self.endpoint,
            global_command_count=0,
            guild_command_count=0 if self.state is CommandInventoryState.EMPTY else 1,
            command_state=self.state,
            channel_count=1,
        )


def event(operation: str, acknowledgement: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": operation,
        "expected_commit_sha": COMMIT,
        "expected_command_schema_hash": command_schema_hash(),
        "acknowledge_write": acknowledgement,
    }


def handler(fake: FakeService) -> DiscordBootstrapLambda:
    return DiscordBootstrapLambda(
        service=cast(DiscordBootstrapService, fake),
        expected_commit_sha=COMMIT,
        expected_command_schema_hash=command_schema_hash(),
    )


def test_operations_enforce_activation_order_and_return_content_free_evidence() -> None:
    fake = FakeService()
    current = handler(fake)

    assert current.handle(event("inspect"))["status"] == "PASS"
    endpoint = current.handle(event("reconcile_endpoint", "reconcile-discord-endpoint"))
    assert endpoint["status"] == "CHANGED"
    assert current.handle(event("assess_command"))["command_state"] == "empty"
    command = current.handle(event("reconcile_command", "reconcile-discord-command"))
    assert command["status"] == "CHANGED"
    result = current.handle(event("verify"))
    assert result["status"] == "PASS"
    assert fake.endpoint_calls == 1
    assert fake.command_calls == 1
    assert set(result) == {
        "schema_version",
        "operation",
        "status",
        "expected_commit_sha",
        "application_match",
        "guild_access",
        "channel_count",
        "endpoint_match",
        "endpoint_sha256",
        "global_command_count",
        "guild_command_count",
        "command_schema_hash",
        "command_state",
        "endpoint_changed",
        "command_changed",
    }


@pytest.mark.parametrize(
    "operation",
    ("reconcile_endpoint", "reconcile_command"),
)
def test_write_operations_require_the_exact_acknowledgement(operation: str) -> None:
    fake = FakeService(endpoint=True)
    with pytest.raises(ValueError, match="acknowledgement"):
        handler(fake).handle(event(operation))
    assert fake.endpoint_calls == 0
    assert fake.command_calls == 0


def test_request_identity_mismatch_fails_before_service_call() -> None:
    fake = FakeService()
    value = event("inspect")
    value["expected_commit_sha"] = "b" * 40
    with pytest.raises(ValueError, match="identity"):
        handler(fake).handle(value)
    assert fake.endpoint_calls == 0
    assert fake.command_calls == 0
