# SPDX-License-Identifier: MIT
"""Enforce the narrow pull_request_target exception for Discord notifications."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
ALLOWED_TARGET_WORKFLOW = "discord-repository-events.yml"
WRITE_PERMISSION = re.compile(r"(?m)^\s+[a-z-]+:\s*write\s*(?:#.*)?$")


class WorkflowPolicyError(RuntimeError):
    """Raised when the target-workflow trust boundary is widened."""


def validate_notification_workflows(directory: Path = WORKFLOW_DIRECTORY) -> int:
    """Validate every target trigger and the one approved workflow."""

    target_files: list[Path] = []
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        text = path.read_text(encoding="utf-8")
        if any(
            "pull_request_target" in line and not line.lstrip().startswith("#")
            for line in text.splitlines()
        ):
            target_files.append(path)
    names = [path.name for path in target_files]
    if names != [ALLOWED_TARGET_WORKFLOW]:
        raise WorkflowPolicyError(
            "pull_request_target is restricted to the Discord repository-events workflow"
        )
    text = target_files[0].read_text(encoding="utf-8")
    forbidden = {
        "write permission": WRITE_PERMISSION,
        "PR head checkout": re.compile(
            r"github\.(?:head_ref|event\.pull_request\.head\.(?:sha|ref))"
        ),
        "artifact action": re.compile(r"actions/(?:download|upload)-artifact@"),
        "cache action": re.compile(r"actions/cache@|cache-from:|cache-to:"),
        "self-hosted runner": re.compile(r"runs-on:\s*self-hosted"),
        "write-all permissions": re.compile(r"permissions:\s*write-all"),
    }
    for label, pattern in forbidden.items():
        if pattern.search(text):
            raise WorkflowPolicyError(f"target workflow contains forbidden {label}")
    if _contains_untrusted_run_expression(text):
        raise WorkflowPolicyError(
            "target workflow contains forbidden untrusted event expression in run"
        )
    required = (
        "permissions: {}",
        "contents: read",
        "pull-requests: read",
        "ref: ${{ github.sha }}",
        "persist-credentials: false",
        "github.event.pull_request.user.login != 'dependabot[bot]'",
    )
    for marker in required:
        if marker not in text:
            raise WorkflowPolicyError(f"target workflow lacks required policy marker: {marker}")
    if text.count("uses: actions/checkout@") != 2 or text.count("ref: ${{ github.sha }}") != 2:
        raise WorkflowPolicyError("every checkout must use the trusted github.sha ref")
    secret_references = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    if secret_references != {"DISCORD_WEBHOOK_URL"}:
        raise WorkflowPolicyError("target workflow may use only DISCORD_WEBHOOK_URL")
    return 1


def _contains_untrusted_run_expression(text: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run:\s*(.*)$", line)
        if match is None:
            continue
        indentation = len(match.group(1))
        remainder = match.group(2)
        if "${{ github.event." in remainder:
            return True
        if remainder not in {"|", ">", "|-", ">-", "|+", ">+"}:
            continue
        for nested in lines[index + 1 :]:
            if nested.strip() and len(nested) - len(nested.lstrip()) <= indentation:
                break
            if "${{ github.event." in nested:
                return True
    return False


def main() -> int:
    try:
        count = validate_notification_workflows()
    except (OSError, UnicodeDecodeError, WorkflowPolicyError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"notification workflow policy is valid: {count} target workflow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
