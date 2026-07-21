#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Render Grype scan results into the GitHub Actions summary and enforce the severity gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

MAX_REPORT_BYTES: Final = 512 * 1024 * 1024
MAX_ANNOTATIONS_PER_KIND: Final = 10
SEVERITY_ORDER: Final = ("critical", "high", "medium", "low", "negligible", "unknown")
BLOCKING_SEVERITIES: Final = frozenset({"critical", "high"})
WARNING_SEVERITIES: Final = frozenset({"medium", "low"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One Grype vulnerability match reduced to annotation-safe fields."""

    target: str
    vulnerability_id: str
    severity: str
    package_name: str
    package_version: str
    fixed_versions: tuple[str, ...]


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"grype report field must be an object: {context}")
    return cast(Mapping[str, object], value)


def _require_string(data: Mapping[str, object], field: str, context: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"grype report field must be a non-empty string: {context}.{field}")
    return value


def load_findings(path: Path, target: str) -> tuple[Finding, ...]:
    """Load one Grype JSON report, failing closed on any malformed structure."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"grype report must be a regular file: {path}")
    data = path.read_bytes()
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError(f"grype report is too large: {path}")
    try:
        root = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid grype report JSON: {path}: {error}") from error
    root = _require_mapping(root, str(path))
    matches = root.get("matches")
    if not isinstance(matches, list):
        raise ValueError(f"grype report has no matches list: {path}")

    findings: list[Finding] = []
    for index, raw_match in enumerate(matches):
        context = f"{path} matches[{index}]"
        match = _require_mapping(raw_match, context)
        vulnerability = _require_mapping(match.get("vulnerability"), f"{context}.vulnerability")
        artifact = _require_mapping(match.get("artifact"), f"{context}.artifact")
        severity = _require_string(vulnerability, "severity", context).lower()
        if severity not in SEVERITY_ORDER:
            severity = "unknown"
        fix = vulnerability.get("fix")
        fixed_versions: tuple[str, ...] = ()
        if isinstance(fix, dict):
            versions = cast(Mapping[str, object], fix).get("versions")
            if isinstance(versions, list) and all(isinstance(v, str) for v in versions):
                fixed_versions = tuple(cast(list[str], versions))
        findings.append(
            Finding(
                target=target,
                vulnerability_id=_require_string(vulnerability, "id", context),
                severity=severity,
                package_name=_require_string(artifact, "name", context),
                package_version=_require_string(artifact, "version", context),
                fixed_versions=fixed_versions,
            )
        )
    return tuple(findings)


def _escape_annotation(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def emit_annotations(findings: Sequence[Finding]) -> None:
    """Emit GitHub Actions annotations, capped at the service-side display limit."""

    for kind, severities in (
        ("error", BLOCKING_SEVERITIES),
        ("warning", WARNING_SEVERITIES),
    ):
        selected = [finding for finding in findings if finding.severity in severities]
        for finding in selected[:MAX_ANNOTATIONS_PER_KIND]:
            message = (
                f"grype: {finding.vulnerability_id} {finding.package_name}"
                f"@{finding.package_version} ({finding.severity}) in {finding.target}"
            )
            print(f"::{kind}::{_escape_annotation(message)}")
        remaining = len(selected) - MAX_ANNOTATIONS_PER_KIND
        if remaining > 0:
            print(
                f"::{kind}::{remaining} additional grype finding(s);"
                " see the job summary and artifact"
            )


def _summary_rows(label: str, findings: Sequence[Finding]) -> list[str]:
    counts = Counter(finding.severity for finding in findings)
    cells = [str(counts.get(severity, 0)) for severity in SEVERITY_ORDER]
    return [f"| {label} | {' | '.join(cells)} | {len(findings)} |"]


def write_summary(reports: Sequence[tuple[str, tuple[Finding, ...]]], summary_path: Path) -> None:
    """Write the Markdown job summary with per-target counts and blocking details."""

    lines = [
        "## Grype vulnerability scan",
        "",
        "| Target | Critical | High | Medium | Low | Negligible | Unknown | Total |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for label, findings in reports:
        lines.extend(_summary_rows(label, findings))
    blocking = [
        finding
        for _, findings in reports
        for finding in findings
        if finding.severity in BLOCKING_SEVERITIES
    ]
    lines.append("")
    if blocking:
        lines.append(f"**Gate: FAILED — {len(blocking)} high-or-above finding(s).**")
        lines.append("")
        lines.append("| Severity | Vulnerability | Package | Installed | Fixed | Target |")
        lines.append("|---|---|---|---|---|---|")
        for finding in blocking:
            fixed = ", ".join(finding.fixed_versions) if finding.fixed_versions else "-"
            lines.append(
                f"| {finding.severity} | {finding.vulnerability_id} | {finding.package_name}"
                f" | {finding.package_version} | {fixed} | {finding.target} |"
            )
    else:
        lines.append("**Gate: PASSED — no high-or-above findings.**")
        lines.append("")
        lines.append("Medium and low findings are reported as workflow annotations only.")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        action="append",
        nargs=2,
        metavar=("LABEL", "REPORT"),
        required=True,
        help="target label and Grype JSON report path; repeatable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reports = [
            (label, load_findings(Path(report_path), label))
            for label, report_path in cast(list[list[str]], args.target)
        ]
    except ValueError as error:
        print(f"grype report error: {error}", file=sys.stderr)
        return 1

    findings = [finding for _, target_findings in reports for finding in target_findings]
    emit_annotations(findings)
    summary_env = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_env:
        write_summary(reports, Path(summary_env))
    for label, target_findings in reports:
        counts = Counter(finding.severity for finding in target_findings)
        rendered = ", ".join(f"{severity}={counts.get(severity, 0)}" for severity in SEVERITY_ORDER)
        print(f"grype {label}: {rendered}")

    blocking = sum(1 for finding in findings if finding.severity in BLOCKING_SEVERITIES)
    if blocking > 0:
        print(f"grype severity gate: FAILED ({blocking} high-or-above finding(s))")
        return 1
    print("grype severity gate: PASSED (no high-or-above findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
