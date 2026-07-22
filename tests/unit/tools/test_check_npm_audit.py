"""Regression tests for the fail-closed npm audit report gate."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPOSITORY_ROOT / "infra" / "check-audit.mjs"
NODE = shutil.which("node")
if NODE is None:
    raise RuntimeError("Node.js is required by the infrastructure test suite")


def _run(report: object) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(  # noqa: S603 - fixed local Node script.
        (NODE, str(CHECKER)),
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
