"""Tests for digest-bound, expiring container vulnerability acceptances."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from tools.check_container_risk_acceptance import (
    Finding,
    FindingKey,
    load_vendor_vex_suppressions,
    validate_acceptances,
)

DIGEST = "sha256:" + "a" * 64
TODAY = dt.date(2026, 7, 22)
FINDING = Finding(FindingKey("CVE-2026-12345", "libexample"), "High", "not-fixed")


def _write_policy(path: Path, acceptances: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "maximum_validity_days": 90,
                "acceptances": acceptances,
            }
        ),
        encoding="utf-8",
    )
    return path


def _acceptance(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "vulnerability_id": "CVE-2026-12345",
        "package": "libexample",
        "image_digest": DIGEST,
        "status": "under_investigation",
        "justification": "No upstream fix is available; deployment remains monitored.",
        "impact": "A successful exploit could affect the isolated application process.",
        "exploitability": "No shell, no ingress, and read-only root reduce but do not remove risk.",
        "evidence": "Grype report and DHI package inventory were reviewed for this digest.",
        "approved_on": "2026-07-22",
        "expires_on": "2026-08-21",
        "reevaluation_conditions": (
            "Reevaluate on a new image digest, fix release, or exposure change."
        ),
        "owner": "security-owner",
    }
    value.update(overrides)
    return value


def test_current_digest_bound_acceptance_covers_residual_finding(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [_acceptance()])

    assert validate_acceptances(
        policy,
        findings=(FINDING,),
        vendor_suppressions=frozenset(),
        image_digest=DIGEST,
        today=TODAY,
    ) == (0, 1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image_digest": "sha256:" + "b" * 64}, "does not match"),
        ({"expires_on": "2026-07-21"}, "expired"),
        ({"expires_on": "2026-12-31"}, "within 90 days"),
        ({"status": "not_affected"}, "must not claim"),
        ({"evidence": "guess"}, "concrete evidence"),
    ],
)
def test_invalid_acceptance_fails_closed(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    policy = _write_policy(tmp_path / "policy.json", [_acceptance(**overrides)])

    with pytest.raises(ValueError, match=message):
        validate_acceptances(
            policy,
            findings=(FINDING,),
            vendor_suppressions=frozenset(),
            image_digest=DIGEST,
            today=TODAY,
        )


def test_unrecorded_residual_finding_fails(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    with pytest.raises(ValueError, match="untracked"):
        validate_acceptances(
            policy,
            findings=(FINDING,),
            vendor_suppressions=frozenset(),
            image_digest=DIGEST,
            today=TODAY,
        )


def test_verified_vendor_vex_suppression_needs_no_local_acceptance(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert validate_acceptances(
        policy,
        findings=(FINDING,),
        vendor_suppressions=frozenset({FINDING.key}),
        image_digest=DIGEST,
        today=TODAY,
    ) == (1, 0)


def test_only_explicit_vex_rules_are_recognized(tmp_path: Path) -> None:
    report = {
        "ignoredMatches": [
            {
                "vulnerability": {
                    "id": "CVE-2026-12345",
                    "severity": "High",
                    "fix": {"state": "not-fixed"},
                },
                "artifact": {"name": "libexample"},
                "appliedIgnoreRules": [{"vex-status": "not_affected"}],
            }
        ]
    }
    path = tmp_path / "vex-report.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    assert load_vendor_vex_suppressions(path) == frozenset({FINDING.key})
