# SPDX-License-Identifier: MIT
"""Run an npm-family audit unless npm officially confirms an audit outage."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from collections.abc import Mapping, Sequence

NPM_STATUS_SUMMARY_URL = "https://status.npmjs.org/api/v2/summary.json"
SECURITY_AUDIT_COMPONENT = "Security Audit"
ACTIVE_INCIDENT_STATES = frozenset({"investigating", "identified", "monitoring"})
OUTAGE_COMPONENT_STATES = frozenset({"degraded_performance", "partial_outage", "major_outage"})
MAXIMUM_STATUS_BYTES = 1_048_576


class NpmAuditStatusError(RuntimeError):
    """Raised when the official status cannot safely authorize an audit skip."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise NpmAuditStatusError(f"{label} was not an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise NpmAuditStatusError(f"{label} was not an array")
    return value


def official_security_audit_outage(payload: object) -> bool:
    """Return whether npm confirms an active incident for its audit component."""

    summary = _mapping(payload, "npm status summary")
    components = [
        _mapping(component, "npm status component")
        for component in _sequence(summary.get("components"), "npm status components")
    ]
    matches = [
        component for component in components if component.get("name") == SECURITY_AUDIT_COMPONENT
    ]
    if len(matches) != 1:
        raise NpmAuditStatusError("npm Security Audit component was not uniquely identified")

    component = matches[0]
    component_id = component.get("id")
    component_status = component.get("status")
    if not isinstance(component_id, str) or not component_id:
        raise NpmAuditStatusError("npm Security Audit component id was invalid")
    if component_status == "operational":
        return False
    if component_status not in OUTAGE_COMPONENT_STATES:
        raise NpmAuditStatusError("npm Security Audit component status was not recognized")

    for raw_incident in _sequence(summary.get("incidents"), "npm status incidents"):
        incident = _mapping(raw_incident, "npm status incident")
        if incident.get("status") not in ACTIVE_INCIDENT_STATES:
            continue
        for raw_affected in _sequence(incident.get("components"), "npm status incident components"):
            affected = _mapping(raw_affected, "npm status affected component")
            if affected.get("id") == component_id and affected.get("status") == component_status:
                return True
    raise NpmAuditStatusError(
        "npm reported a degraded Security Audit component without a matching active incident"
    )


def _fetch_summary() -> object:
    request = urllib.request.Request(
        NPM_STATUS_SUMMARY_URL,
        headers={"Accept": "application/json", "User-Agent": "shittim-chest-ci/1"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        if response.status != 200:
            raise NpmAuditStatusError("npm status endpoint did not return HTTP 200")
        body = response.read(MAXIMUM_STATUS_BYTES + 1)
    if len(body) > MAXIMUM_STATUS_BYTES:
        raise NpmAuditStatusError("npm status response exceeded the size limit")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NpmAuditStatusError("npm status response was not valid JSON") from error


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2 or arguments[0] != "--":
        print("usage: run_npm_audit.py -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    command = arguments[1:]
    try:
        outage = official_security_audit_outage(_fetch_summary())
    except (NpmAuditStatusError, OSError) as error:
        print(
            f"npm Security Audit status could not be verified: {error}; refusing to skip",
            file=sys.stderr,
        )
        return 1
    if outage:
        print(
            "::warning title=npm Security Audit unavailable::"
            "Official npm status confirms an active Security Audit incident; "
            "this external audit is skipped for this run."
        )
        return 0
    return subprocess.run(command, check=False).returncode  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
