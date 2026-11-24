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


@pytest.mark.parametrize("expires", ["2026-99-99", "2026-02-29", "2999-01-01T00:00:00"])
def test_exception_invalid_expiry_date_fails_closed(
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


def _pnpm_advisory(**overrides: object) -> dict[str, object]:
    advisory: dict[str, object] = {
        "id": 1247710,
        "module_name": "picomatch",
        "title": "ReDoS in repeated extglobs",
        "severity": "high",
        "github_advisory_id": "GHSA-fc5q-39xc-vjpf",
        "url": "https://github.com/advisories/GHSA-fc5q-39xc-vjpf",
        "patched_versions": None,
        "findings": [{"version": "4.0.7", "paths": [".>vitest>picomatch"], "dev": True}],
    }
    advisory.update(overrides)
    return advisory


def _pnpm_report(
    *,
    advisories: dict[str, object] | None = None,
    counts: dict[str, object] | None = None,
    **extra: object,
) -> object:
    return {
        "advisories": {"1247710": _pnpm_advisory()} if advisories is None else advisories,
        "metadata": {
            "vulnerabilities": {
                "info": 0,
                "low": 0,
                "moderate": 0,
                "high": 1,
                "critical": 0,
            }
            if counts is None
            else counts,
            "dependencies": 27,
            "devDependencies": 222,
            "totalDependencies": 249,
        },
        **extra,
    }


def _pnpm_exception(**overrides: str) -> dict[str, str]:
    return _exception(id="GHSA-fc5q-39xc-vjpf", package="picomatch", **overrides)


def test_pnpm_unpatched_advisory_uses_the_same_dated_gate(tmp_path: Path) -> None:
    result = _run(
        _pnpm_report(),
        exceptions_path=_write_exceptions(tmp_path, [_pnpm_exception()]),
    )

    assert result.returncode == 0
    assert "1 dated exception(s) active" in result.stdout


@pytest.mark.parametrize(
    "override,message",
    [
        ({"package": "vite-plus"}, "exception package vite-plus did not match picomatch"),
        ({"severity": "moderate"}, "exception severity moderate did not match high"),
        ({"expires": datetime.now(UTC).date().isoformat()}, "exception expired on"),
    ],
)
def test_pnpm_exception_scope_and_expiry_remain_enforced(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    entry = _pnpm_exception()
    entry.update(override)
    result = _run(
        _pnpm_report(),
        exceptions_path=_write_exceptions(tmp_path, [entry]),
    )

    assert result.returncode == 1
    assert message in result.stderr


def test_pnpm_known_advisory_does_not_cover_an_unknown_one(tmp_path: Path) -> None:
    result = _run(
        _pnpm_report(
            advisories={
                "1247710": _pnpm_advisory(),
                "1247711": _pnpm_advisory(
                    id=1247711,
                    github_advisory_id="GHSA-xxxx-yyyy-zzzz",
                    url="https://github.com/advisories/GHSA-xxxx-yyyy-zzzz",
                ),
            },
            counts={"info": 0, "low": 0, "moderate": 0, "high": 2, "critical": 0},
        ),
        exceptions_path=_write_exceptions(tmp_path, [_pnpm_exception()]),
    )

    assert result.returncode == 1
    assert "GHSA-xxxx-yyyy-zzzz" in result.stderr


@pytest.mark.parametrize(
    "override",
    [
        {"module_name": ""},
        {"severity": "unrecognized"},
        {"github_advisory_id": ""},
        {"url": "https://github.com/advisories/GHSA-xxxx-yyyy-zzzz"},
        {"id": 1247711},
    ],
)
def test_pnpm_incomplete_or_mismatched_identity_fails_closed(
    tmp_path: Path, override: dict[str, object]
) -> None:
    result = _run(
        _pnpm_report(advisories={"1247710": _pnpm_advisory(**override)}),
        exceptions_path=_write_exceptions(tmp_path, [_pnpm_exception()]),
    )

    assert result.returncode == 1
    assert "invalid pnpm audit advisory metadata" in result.stderr


@pytest.mark.parametrize(
    "report,message",
    [
        (
            _pnpm_report(advisories={}),
            "count did not match advisories",
        ),
        (
            _pnpm_report(counts={"high": 1}),
            "count did not match advisories",
        ),
        (
            _pnpm_report(ignoredAdvisories=[_pnpm_advisory()]),
            "incomplete or reported an error",
        ),
        (
            _pnpm_report(error={"code": "ERR_PNPM_AUDIT_BAD_RESPONSE"}),
            "incomplete or reported an error",
        ),
    ],
)
def test_pnpm_filtered_or_incomplete_report_fails_closed(
    tmp_path: Path, report: object, message: str
) -> None:
    result = _run(
        report,
        exceptions_path=_write_exceptions(tmp_path, [_pnpm_exception()]),
    )

    assert result.returncode == 1
    assert message in result.stderr


def test_clean_pnpm_report_rejects_an_unused_exception(tmp_path: Path) -> None:
    report = _pnpm_report(
        advisories={},
        counts={"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0},
    )
    unused = _run(report, exceptions_path=_write_exceptions(tmp_path, [_pnpm_exception()]))
    clean = _run(report, exceptions_path=_write_exceptions(tmp_path, []))

    assert unused.returncode == 1
    assert "unused npm audit exception" in unused.stderr
    assert clean.returncode == 0
