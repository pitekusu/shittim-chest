"""Tests for the Grype severity gate and GitHub Actions summary rendering."""

import json
from pathlib import Path

import pytest
from tools.report_grype import (
    MAX_ANNOTATIONS_PER_KIND,
    emit_annotations,
    load_findings,
    main,
    write_summary,
)


def _match(
    vulnerability_id: str,
    severity: str,
    package: str,
    version: str,
    fixed: list[str] | None = None,
) -> dict[str, object]:
    return {
        "vulnerability": {
            "id": vulnerability_id,
            "severity": severity,
            "fix": {"versions": fixed or []},
        },
        "artifact": {"name": package, "version": version, "type": "python"},
    }


def _write_report(path: Path, matches: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"matches": matches}), encoding="utf-8")


def test_load_findings_normalizes_severity_and_fix_versions(tmp_path: Path) -> None:
    report = tmp_path / "grype.json"
    _write_report(
        report,
        [
            _match("CVE-2026-0001", "High", "urllib3", "2.5.0", ["2.6.0"]),
            _match("CVE-2026-0002", "Supersonic", "wheel", "0.45.1"),
        ],
    )

    findings = load_findings(report, "source SBOM")

    assert [(f.severity, f.vulnerability_id) for f in findings] == [
        ("high", "CVE-2026-0001"),
        ("unknown", "CVE-2026-0002"),
    ]
    assert findings[0].fixed_versions == ("2.6.0",)
    assert findings[1].fixed_versions == ()


def test_load_findings_rejects_missing_matches(tmp_path: Path) -> None:
    report = tmp_path / "grype.json"
    report.write_text(json.dumps({"descriptor": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="no matches list"):
        load_findings(report, "source SBOM")


def test_load_findings_rejects_invalid_json(tmp_path: Path) -> None:
    report = tmp_path / "grype.json"
    report.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid grype report JSON"):
        load_findings(report, "source SBOM")


def test_load_findings_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular file"):
        load_findings(tmp_path / "missing.json", "source SBOM")


def test_main_passes_with_medium_and_low_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "grype.json"
    _write_report(
        report,
        [
            _match("CVE-2026-1000", "Medium", "aiohttp", "3.12.15"),
            _match("CVE-2026-1001", "Low", "certifi", "2026.4.26"),
            _match("CVE-2026-1002", "Negligible", "idna", "3.10"),
        ],
    )

    result = main(["--target", "source SBOM", str(report)])

    assert result == 0
    output = capsys.readouterr().out
    assert output.count("::warning::") == 2
    assert "::error::" not in output
    assert "PASSED" in output


def test_main_fails_on_high_and_annotates_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "grype.json"
    _write_report(
        report,
        [
            _match("CVE-2026-2000", "Critical", "openssl", "3.0.0", ["3.0.1"]),
            _match("CVE-2026-2001", "Medium", "aiohttp", "3.12.15"),
        ],
    )

    result = main(["--target", "image SBOM (arm64)", str(report)])

    assert result == 1
    output = capsys.readouterr().out
    assert "::error::grype: CVE-2026-2000 " in output
    assert "3.0.0 (critical) in image SBOM (arm64)" in output
    assert output.count("::warning::") == 1
    assert "FAILED (1 high-or-above finding(s))" in output


def test_annotations_are_capped(capsys: pytest.CaptureFixture[str]) -> None:
    from tools.report_grype import Finding

    findings = tuple(
        Finding(
            target="t",
            vulnerability_id=f"CVE-2026-9{i:03d}",
            severity="medium",
            package_name="pkg",
            package_version="1.0",
            fixed_versions=(),
        )
        for i in range(MAX_ANNOTATIONS_PER_KIND + 3)
    )

    emit_annotations(findings)

    output = capsys.readouterr().out
    assert output.count("::warning::") == MAX_ANNOTATIONS_PER_KIND + 1
    assert "3 additional grype finding(s)" in output


def test_write_summary_renders_counts_and_blocking_table(tmp_path: Path) -> None:
    report = tmp_path / "grype.json"
    _write_report(
        report,
        [
            _match("CVE-2026-3000", "High", "openssl", "3.0.0", ["3.0.1"]),
            _match("CVE-2026-3001", "Medium", "aiohttp", "3.12.15"),
        ],
    )
    findings = load_findings(report, "source SBOM")
    summary = tmp_path / "summary" / "SUMMARY.md"

    write_summary((("source SBOM", findings),), summary)

    rendered = summary.read_text(encoding="utf-8")
    assert "| source SBOM | 0 | 1 | 1 | 0 | 0 | 0 | 2 |" in rendered
    assert "Gate: FAILED — 1 high-or-above finding(s)." in rendered
    assert "| high | CVE-2026-3000 | openssl | 3.0.0 | 3.0.1 | source SBOM |" in rendered


def test_write_summary_marks_clean_gate(tmp_path: Path) -> None:
    summary = tmp_path / "SUMMARY.md"

    write_summary((("source SBOM", ()),), summary)

    assert "Gate: PASSED — no high-or-above findings." in summary.read_text(encoding="utf-8")


def test_main_fails_closed_on_malformed_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = tmp_path / "grype.json"
    report.write_text("not json", encoding="utf-8")

    result = main(["--target", "source SBOM", str(report)])

    assert result == 1
    assert "grype report error" in capsys.readouterr().err
