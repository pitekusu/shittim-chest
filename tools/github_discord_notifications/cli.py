# SPDX-License-Identifier: MIT
"""Command-line entrypoint for GitHub Actions notification workflows."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from tools.github_discord_notifications.github_api import GitHubApiError, GitHubClient
from tools.github_discord_notifications.models import JsonObject
from tools.github_discord_notifications.webhook import DiscordWebhookError, DiscordWebhookSender
from tools.github_discord_notifications.workflow_run import run_notification


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("workflow-run", help="notify a completed trusted workflow run")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "workflow-run":
            results = _workflow_run(os.environ)
        else:  # pragma: no cover - argparse rejects unknown commands.
            raise ValueError("unsupported command")
    except (DiscordWebhookError, GitHubApiError, OSError, ValueError) as error:
        _append_summary(os.environ, success=False, lines=("通知処理に失敗しました。",))
        print(f"notification failed: {error}", file=sys.stderr)
        return 1
    if not results:
        _append_summary(
            os.environ,
            success=True,
            lines=("対象外のworkflow name/pathだったため送信しませんでした。",),
        )
        return 0
    _append_summary(
        os.environ,
        success=True,
        lines=tuple(
            f"{result.kind}: {result.logical_thread} / {result.target_url}" for result in results
        ),
    )
    return 0


def _workflow_run(environment: Mapping[str, str]):
    event = _read_event(_required(environment, "GITHUB_EVENT_PATH"))
    github = GitHubClient(
        token=_required(environment, "GITHUB_TOKEN"),
        repository=_required(environment, "GITHUB_REPOSITORY"),
    )
    return run_notification(
        event=event,
        environment=environment,
        github=github,
        discord=DiscordWebhookSender(),
    )


def _read_event(path_value: str) -> JsonObject:
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("GITHUB_EVENT_PATH must be a regular file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("GitHub event payload is invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("GitHub event payload must be an object")
    return cast(JsonObject, value)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable {name} is not configured")
    return value


def _append_summary(
    environment: Mapping[str, str],
    *,
    success: bool,
    lines: tuple[str, ...],
) -> None:
    path_value = environment.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path_value:
        return
    path = Path(path_value)
    heading = "success" if success else "failure"
    body = "\n".join(f"- {line}" for line in lines)
    with path.open("a", encoding="utf-8", newline="\n") as summary:
        summary.write(f"## Discord notification: {heading}\n\n{body}\n")
