"""Tests for secret-scanner contract helpers."""

import json
from pathlib import Path

import pytest
from tools.check_public_surface import DENY_PATTERNS
from tools.check_secret_scanners import report_finding_count, synthetic_secret


def test_synthetic_secret_is_generated_but_detectable() -> None:
    secret = synthetic_secret()

    assert secret.startswith("ghp_")
    assert len(secret) == 40
    assert DENY_PATTERNS["GitHub token"].fullmatch(secret.encode())


def test_report_finding_count_accepts_array(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{"redacted": True}, {"redacted": True}]), encoding="utf-8")

    assert report_finding_count(report) == 2


def test_report_finding_count_accepts_null_as_no_findings(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("null\n", encoding="utf-8")

    assert report_finding_count(report) == 0


def test_report_finding_count_rejects_object(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="root must be an array"):
        report_finding_count(report)
