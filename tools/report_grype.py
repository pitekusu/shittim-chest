#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate Grype report separation and emit vulnerability comparison metrics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

MAX_INPUT_BYTES: Final = 64 * 1024 * 1024


def _read_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file: {path}")
    payload = path.read_bytes()
    if len(payload) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} is too large")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label} JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _matches(report: Mapping[str, object], label: str) -> list[dict[str, object]]:
    values = report.get("matches")
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError(f"{label}.matches must be an object array")
    return cast(list[dict[str, object]], values)


def _ignored_matches(report: Mapping[str, object], label: str) -> list[dict[str, object]]:
    values = report.get("ignoredMatches", [])
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError(f"{label}.ignoredMatches must be an object array")
    return cast(list[dict[str, object]], values)


def _vulnerability(match: Mapping[str, object], label: str) -> dict[str, object]:
    value = match.get("vulnerability")
    if not isinstance(value, dict):
        raise ValueError(f"{label}.vulnerability must be an object")
    return cast(dict[str, object], value)


def _fix_state(vulnerability: dict[str, object], label: str) -> str:
    fix = vulnerability.get("fix")
    if not isinstance(fix, dict):
        raise ValueError(f"{label}.fix must be an object")
    state = fix.get("state", "")
    if not isinstance(state, str):
        raise ValueError(f"{label}.fix.state must be a string")
    return state


def summarize_report(
    full_report: dict[str, object],
    actionable_report: Mapping[str, object],
    label: str,
) -> dict[str, object]:
    """Validate the `--only-fixed` result and summarize the complete report."""

    full = _matches(full_report, f"{label}.full")
    ignored = _ignored_matches(full_report, f"{label}.full")
    actionable = _matches(actionable_report, f"{label}.actionable")
    severities: Counter[str] = Counter()
    fix_states: Counter[str] = Counter()
    vulnerability_ids: set[str] = set()
    for index, match in enumerate(full):
        vulnerability = _vulnerability(match, f"{label}.full.matches[{index}]")
        vulnerability_id = vulnerability.get("id")
        severity = vulnerability.get("severity")
        if not isinstance(vulnerability_id, str) or not isinstance(severity, str):
            raise ValueError(f"{label} vulnerability ID/severity must be strings")
        vulnerability_ids.add(vulnerability_id)
        severities[severity] += 1
        fix_states[_fix_state(vulnerability, f"{label}.full.matches[{index}]") or "unknown"] += 1

    actionable_high_critical = 0
    for index, match in enumerate(actionable):
        vulnerability = _vulnerability(match, f"{label}.actionable.matches[{index}]")
        if _fix_state(vulnerability, f"{label}.actionable.matches[{index}]") != "fixed":
            raise ValueError(f"{label} actionable report contains a non-fixable finding")
        if vulnerability.get("severity") in {"High", "Critical"}:
            actionable_high_critical += 1

    if len(actionable) > len(full):
        raise ValueError(f"{label} actionable report is larger than the full report")
    return {
        "active_findings": len(full),
        "scanner_ignored_findings": len(ignored),
        "observed_findings": len(full) + len(ignored),
        "unique_vulnerabilities": len(vulnerability_ids),
        "severity": dict(sorted(severities.items())),
        "fix_state": dict(sorted(fix_states.items())),
        "actionable_findings": len(actionable),
        "actionable_high_critical": actionable_high_critical,
    }


def _is_debian_reference(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    reference = cast(dict[str, object], value)
    locator = reference.get("referenceLocator")
    return (
        reference.get("referenceType") == "purl"
        and isinstance(locator, str)
        and locator.startswith("pkg:deb/")
    )


def image_inventory(spdx: Mapping[str, object], size_bytes: int) -> dict[str, int]:
    """Count all and Debian packages in the final image SPDX inventory."""

    packages = spdx.get("packages")
    if not isinstance(packages, list):
        raise ValueError("image SPDX packages must be an array")
    debian_packages = 0
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("image SPDX package must be an object")
        external_refs = package.get("externalRefs", [])
        if not isinstance(external_refs, list):
            raise ValueError("image SPDX externalRefs must be an array")
        if any(_is_debian_reference(reference) for reference in external_refs):
            debian_packages += 1
    return {
        "size_bytes": size_bytes,
        "packages": len(packages),
        "debian_packages": debian_packages,
    }


def _positive_integer_file(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("image size file must contain an integer") from error
    if value <= 0:
        raise ValueError("image size must be positive")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-full", type=Path, required=True)
    parser.add_argument("--source-actionable", type=Path, required=True)
    parser.add_argument("--image-full", type=Path, required=True)
    parser.add_argument("--image-actionable", type=Path, required=True)
    parser.add_argument("--image-sbom", type=Path, required=True)
    parser.add_argument("--image-size-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        summary = {
            "schema_version": 1,
            "source": summarize_report(
                _read_object(args.source_full, "source full report"),
                _read_object(args.source_actionable, "source actionable report"),
                "source",
            ),
            "image": summarize_report(
                _read_object(args.image_full, "image full report"),
                _read_object(args.image_actionable, "image actionable report"),
                "image",
            ),
            "image_inventory": image_inventory(
                _read_object(args.image_sbom, "image SPDX"),
                _positive_integer_file(args.image_size_file),
            ),
        }
        args.output.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as error:
        print(f"Grype report validation failed: {error}", file=sys.stderr)
        return 1
    print("Grype full/actionable reports are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
