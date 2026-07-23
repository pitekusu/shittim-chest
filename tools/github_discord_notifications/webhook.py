# SPDX-License-Identifier: MIT
"""Discord Incoming Webhook transport implemented with a fixed curl command."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from tools.github_discord_notifications.formatting import validate_snowflake
from tools.github_discord_notifications.models import CurlResult, JsonObject

MAX_ATTEMPTS = 4
TRANSIENT_CURL_CODES = frozenset({5, 6, 7, 18, 28, 35, 52, 55, 56})
_STATUS_CODE_ERRORS = (UnicodeDecodeError, ValueError)


class DiscordWebhookError(RuntimeError):
    """A sanitized webhook failure that never includes the secret URL or response body."""


CurlRunner = Callable[[str, str, bytes], CurlResult]
Sleeper = Callable[[float], None]


class DiscordWebhookSender:
    """Send one message to an existing Discord Forum thread with bounded retry."""

    def __init__(
        self,
        *,
        runner: CurlRunner | None = None,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._runner = runner or _run_curl
        self._sleeper = sleeper

    def send(self, *, webhook_url: str, thread_id: str, payload: JsonObject) -> None:
        """Send JSON, retrying only transient transport errors, 429, and 5xx."""

        validate_snowflake(thread_id, label="Discord thread ID")
        _validate_webhook_url(webhook_url)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = self._runner(webhook_url, thread_id, body)
            if result.return_code == 0 and result.status_code in {200, 204}:
                return
            retryable = result.return_code in TRANSIENT_CURL_CODES or (
                result.status_code == 429
                or (result.status_code is not None and 500 <= result.status_code <= 599)
            )
            if not retryable or attempt == MAX_ATTEMPTS:
                if result.status_code is not None:
                    raise DiscordWebhookError(
                        f"Discord webhook failed with HTTP {result.status_code}"
                    )
                raise DiscordWebhookError("Discord webhook transport failed")
            self._sleeper(_retry_delay(result, attempt))


def _validate_webhook_url(webhook_url: str) -> None:
    parsed = urllib.parse.urlsplit(webhook_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "discordapp.com"}
        or not parsed.path.startswith("/api/webhooks/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise DiscordWebhookError("Discord webhook URL is invalid")


def _retry_delay(result: CurlResult, attempt: int) -> float:
    retry_after = result.header("Retry-After")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    return float(min(2 ** (attempt - 1), 8))


def _run_curl(webhook_url: str, thread_id: str, body: bytes) -> CurlResult:
    curl = shutil.which("curl")
    if curl is None:
        raise DiscordWebhookError("curl executable is required")
    target = _thread_url(webhook_url, thread_id)
    with tempfile.TemporaryDirectory(prefix="shittim-discord-") as directory:
        headers_path = Path(directory) / "headers.txt"
        response_path = Path(directory) / "response.json"
        result = subprocess.run(  # noqa: S603 - fixed executable and arguments; no shell.
            [
                curl,
                "--silent",
                "--show-error",
                "--connect-timeout",
                "10",
                "--max-time",
                "30",
                "--header",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
                "--dump-header",
                str(headers_path),
                "--output",
                str(response_path),
                "--write-out",
                "%{http_code}",
                target,
            ],
            input=body,
            check=False,
            capture_output=True,
        )
        status_code = _status_code(result.stdout)
        headers = _read_headers(headers_path)
    return CurlResult(result.returncode, status_code, headers)


def _thread_url(webhook_url: str, thread_id: str) -> str:
    parsed = urllib.parse.urlsplit(webhook_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"thread_id": thread_id, "wait": "true"})
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _status_code(raw: bytes) -> int | None:
    try:
        value = int(raw.decode("ascii"))
    except _STATUS_CODE_ERRORS:
        return None
    return value if 100 <= value <= 599 else None


def _read_headers(path: Path) -> tuple[tuple[str, str], ...]:
    try:
        text = path.read_text(encoding="iso-8859-1")
    except OSError:
        return ()
    headers: list[tuple[str, str]] = []
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            headers.append((key.strip(), value.strip()))
    return tuple(headers)
