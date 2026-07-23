# SPDX-License-Identifier: MIT
"""Typed payload models shared by notification workflows."""

from __future__ import annotations

from dataclasses import dataclass

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class DiscordField:
    """One Discord embed field."""

    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True, slots=True)
class DiscordEmbed:
    """Provider-independent representation of a Discord rich embed."""

    title: str
    description: str
    color: int
    url: str
    fields: tuple[DiscordField, ...]
    timestamp: str
    footer: str = "pitekusu/shittim-chest"


@dataclass(frozen=True, slots=True)
class ConclusionPresentation:
    """Deterministic visual and operational treatment of a workflow result."""

    icon: str
    color: int
    action: str
    mention_role: bool


@dataclass(frozen=True, slots=True)
class CurlResult:
    """Sanitized result of one curl attempt."""

    return_code: int
    status_code: int | None
    headers: tuple[tuple[str, str], ...] = ()

    def header(self, name: str) -> str | None:
        """Return the last matching response header, case-insensitively."""

        normalized = name.casefold()
        matches = [value for key, value in self.headers if key.casefold() == normalized]
        return matches[-1] if matches else None
