"""Tests for Grype full/actionable report separation."""

from __future__ import annotations

import pytest
from tools.report_grype import image_inventory, summarize_report


def _match(identifier: str, severity: str, fix_state: str) -> dict[str, object]:
    return {
        "vulnerability": {
            "id": identifier,
            "severity": severity,
            "fix": {"state": fix_state},
        }
    }


def test_summary_keeps_unfixed_in_full_and_fixed_only_in_actionable() -> None:
    full: dict[str, object] = {
        "matches": [
            _match("CVE-2026-10000", "Critical", "not-fixed"),
            _match("CVE-2026-10001", "High", "fixed"),
        ],
        "ignoredMatches": [_match("CVE-2026-10002", "Low", "not-fixed")],
    }
    actionable: dict[str, object] = {"matches": [_match("CVE-2026-10001", "High", "fixed")]}

    assert summarize_report(full, actionable, "image") == {
        "active_findings": 2,
        "scanner_ignored_findings": 1,
        "observed_findings": 3,
        "unique_vulnerabilities": 2,
        "severity": {"Critical": 1, "High": 1},
        "fix_state": {"fixed": 1, "not-fixed": 1},
        "actionable_findings": 1,
        "actionable_high_critical": 1,
    }


def test_actionable_report_rejects_unfixed_finding() -> None:
    report: dict[str, object] = {
        "matches": [_match("CVE-2026-10000", "High", "wont-fix")],
        "ignoredMatches": [],
    }

    with pytest.raises(ValueError, match="non-fixable"):
        summarize_report(report, report, "image")


def test_image_inventory_counts_debian_purls() -> None:
    spdx: dict[str, object] = {
        "packages": [
            {
                "externalRefs": [
                    {"referenceType": "purl", "referenceLocator": "pkg:deb/debian/libc6@1"}
                ]
            },
            {"externalRefs": [{"referenceType": "purl", "referenceLocator": "pkg:pypi/httpx@1"}]},
        ]
    }

    assert image_inventory(spdx, 123) == {
        "size_bytes": 123,
        "packages": 2,
        "debian_packages": 1,
    }
