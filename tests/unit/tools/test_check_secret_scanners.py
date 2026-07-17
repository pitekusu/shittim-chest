"""Tests for secret-scanner contract helpers."""

import json
from pathlib import Path

import pytest
from tools.check_public_surface import DENY_PATTERNS
from tools.check_secret_scanners import report_finding_count, synthetic_marker


def test_synthetic_marker_is_generated_but_detectable() -> None:
    marker = synthetic_marker()

    assert marker.startswith("ghp_")
    assert len(marker) == 40
    assert DENY_PATTERNS["GitHub token"].fullmatch(marker.encode())


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
