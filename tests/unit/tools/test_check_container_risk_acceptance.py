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
    validate_config_digest_bindings,
)

DIGEST = "sha256:" + "a" * 64
BREAK_GLASS_DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "c" * 64
TODAY = dt.date(2026, 7, 22)
FINDING = Finding(FindingKey("CVE-2026-12345", "libexample"), "High", "not-fixed")


def _write_policy(
    path: Path,
    acceptances: list[dict[str, object]],
    *,
    schema_version: int = 4,
    extra_root: dict[str, object] | None = None,
) -> Path:
    root: dict[str, object] = {
        "schema_version": schema_version,
        "maximum_validity_days": 90,
        "acceptances": acceptances,
    }
    if extra_root is not None:
        root.update(extra_root)
    path.write_text(
        json.dumps(root),
        encoding="utf-8",
    )
    return path


def _acceptance(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "vulnerability_id": "CVE-2026-12345",
        "package": "libexample",
        "image_config_digests": {"production": DIGEST},
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
        image_kind="production",
        image_config_digest=DIGEST,
        today=TODAY,
    ) == (0, 1)


def test_config_digest_preflight_accepts_the_exact_loaded_image(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [_acceptance()])

    assert (
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )
        == 1
    )


def test_schema_v4_empty_policy_accepts_any_valid_production_digest(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert (
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )
        == 0
    )


def test_empty_policy_has_no_static_production_digest_baseline(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert (
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=OTHER_DIGEST,
            today=TODAY,
        )
        == 0
    )


def test_empty_policy_has_no_static_break_glass_digest_baseline(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert (
        validate_config_digest_bindings(
            policy,
            image_kind="break-glass",
            image_config_digest=OTHER_DIGEST,
            today=TODAY,
        )
        == 0
    )


def test_policy_rejects_removed_static_baseline_field(tmp_path: Path) -> None:
    policy = _write_policy(
        tmp_path / "policy.json",
        [],
        extra_root={"image_config_digests": {"production": DIGEST}},
    )

    with pytest.raises(ValueError, match="unexpected root fields"):
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_policy_rejects_extra_root_field(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [], extra_root={"unexpected": True})

    with pytest.raises(ValueError, match="unexpected root fields"):
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_policy_rejects_legacy_schema(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [], schema_version=3)

    with pytest.raises(ValueError, match="version"):
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_config_digest_preflight_blocks_before_push_on_exporter_drift(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [_acceptance()])

    with pytest.raises(ValueError, match="does not match"):
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=OTHER_DIGEST,
            today=TODAY,
        )


def test_config_digest_preflight_rejects_expired_policy_before_push(tmp_path: Path) -> None:
    policy = _write_policy(
        tmp_path / "policy.json",
        [_acceptance(expires_on="2026-07-21")],
    )

    with pytest.raises(ValueError, match="expired"):
        validate_config_digest_bindings(
            policy,
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_full_validation_with_empty_acceptances_has_no_static_baseline(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert validate_acceptances(
        policy,
        findings=(),
        vendor_suppressions=frozenset(),
        image_kind="production",
        image_config_digest=OTHER_DIGEST,
        today=TODAY,
    ) == (0, 0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"image_config_digests": {"production": OTHER_DIGEST}}, "does not match"),
        ({"image_config_digests": {"unknown": DIGEST}}, "unsupported"),
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
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_unrecorded_residual_finding_fails(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    with pytest.raises(ValueError, match="untracked"):
        validate_acceptances(
            policy,
            findings=(FINDING,),
            vendor_suppressions=frozenset(),
            image_kind="production",
            image_config_digest=DIGEST,
            today=TODAY,
        )


def test_verified_vendor_vex_suppression_needs_no_local_acceptance(tmp_path: Path) -> None:
    policy = _write_policy(tmp_path / "policy.json", [])

    assert validate_acceptances(
        policy,
        findings=(FINDING,),
        vendor_suppressions=frozenset({FINDING.key}),
        image_kind="production",
        image_config_digest=DIGEST,
        today=TODAY,
    ) == (1, 0)


def test_acceptances_are_scoped_to_one_image_kind(tmp_path: Path) -> None:
    policy = _write_policy(
        tmp_path / "policy.json",
        [
            _acceptance(
                image_config_digests={
                    "production": DIGEST,
                    "break-glass": BREAK_GLASS_DIGEST,
                }
            ),
        ],
    )

    assert validate_acceptances(
        policy,
        findings=(FINDING,),
        vendor_suppressions=frozenset(),
        image_kind="production",
        image_config_digest=DIGEST,
        today=TODAY,
    ) == (0, 1)
    assert validate_acceptances(
        policy,
        findings=(FINDING,),
        vendor_suppressions=frozenset(),
        image_kind="break-glass",
        image_config_digest=BREAK_GLASS_DIGEST,
        today=TODAY,
    ) == (0, 1)


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
