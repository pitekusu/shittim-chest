"""Fail-closed control-record CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tools import control_records

from shittim_chest.adapters.dynamodb.deployment_guard import (
    DeploymentGuardUnavailable,
    DeploymentLockAcquisition,
)
from shittim_chest.application.deployment_guard import (
    DeploymentGuardAssessment,
    DeploymentGuardCode,
    DeploymentGuardContext,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
)
from shittim_chest.application.scale_to_zero import RuntimeStatus

CONTEXT_ARGS = (
    "--commit-sha",
    "a" * 40,
    "--actor",
    "pitekusu",
    "--run-id",
    "123456",
    "--environment",
    "production",
)
GUARD_ID = "019d2c1f-0000-7000-8000-a00000000007"


class UnavailableGuard:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def guard(self, **_kwargs: object) -> None:
        raise DeploymentGuardUnavailable("provider detail must not escape")


def test_guard_read_failure_writes_content_free_owner_only_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "guard.json"
    monkeypatch.setattr(
        control_records, "create_control_records_dynamodb_client", lambda **_: object()
    )
    monkeypatch.setattr(control_records, "DynamoDbDeploymentGuard", UnavailableGuard)

    result = control_records.main(
        (
            "guard",
            "--table-name",
            "private-table-name",
            "--region",
            "ap-northeast-1",
            *CONTEXT_ARGS,
            "--audit-output",
            str(output),
        )
    )

    assert result == 3
    assert output.stat().st_mode & 0o777 == 0o600
    audit = json.loads(output.read_text(encoding="utf-8"))
    assert audit["allowed"] is False
    assert audit["code"] == "snapshot_unavailable"
    encoded = json.dumps(audit)
    assert "private-table-name" not in encoded
    assert "provider detail" not in encoded
    assert "ap-northeast-1" not in encoded
    assert json.loads(capsys.readouterr().out)["code"] == "snapshot_unavailable"


def test_guard_rejects_break_glass_without_reason_before_sdk_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "guard.json"

    def unexpected_client(**_kwargs: object) -> object:
        raise AssertionError("malformed context must not reach the SDK")

    monkeypatch.setattr(
        control_records, "create_control_records_dynamodb_client", unexpected_client
    )

    result = control_records.main(
        (
            "guard",
            "--table-name",
            "table",
            "--region",
            "ap-northeast-1",
            *CONTEXT_ARGS,
            "--break-glass",
            "--audit-output",
            str(output),
        )
    )

    assert result == 3
    assert json.loads(output.read_text(encoding="utf-8"))["code"] == "snapshot_unavailable"


def test_initialize_requires_exact_write_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        control_records,
        "create_control_records_dynamodb_client",
        lambda **_: pytest.fail("unacknowledged initialize must not create an SDK client"),
    )

    result = control_records.main(
        (
            "initialize",
            "--table-name",
            "table",
            "--region",
            "ap-northeast-1",
            "--acknowledge-write",
            "yes",
        )
    )

    assert result == 2


def test_acquire_cli_replay_uses_stored_times_across_separate_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_now = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
    retry_now = first_now + timedelta(minutes=2)
    current_times = iter((first_now, retry_now))
    received_times: list[tuple[datetime, datetime]] = []
    stored: list[DeploymentLockAcquisition] = []

    class SequencedDatetime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            assert tz is UTC
            return next(current_times)

    class ReplayGuard:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def acquire(
            self,
            *,
            context: DeploymentGuardContext,
            guard_id: str,
            acquired_at: datetime,
            expires_at: datetime,
        ) -> DeploymentLockAcquisition:
            received_times.append((acquired_at, expires_at))
            if not stored:
                assessment = DeploymentGuardAssessment(
                    allowed=True,
                    code=DeploymentGuardCode.SAFE,
                    context=context,
                    evaluated_at=acquired_at,
                    runtime_status=RuntimeStatus.STOPPED,
                    runtime_generation=0,
                    runtime_version=0,
                    activity_clear=True,
                    deployment_lock_state=DeploymentLockState.OPEN,
                    deployment_lock_fencing_token=0,
                )
                lock = DeploymentLock(
                    state=DeploymentLockState.LOCKED,
                    fencing_token=1,
                    version=1,
                    updated_at=acquired_at,
                    guard_id=guard_id,
                    owner=context.actor,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                    mode=DeploymentMode.NORMAL,
                )
                stored.append(
                    DeploymentLockAcquisition(
                        assessment=assessment,
                        lock=lock,
                        audit_item={},
                    )
                )
            return stored[0]

    monkeypatch.setattr(control_records, "datetime", SequencedDatetime)
    monkeypatch.setattr(
        control_records, "create_control_records_dynamodb_client", lambda **_: object()
    )
    monkeypatch.setattr(control_records, "DynamoDbDeploymentGuard", ReplayGuard)
    first_output = tmp_path / "first-acquire.json"
    retry_output = tmp_path / "retry-acquire.json"

    def invoke(output: Path) -> int:
        return control_records.main(
            (
                "acquire",
                "--table-name",
                "table",
                "--region",
                "ap-northeast-1",
                *CONTEXT_ARGS,
                "--guard-id",
                GUARD_ID,
                "--lock-seconds",
                "900",
                "--acknowledge-write",
                control_records.ACQUIRE_ACKNOWLEDGEMENT,
                "--audit-output",
                str(output),
            )
        )

    assert invoke(first_output) == 0
    assert invoke(retry_output) == 0
    assert received_times == [
        (first_now, first_now + timedelta(minutes=15)),
        (retry_now, retry_now + timedelta(minutes=15)),
    ]
    first_audit = json.loads(first_output.read_text(encoding="utf-8"))
    retry_audit = json.loads(retry_output.read_text(encoding="utf-8"))
    assert retry_audit == first_audit
    assert retry_audit["evaluated_at"] == "2026-07-28T09:00:00.000000Z"
    assert retry_audit["lock_expires_at"] == "2026-07-28T09:15:00.000000Z"


def test_audit_output_refuses_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("safe", encoding="utf-8")
    link = tmp_path / "audit.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        control_records._write_audit(link, {"allowed": False})

    assert target.read_text(encoding="utf-8") == "safe"
