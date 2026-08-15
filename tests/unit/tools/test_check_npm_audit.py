"""Regression tests for the fail-closed npm audit report gate."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPOSITORY_ROOT / "infra" / "check-audit.mjs"
NODE = shutil.which("node")
if NODE is None:
    raise RuntimeError("Node.js is required by the infrastructure test suite")


def _run(
    report: object,
    *,
    exceptions_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    command = [NODE, str(CHECKER)]
    if exceptions_path is not None:
        command.append(str(exceptions_path))
    return subprocess.run(  # noqa: S603 - fixed local Node script.
        command,
        input=json.dumps(report),
        text=True,
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
    )


def test_complete_clean_report_passes() -> None:
    result = _run(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {},
            "metadata": {"vulnerabilities": {"total": 0}},
        }
    )

    assert result.returncode == 0
    assert "npm audit: clean" in result.stdout


def test_registry_error_report_fails_closed() -> None:
    result = _run({"error": {"code": "EAI_AGAIN", "summary": "registry unavailable"}})

    assert result.returncode == 1
    assert "reported an error" in result.stderr


def test_incomplete_report_fails_closed() -> None:
    result = _run({"auditReportVersion": 2, "vulnerabilities": {}})

    assert result.returncode == 1
    assert "incomplete" in result.stderr


def _vulnerable_report(*, package: str = "brace-expansion", severity: str = "high") -> object:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            package: {
                "severity": severity,
                "via": [
                    {
                        "title": "test advisory",
                        "url": "https://github.com/advisories/GHSA-test-audit-entry",
                        "severity": severity,
                    }
                ],
            }
        },
        "metadata": {"vulnerabilities": {severity: 1, "total": 1}},
    }


def _write_exceptions(tmp_path: Path, entries: list[dict[str, str]]) -> Path:
    path = tmp_path / "npm-audit-exceptions.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _exception(**overrides: str) -> dict[str, str]:
    entry = {
        "id": "GHSA-test-audit-entry",
        "package": "brace-expansion",
        "severity": "high",
        "reason": "bounded test-only exception",
        "expires": "2999-01-01",
    }
    entry.update(overrides)
    return entry


def test_matching_package_and_severity_exception_passes(tmp_path: Path) -> None:
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception()]),
    )

    assert result.returncode == 0
    assert "1 dated exception(s) active" in result.stdout


def test_exception_package_mismatch_fails_closed(tmp_path: Path) -> None:
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(package="minimatch")]),
    )

    assert result.returncode == 1
    assert "exception package minimatch did not match brace-expansion" in result.stderr


def test_exception_severity_mismatch_fails_closed(tmp_path: Path) -> None:
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(severity="moderate")]),
    )

    assert result.returncode == 1
    assert "exception severity moderate did not match high" in result.stderr


def test_exception_unknown_field_fails_closed(tmp_path: Path) -> None:
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(owner="nobody")]),
    )

    assert result.returncode == 1
    assert "unknown: owner" in result.stderr


@pytest.mark.parametrize("expires", ["2026-99-99", "2026-02-29"])
def test_exception_nonexistent_calendar_date_fails_closed(
    tmp_path: Path,
    expires: str,
) -> None:
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(expires=expires)]),
    )

    assert result.returncode == 1
    assert "invalid expires" in result.stderr


def test_unused_exception_fails_closed(tmp_path: Path) -> None:
    result = _run(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {},
            "metadata": {"vulnerabilities": {"total": 0}},
        },
        exceptions_path=_write_exceptions(tmp_path, [_exception()]),
    )

    assert result.returncode == 1
    assert "unused npm audit exception" in result.stderr
