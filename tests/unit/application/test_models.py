"""Application persistence-model invariants."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.application import DebateSnapshot, PanelRefreshState
from shittim_chest.domain import AttemptId, DebateId, DebateState

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def snapshot() -> DebateSnapshot:
    return DebateSnapshot(
        state=DebateState.accepted(DebateId.new(), AttemptId.new(), at=NOW),
        question="question",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="channel",
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="starter",
        thread_id="thread",
        control_panel_message_id="panel",
    )


def test_panel_refresh_state_is_derived_from_durable_delivery_fields() -> None:
    not_required = snapshot()
    pending = replace(not_required, panel_refresh_required_at=NOW)
    delivered = replace(pending, panel_refreshed_at=NOW)
    abandoned = replace(
        pending,
        panel_refresh_delivery_attempt=1,
        panel_refresh_failed_at=NOW + timedelta(seconds=1),
        panel_refresh_error_code="discord_permission_denied",
    )

    assert not_required.panel_refresh_state is PanelRefreshState.NOT_REQUIRED
    assert pending.panel_refresh_state is PanelRefreshState.PENDING
    assert pending.panel_refresh_pending is True
    assert delivered.panel_refresh_state is PanelRefreshState.DELIVERED
    assert abandoned.panel_refresh_state is PanelRefreshState.ABANDONED
    assert abandoned.panel_refresh_pending is False


def test_panel_refresh_failure_requires_timestamp_code_and_current_requirement() -> None:
    source = snapshot()

    with pytest.raises(ValueError, match="timestamp and error code"):
        replace(source, panel_refresh_failed_at=NOW)
    with pytest.raises(ValueError, match="timestamp and error code"):
        replace(source, panel_refresh_error_code="discord_forbidden")
    with pytest.raises(ValueError, match="requires a requirement"):
        replace(
            source,
            panel_refresh_failed_at=NOW,
            panel_refresh_error_code="discord_forbidden",
        )


def test_panel_refresh_failure_cannot_precede_or_conflict_with_delivery() -> None:
    required_at = NOW + timedelta(seconds=2)
    pending = replace(snapshot(), panel_refresh_required_at=required_at)

    with pytest.raises(ValueError, match="cannot precede"):
        replace(
            pending,
            panel_refresh_failed_at=NOW + timedelta(seconds=1),
            panel_refresh_error_code="discord_forbidden",
        )
    with pytest.raises(ValueError, match="cannot also be abandoned"):
        replace(
            pending,
            panel_refreshed_at=required_at,
            panel_refresh_failed_at=required_at + timedelta(seconds=1),
            panel_refresh_error_code="discord_forbidden",
        )


def test_abandoned_panel_refresh_cannot_retain_claim_or_retry_state() -> None:
    failed_at = NOW + timedelta(seconds=1)
    abandoned = replace(
        snapshot(),
        panel_refresh_required_at=NOW,
        panel_refresh_delivery_attempt=1,
        panel_refresh_failed_at=failed_at,
        panel_refresh_error_code="discord_forbidden",
    )

    with pytest.raises(ValueError, match="cannot retain retry or claim"):
        replace(
            abandoned,
            panel_refresh_claim_owner="worker",
            panel_refresh_claim_expires_at=failed_at + timedelta(seconds=60),
        )
    with pytest.raises(ValueError, match="cannot retain retry or claim"):
        replace(
            abandoned,
            panel_refresh_next_attempt_at=failed_at + timedelta(seconds=30),
        )
