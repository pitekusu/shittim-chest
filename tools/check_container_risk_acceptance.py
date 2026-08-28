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
MAX_CONFIG_DIGESTS_PER_IMAGE_KIND: Final = 4
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
        "image_config_digests",
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
IMAGE_KINDS: Final = frozenset({"production"})
REQUIRED_POLICY_FIELDS: Final = frozenset(
    {"schema_version", "maximum_validity_days", "acceptances"}
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


def _validate_image_scope(image_kind: str, image_config_digest: str) -> None:
    if image_kind not in IMAGE_KINDS:
        raise ValueError("image kind is unsupported")
    if DIGEST_PATTERN.fullmatch(image_config_digest) is None:
        raise ValueError("image config digest must be sha256:<64 lowercase hex>")


def _load_policy(policy_path: Path) -> list[object]:
    root = _object(_read_json(policy_path, "risk acceptance policy"), "risk policy")
    if set(root) != REQUIRED_POLICY_FIELDS:
        raise ValueError("risk acceptance policy has unexpected root fields")
    if root["schema_version"] != 6 or root["maximum_validity_days"] != 90:
        raise ValueError("risk acceptance policy version or maximum validity is unsupported")
    raw_acceptances = root["acceptances"]
    if not isinstance(raw_acceptances, list):
        raise ValueError("acceptances must be an array")
    return cast(list[object], raw_acceptances)


def _acceptance_record(value: object, label: str) -> dict[str, object]:
    record = _object(value, label)
    if set(record) != REQUIRED_ACCEPTANCE_FIELDS:
        missing = sorted(REQUIRED_ACCEPTANCE_FIELDS - set(record))
        extra = sorted(set(record) - REQUIRED_ACCEPTANCE_FIELDS)
        raise ValueError(f"{label} fields invalid: missing={missing}, extra={extra}")
    return record


def _config_digests(record: dict[str, object], label: str) -> dict[str, tuple[str, ...]]:
    raw_config_digests = _object(
        record.get("image_config_digests"), f"{label}.image_config_digests"
    )
    if not raw_config_digests or not set(raw_config_digests) <= IMAGE_KINDS:
        raise ValueError(f"{label}.image_config_digests has unsupported image kinds")
    config_digests: dict[str, tuple[str, ...]] = {}
    for scoped_kind, raw_digests in raw_config_digests.items():
        if not isinstance(raw_digests, list) or not raw_digests:
            raise ValueError(
                f"{label}.image_config_digests.{scoped_kind} must be a non-empty array"
            )
        if len(raw_digests) > MAX_CONFIG_DIGESTS_PER_IMAGE_KIND:
            raise ValueError(
                f"{label}.image_config_digests.{scoped_kind} must contain at most "
                f"{MAX_CONFIG_DIGESTS_PER_IMAGE_KIND} digests"
            )
        if not all(
            isinstance(digest, str) and DIGEST_PATTERN.fullmatch(digest) is not None
            for digest in raw_digests
        ):
            raise ValueError(f"{label}.image_config_digests.{scoped_kind} is invalid")
        typed_digests = cast(list[str], raw_digests)
        if len(set(typed_digests)) != len(typed_digests):
            raise ValueError(
                f"{label}.image_config_digests.{scoped_kind} must contain unique digests"
            )
        config_digests[scoped_kind] = tuple(typed_digests)
    return config_digests


def _validated_acceptance(
    value: object, label: str, today: dt.date
) -> tuple[FindingKey, dict[str, tuple[str, ...]]]:
    record = _acceptance_record(value, label)
    vulnerability_id = _string(record, "vulnerability_id", label)
    package = _string(record, "package", label)
    if VULNERABILITY_PATTERN.fullmatch(vulnerability_id) is None:
        raise ValueError(f"{label}.vulnerability_id is invalid")
    config_digests = _config_digests(record, label)
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
    return FindingKey(vulnerability_id, package), config_digests


def _register_acceptance_scopes(
    seen: set[tuple[str, FindingKey]],
    key: FindingKey,
    config_digests: dict[str, tuple[str, ...]],
) -> None:
    for scoped_kind in config_digests:
        scoped_key = (scoped_kind, key)
        if scoped_key in seen:
            raise ValueError(
                f"duplicate risk acceptance: {scoped_kind}/{key.vulnerability_id}/{key.package}"
            )
        seen.add(scoped_key)


def validate_config_digest_bindings(
    policy_path: Path, *, image_kind: str, image_config_digest: str, today: dt.date
) -> int:
    """Fail before push on stale acceptance metadata or a changed scoped image."""

    _validate_image_scope(image_kind, image_config_digest)
    raw_acceptances = _load_policy(policy_path)
    bound = 0
    seen: set[tuple[str, FindingKey]] = set()
    for index, value in enumerate(raw_acceptances):
        label = f"acceptances[{index}]"
        key, config_digests = _validated_acceptance(value, label, today)
        _register_acceptance_scopes(seen, key, config_digests)
        record_config_digests = config_digests.get(image_kind)
        if record_config_digests is None:
            continue
        if image_config_digest not in record_config_digests:
            raise ValueError(f"{label} config digest does not match the tested image")
        bound += 1
    return bound


def validate_acceptances(
    policy_path: Path,
    *,
    findings: tuple[Finding, ...],
    vendor_suppressions: frozenset[FindingKey],
    image_kind: str,
    image_config_digest: str,
    today: dt.date,
) -> tuple[int, int]:
    """Validate records and require coverage for every unfixable High/Critical."""

    _validate_image_scope(image_kind, image_config_digest)
    raw_acceptances = _load_policy(policy_path)

    tracked = {
        finding.key
        for finding in findings
        if finding.severity in TRACKED_SEVERITIES and finding.fix_state in UNFIXED_STATES
    }
    active: set[FindingKey] = set()
    seen: set[tuple[str, FindingKey]] = set()
    for index, value in enumerate(raw_acceptances):
        label = f"acceptances[{index}]"
        key, config_digests = _validated_acceptance(value, label, today)
        _register_acceptance_scopes(seen, key, config_digests)
        record_config_digests = config_digests.get(image_kind)
        if record_config_digests is not None and image_config_digest not in record_config_digests:
            raise ValueError(f"{label} config digest does not match the tested image")
        if record_config_digests is None:
            continue
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
    parser.add_argument("--raw-report", type=Path)
    parser.add_argument("--vex-report", type=Path)
    parser.add_argument("--image-kind", choices=tuple(sorted(IMAGE_KINDS)), required=True)
    parser.add_argument("--image-config-digest-file", type=Path, required=True)
    parser.add_argument("--config-digest-only", action="store_true")
    parser.add_argument(
        "--today", type=dt.date.fromisoformat, default=dt.datetime.now(dt.UTC).date()
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        image_config_digest = args.image_config_digest_file.read_text(encoding="ascii").strip()
        if args.config_digest_only:
            bound_count = validate_config_digest_bindings(
                args.policy,
                image_kind=args.image_kind,
                image_config_digest=image_config_digest,
                today=args.today,
            )
            print(f"container config digest preflight passed: time_limited_bindings={bound_count}")
            return 0
        if args.raw_report is None or args.vex_report is None:
            raise ValueError("raw and VEX reports are required unless config-digest-only is set")
        vendor_count, accepted_count = validate_acceptances(
            args.policy,
            findings=load_findings(args.raw_report),
            vendor_suppressions=load_vendor_vex_suppressions(args.vex_report),
            image_kind=args.image_kind,
            image_config_digest=image_config_digest,
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
