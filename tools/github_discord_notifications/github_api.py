# SPDX-License-Identifier: MIT
"""Minimal read-only GitHub REST API client for notification workflows."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import cast

from tools.github_discord_notifications.models import JsonObject, JsonValue

API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubApiError(RuntimeError):
    """A sanitized GitHub API failure that does not expose response content."""


class GitHubClient:
    """Read JSON objects and paginated arrays with the workflow token."""

    def __init__(self, *, token: str, repository: str, timeout: float = 20.0) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError("GITHUB_REPOSITORY must use owner/name format")
        self._token = token
        self.repository = repository
        self.timeout = timeout

    def get_object(self, path: str, *, query: dict[str, str] | None = None) -> JsonObject:
        """Read one JSON object from a repository-scoped endpoint."""

        value, _ = self._get(path, query=query)
        if not isinstance(value, dict):
            raise GitHubApiError("GitHub API returned an unexpected non-object response")
        return value

    def get_array(self, path: str, *, query: dict[str, str] | None = None) -> list[JsonValue]:
        """Read one JSON array from a repository-scoped endpoint."""

        value, _ = self._get(path, query=query)
        if not isinstance(value, list):
            raise GitHubApiError("GitHub API returned an unexpected non-array response")
        return value

    def paginate_array(
        self,
        path: str,
        *,
        query: dict[str, str] | None = None,
    ) -> Iterator[JsonObject]:
        """Yield objects from every Link-header page of a top-level JSON array."""

        current_path = path
        current_query = {**(query or {}), "per_page": "100"}
        while True:
            value, next_url = self._get(current_path, query=current_query)
            if not isinstance(value, list):
                raise GitHubApiError("GitHub pagination response was not an array")
            for item in value:
                if not isinstance(item, dict):
                    raise GitHubApiError("GitHub pagination item was not an object")
                yield item
            if next_url is None:
                return
            parsed = urllib.parse.urlsplit(next_url)
            if parsed.scheme != "https" or parsed.netloc != "api.github.com":
                raise GitHubApiError("GitHub pagination returned an unexpected host")
            current_path = parsed.path
            current_query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))

    def _get(
        self,
        path: str,
        *,
        query: dict[str, str] | None,
    ) -> tuple[JsonValue, str | None]:
        if not path.startswith("/"):
            path = f"/repos/{self.repository}/{path.lstrip('/')}"
        url = f"{API_ROOT}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(  # noqa: S310 - URL is fixed to api.github.com.
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "shittim-chest-discord-notifications",
                "X-GitHub-Api-Version": API_VERSION,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
                next_url = _next_link(response.headers.get("Link"))
        except urllib.error.HTTPError as error:
            raise GitHubApiError(f"GitHub API request failed with HTTP {error.code}") from None
        except (OSError, urllib.error.URLError) as error:
            raise GitHubApiError("GitHub API request failed before receiving a response") from error
        try:
            return cast(JsonValue, json.loads(raw)), next_url
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError("GitHub API returned invalid JSON") from error


def _next_link(header: str | None) -> str | None:
    if header is None:
        return None
    for part in header.split(","):
        target, *parameters = part.strip().split(";")
        if (
            any(parameter.strip() == 'rel="next"' for parameter in parameters)
            and target.startswith("<")
            and target.endswith(">")
        ):
            return target[1:-1]
    return None


def object_value(value: JsonValue, *, label: str) -> JsonObject:
    """Require one nested JSON object with a stable diagnostic."""

    if not isinstance(value, dict):
        raise GitHubApiError(f"GitHub payload field {label} must be an object")
    return value


def string_value(value: JsonValue, *, default: str = "—") -> str:
    """Render nullable GitHub scalars without leaking complex response values."""

    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default
