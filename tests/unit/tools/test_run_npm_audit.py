"""Tests for the outage-aware npm audit runner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tools import run_npm_audit


def _summary(*, component_status: str, incident_status: str | None = None) -> dict[str, object]:
    incidents: list[object] = []
    if incident_status is not None:
        incidents.append(
            {
                "status": incident_status,
                "components": [
                    {
                        "id": "security-audit",
                        "name": "Security Audit",
                        "status": component_status,
                    }
                ],
            }
        )
    return {
        "components": [
            {
                "id": "security-audit",
                "name": "Security Audit",
                "status": component_status,
            }
        ],
        "incidents": incidents,
    }


def test_operational_status_runs_audit_and_propagates_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_npm_audit, "_fetch_summary", lambda: _summary(component_status="operational")
    )
    observed: list[list[str]] = []

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        observed.append(command)
        assert check is False
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(run_npm_audit.subprocess, "run", run)

    assert run_npm_audit.main(["--", "npm", "audit"]) == 7
    assert observed == [["npm", "audit"]]


def test_confirmed_active_incident_skips_only_the_external_audit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        run_npm_audit,
        "_fetch_summary",
        lambda: _summary(component_status="degraded_performance", incident_status="investigating"),
    )
    monkeypatch.setattr(
        run_npm_audit.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("audit command must not run during the incident"),
    )

    assert run_npm_audit.main(["--", "pnpm", "audit", "--audit-level=low"]) == 0
    assert "Official npm status confirms" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (_summary(component_status="degraded_performance"), "without a matching active incident"),
        (_summary(component_status="maintenance"), "status was not recognized"),
        ({"components": [], "incidents": []}, "was not uniquely identified"),
    ),
)
def test_ambiguous_status_refuses_to_skip(payload: object, message: str) -> None:
    with pytest.raises(run_npm_audit.NpmAuditStatusError, match=message):
        run_npm_audit.official_security_audit_outage(payload)


def test_status_fetch_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> object:
        raise OSError("offline")

    monkeypatch.setattr(run_npm_audit, "_fetch_summary", fail)
    monkeypatch.setattr(
        run_npm_audit.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("audit command must not run without status evidence"),
    )

    assert run_npm_audit.main(["--", "npm", "audit"]) == 1
    assert "refusing to skip" in capsys.readouterr().err


def test_command_separator_is_required() -> None:
    assert run_npm_audit.main(["npm", "audit"]) == 2
