"""Negative policy tests for the one pull_request_target exception."""

from __future__ import annotations

from pathlib import Path

import pytest
from tools.check_notification_workflows import (
    ALLOWED_TARGET_WORKFLOW,
    WORKFLOW_DIRECTORY,
    WorkflowPolicyError,
    validate_notification_workflows,
)


def _workflow_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir()
    source = WORKFLOW_DIRECTORY / ALLOWED_TARGET_WORKFLOW
    (directory / ALLOWED_TARGET_WORKFLOW).write_bytes(source.read_bytes())
    digest = WORKFLOW_DIRECTORY / "discord-security-digest.yml"
    (directory / digest.name).write_bytes(digest.read_bytes())
    return directory


def _replace(directory: Path, old: str, new: str) -> None:
    path = directory / ALLOWED_TARGET_WORKFLOW
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


def test_repository_target_workflow_is_accepted(tmp_path: Path) -> None:
    assert validate_notification_workflows(_workflow_directory(tmp_path)) == 1


def test_unapproved_target_workflow_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    (directory / "unsafe.yml").write_text(
        "name: unsafe\non:\n  pull_request_target:\npermissions: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="restricted"):
        validate_notification_workflows(directory)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("contents: read", "contents: write", "write permission"),
        ("ref: ${{ github.sha }}", "ref: ${{ github.event.pull_request.head.sha }}", "PR head"),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "uses: actions/download-artifact@0000000000000000000000000000000000000000",
            "artifact",
        ),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "uses: actions/cache@0000000000000000000000000000000000000000",
            "cache",
        ),
        (
            "run: python3 -m tools.github_discord_notifications pull-request",
            "run: echo ${{ github.event.pull_request.title }}",
            "untrusted event",
        ),
        ("runs-on: ubuntu-latest", "runs-on: self-hosted", "self-hosted"),
    ],
)
def test_forbidden_target_capability_is_rejected(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(directory, old, new)
    with pytest.raises(WorkflowPolicyError, match=message):
        validate_notification_workflows(directory)


def test_additional_secret_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}",
        "DISCORD_WEBHOOK_URL: ${{ secrets.EXTRA_SECRET }}",
    )
    with pytest.raises(WorkflowPolicyError, match="only DISCORD_WEBHOOK_URL"):
        validate_notification_workflows(directory)


def test_extra_checkout_without_trusted_ref_is_rejected(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "      - name: Notify pull-request lifecycle",
        "      - uses: actions/checkout@0000000000000000000000000000000000000000\n"
        "      - name: Notify pull-request lifecycle",
    )
    with pytest.raises(WorkflowPolicyError, match="every checkout"):
        validate_notification_workflows(directory)


def test_multiline_run_cannot_expand_pull_request_metadata(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    _replace(
        directory,
        "run: python3 -m tools.github_discord_notifications pull-request",
        "run: |\n          echo ${{ github.event.pull_request.title }}",
    )
    with pytest.raises(WorkflowPolicyError, match="untrusted event"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_widened(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    digest = directory / "discord-security-digest.yml"
    digest.write_text(
        digest.read_text(encoding="utf-8").replace(
            "vulnerability-alerts: read", "vulnerability-alerts: write"
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)


def test_vulnerability_alerts_permission_cannot_be_duplicated(tmp_path: Path) -> None:
    directory = _workflow_directory(tmp_path)
    extra = directory / "extra.yml"
    extra.write_text("permissions:\n  vulnerability-alerts: read\n", encoding="utf-8")
    with pytest.raises(WorkflowPolicyError, match="one read-only"):
        validate_notification_workflows(directory)
