#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fail closed on stale or incomplete container vulnerability acceptances."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

MAX_JSON_BYTES: Final = 64 * 1024 * 1024
DIGEST_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
VULNERABILITY_PATTERN: Final = re.compile(
    r"^(?:CVE-[0-9]{4}-[0-9]{4,}|GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2})$"
)
TRACKED_SEVERITIES: Final = frozenset({"Critical", "High"})
UNFIXED_STATES: Final = frozenset({"", "unknown", "not-fixed", "wont-fix"})
VENDOR_VEX_STATUS: Final = "not_affected"
REQUIRED_ACCEPTANCE_FIELDS: Final = frozenset(
    {
        "vulnerability_id",
        "package",
        "image_digest",
        "status",
        "justification",
        "impact",
        "exploitability",
        "evidence",
        "approved_on",
        "expires_on",
        "reevaluation_conditions",
        "owner",
    }
)
TEXT_FIELDS: Final = (
    "justification",
    "impact",
    "exploitability",
    "evidence",
    "reevaluation_conditions",
)


@dataclass(frozen=True, slots=True, order=True)
class FindingKey:
    """A stable package vulnerability pair."""

    vulnerability_id: str
    package: str


@dataclass(frozen=True, slots=True)
class Finding:
    """The Grype fields needed by this policy gate."""

    key: FindingKey
    severity: str
    fix_state: str


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError(f"{label} is too large")
    try:
        return json.loads(payload, object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label} JSON: {error}") from error


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(data: dict[str, object], field: str, label: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{field} must be a non-empty string")
    return value


def _finding(value: object, label: str) -> Finding:
    match = _object(value, label)
    vulnerability = _object(match.get("vulnerability"), f"{label}.vulnerability")
    artifact = _object(match.get("artifact"), f"{label}.artifact")
    fix = _object(vulnerability.get("fix"), f"{label}.vulnerability.fix")
    finding = Finding(
        key=FindingKey(
            vulnerability_id=_string(vulnerability, "id", f"{label}.vulnerability"),
            package=_string(artifact, "name", f"{label}.artifact"),
        ),
        severity=_string(vulnerability, "severity", f"{label}.vulnerability"),
        fix_state=cast(str, fix.get("state", "")),
    )
    if not isinstance(finding.fix_state, str):
        raise ValueError(f"{label}.vulnerability.fix.state must be a string")
    return finding


def load_findings(report_path: Path) -> tuple[Finding, ...]:
    """Load all Grype matches without filtering fix states or severity."""

    report = _object(_read_json(report_path, "Grype report"), "Grype report")
    matches = report.get("matches")
    if not isinstance(matches, list):
        raise ValueError("Grype report.matches must be an array")
    return tuple(_finding(value, f"matches[{index}]") for index, value in enumerate(matches))


def load_vendor_vex_suppressions(report_path: Path) -> frozenset[FindingKey]:
    """Return only suppressions that Grype explicitly attributes to verified VEX."""

    report = _object(_read_json(report_path, "VEX-applied Grype report"), "Grype report")
    ignored = report.get("ignoredMatches")
    if not isinstance(ignored, list):
        raise ValueError("VEX-applied Grype report.ignoredMatches must be an array")
    suppressed: set[FindingKey] = set()
    for index, value in enumerate(ignored):
        item = _object(value, f"ignoredMatches[{index}]")
        finding = _finding(item, f"ignoredMatches[{index}]")
        rules = item.get("appliedIgnoreRules")
        if not isinstance(rules, list):
            raise ValueError(f"ignoredMatches[{index}].appliedIgnoreRules must be an array")
        for rule_value in rules:
            rule = _object(rule_value, f"ignoredMatches[{index}].appliedIgnoreRules")
            if rule.get("vex-status") == VENDOR_VEX_STATUS:
                suppressed.add(finding.key)
                break
    return frozenset(suppressed)


def _parse_date(value: str, field: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO 8601 date") from error


def validate_acceptances(
    policy_path: Path,
    *,
    findings: tuple[Finding, ...],
    vendor_suppressions: frozenset[FindingKey],
    image_digest: str,
    today: dt.date,
) -> tuple[int, int]:
    """Validate records and require coverage for every unfixable High/Critical."""

    if DIGEST_PATTERN.fullmatch(image_digest) is None:
        raise ValueError("image digest must be sha256:<64 lowercase hex>")
    root = _object(_read_json(policy_path, "risk acceptance policy"), "risk policy")
    if set(root) != {"schema_version", "maximum_validity_days", "acceptances"}:
        raise ValueError("risk acceptance policy has unexpected root fields")
    if root["schema_version"] != 1 or root["maximum_validity_days"] != 90:
        raise ValueError("risk acceptance policy version or maximum validity is unsupported")
    raw_acceptances = root["acceptances"]
    if not isinstance(raw_acceptances, list):
        raise ValueError("acceptances must be an array")

    tracked = {
        finding.key
        for finding in findings
        if finding.severity in TRACKED_SEVERITIES and finding.fix_state in UNFIXED_STATES
    }
    active: set[FindingKey] = set()
    for index, value in enumerate(raw_acceptances):
        label = f"acceptances[{index}]"
        record = _object(value, label)
        if set(record) != REQUIRED_ACCEPTANCE_FIELDS:
            missing = sorted(REQUIRED_ACCEPTANCE_FIELDS - set(record))
            extra = sorted(set(record) - REQUIRED_ACCEPTANCE_FIELDS)
            raise ValueError(f"{label} fields invalid: missing={missing}, extra={extra}")
        vulnerability_id = _string(record, "vulnerability_id", label)
        package = _string(record, "package", label)
        if VULNERABILITY_PATTERN.fullmatch(vulnerability_id) is None:
            raise ValueError(f"{label}.vulnerability_id is invalid")
        if _string(record, "image_digest", label) != image_digest:
            raise ValueError(f"{label} image digest does not match the tested image")
        if _string(record, "status", label) not in {"affected", "under_investigation"}:
            raise ValueError(f"{label}.status must not claim not_affected")
        for field in TEXT_FIELDS:
            if len(_string(record, field, label).strip()) < 10:
                raise ValueError(f"{label}.{field} requires concrete evidence")
        _string(record, "owner", label)
        approved = _parse_date(_string(record, "approved_on", label), f"{label}.approved_on")
        expires = _parse_date(_string(record, "expires_on", label), f"{label}.expires_on")
        if approved > today:
            raise ValueError(f"{label} approval date is in the future")
        if expires < today:
            raise ValueError(f"{label} is expired")
        if expires <= approved or (expires - approved).days > 90:
            raise ValueError(f"{label} must expire within 90 days after approval")
        key = FindingKey(vulnerability_id, package)
        if key in active:
            raise ValueError(f"duplicate risk acceptance: {vulnerability_id}/{package}")
        if key not in tracked:
            raise ValueError(f"{label} does not reference a current unfixable High/Critical")
        active.add(key)

    residual = tracked - vendor_suppressions
    missing = sorted(residual - active)
    if missing:
        rendered = ", ".join(f"{item.vulnerability_id}/{item.package}" for item in missing)
        raise ValueError(f"untracked unfixable High/Critical findings: {rendered}")
    stale = active - residual
    if stale:
        rendered = ", ".join(f"{item.vulnerability_id}/{item.package}" for item in sorted(stale))
        raise ValueError(f"risk acceptances are stale or superseded by vendor VEX: {rendered}")
    return len(vendor_suppressions & tracked), len(active)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--raw-report", type=Path, required=True)
    parser.add_argument("--vex-report", type=Path, required=True)
    parser.add_argument("--image-digest-file", type=Path, required=True)
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, default=dt.datetime.now(dt.UTC).date()
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        image_digest = args.image_digest_file.read_text(encoding="ascii").strip()
        vendor_count, accepted_count = validate_acceptances(
            args.policy,
            findings=load_findings(args.raw_report),
            vendor_suppressions=load_vendor_vex_suppressions(args.vex_report),
            image_digest=image_digest,
            today=args.today,
        )
    except (OSError, UnicodeError, ValueError) as error:
        print(f"container risk acceptance check failed: {error}", file=sys.stderr)
        return 1
    print(
        "container risk acceptance check passed: "
        f"vendor_vex={vendor_count}, time_limited_acceptances={accepted_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
