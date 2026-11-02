"""Regression tests for the fail-closed npm audit report gate."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
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


def test_complete_clean_report_passes(tmp_path: Path) -> None:
    result = _run(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {},
            "metadata": {"vulnerabilities": {"total": 0}},
        },
        exceptions_path=_write_exceptions(tmp_path, []),
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


def _transitive_report(
    *,
    leaf_via: list[object] | None = None,
    parent_via: list[object] | None = None,
) -> object:
    advisory = {
        "title": "test advisory",
        "url": "https://github.com/advisories/GHSA-test-audit-entry",
        "severity": "high",
    }
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {
            "cdk-nag": {"via": ["aws-cdk-lib"]},
            "aws-cdk-lib": {"via": ["minimatch"] if parent_via is None else parent_via},
            "minimatch": {"via": [advisory] if leaf_via is None else leaf_via},
        },
        "metadata": {"vulnerabilities": {"high": 3, "total": 3}},
    }


def test_transitive_references_use_one_leaf_package_exception(tmp_path: Path) -> None:
    result = _run(
        _transitive_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(package="minimatch")]),
    )

    assert result.returncode == 0
    assert "1 dated exception(s) active" in result.stdout


@pytest.mark.parametrize("package", ["aws-cdk-lib", "cdk-nag"])
def test_transitive_exception_cannot_bind_to_a_parent(tmp_path: Path, package: str) -> None:
    result = _run(
        _transitive_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(package=package)]),
    )

    assert result.returncode == 1
    assert f"exception package {package} did not match minimatch" in result.stderr


@pytest.mark.parametrize(
    "report,message",
    [
        (_transitive_report(parent_via=["missing"]), "missing npm audit via reference"),
        (_transitive_report(parent_via=["cdk-nag"]), "cyclic npm audit via reference"),
        (_transitive_report(leaf_via=[]), "no advisory metadata"),
        (_transitive_report(leaf_via=[{}]), "invalid npm audit advisory metadata"),
        (
            _transitive_report(leaf_via=[{"url": "", "title": "test", "severity": "high"}]),
            "invalid npm audit advisory metadata",
        ),
    ],
)
def test_invalid_transitive_metadata_fails_closed(
    tmp_path: Path, report: object, message: str
) -> None:
    result = _run(
        report,
        exceptions_path=_write_exceptions(tmp_path, [_exception(package="minimatch")]),
    )

    assert result.returncode == 1
    assert message in result.stderr


def test_transitive_known_advisory_does_not_cover_an_unknown_one(tmp_path: Path) -> None:
    result = _run(
        _transitive_report(
            leaf_via=[
                {
                    "url": f"https://github.com/advisories/{advisory}",
                    "title": "test advisory",
                    "severity": "high",
                }
                for advisory in ("GHSA-test-audit-entry", "GHSA-unknown-audit-entry")
            ]
        ),
        exceptions_path=_write_exceptions(tmp_path, [_exception(package="minimatch")]),
    )

    assert result.returncode == 1
    assert "GHSA-unknown-audit-entry" in result.stderr


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


def test_exception_expiring_today_fails_closed(tmp_path: Path) -> None:
    expires = datetime.now(UTC).date().isoformat()
    result = _run(
        _vulnerable_report(),
        exceptions_path=_write_exceptions(tmp_path, [_exception(expires=expires)]),
    )

    assert result.returncode == 1
    assert f"exception expired on {expires}" in result.stderr


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
