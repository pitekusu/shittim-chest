"""Pure canonical schema and fail-closed Discord command inventory checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum, unique

from shittim_chest.application import SHITTIM_COMMAND_NAME

COMMAND_NAME = SHITTIM_COMMAND_NAME
COMMAND_DESCRIPTION = "3つの視点で質問を合議します"
QUESTION_DESCRIPTION = "合議したい質問"
COMMAND_SCHEMA_VERSION = 1

_GENERATED_COMMAND_FIELDS = frozenset(
    {
        "application_id",
        "contexts",
        "default_member_permissions",
        "default_permission",
        "dm_permission",
        "guild_id",
        "handler",
        "id",
        "integration_types",
        "name_localizations",
        "description_localizations",
        "nsfw",
        "version",
    }
)
_OWNED_COMMAND_FIELDS = frozenset({"name", "description", "type", "options"})
_GENERATED_OPTION_FIELDS = frozenset({"name_localizations", "description_localizations"})
_OWNED_OPTION_FIELDS = frozenset(
    {"name", "description", "type", "required", "min_length", "max_length"}
)


class CommandSchemaError(ValueError):
    """A remote command cannot be safely compared with the owned schema."""


@unique
class CommandInventoryState(StrEnum):
    """Safe deploy-time states for the moderator Guild command inventory."""

    EMPTY = "empty"
    MATCH = "match"
    RECONCILE = "reconcile"


def canonical_command_payload() -> dict[str, object]:
    """Return a fresh Discord API payload for the one owned Guild command."""

    return {
        "name": COMMAND_NAME,
        "description": COMMAND_DESCRIPTION,
        "type": 1,
        "options": [
            {
                "name": "question",
                "description": QUESTION_DESCRIPTION,
                "type": 3,
                "required": True,
                "min_length": 1,
                "max_length": 1000,
            }
        ],
    }


def canonical_command_json() -> str:
    """Serialize the canonical payload with one stable UTF-8 JSON contract."""

    return json.dumps(
        canonical_command_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def command_schema_hash() -> str:
    """Return the lowercase SHA-256 digest of the canonical UTF-8 JSON."""

    return hashlib.sha256(canonical_command_json().encode("utf-8")).hexdigest()


def normalize_remote_command(command: Mapping[str, object]) -> dict[str, object]:
    """Project one remote command onto owned fields and reject unknown semantics."""

    unknown = set(command) - _OWNED_COMMAND_FIELDS - _GENERATED_COMMAND_FIELDS
    if unknown:
        raise CommandSchemaError("remote command contains unknown fields")
    if set(_OWNED_COMMAND_FIELDS) - set(command):
        raise CommandSchemaError("remote command is missing owned fields")
    name = command.get("name")
    description = command.get("description")
    command_type = command.get("type")
    options = command.get("options")
    if not isinstance(name, str) or not isinstance(description, str):
        raise CommandSchemaError("remote command text fields are invalid")
    if isinstance(command_type, bool) or not isinstance(command_type, int):
        raise CommandSchemaError("remote command type is invalid")
    if not isinstance(options, list) or len(options) != 1:
        raise CommandSchemaError("remote command options are unsafe")
    option = options[0]
    if not isinstance(option, Mapping):
        raise CommandSchemaError("remote command option is invalid")
    option_unknown = set(option) - _OWNED_OPTION_FIELDS - _GENERATED_OPTION_FIELDS
    if option_unknown:
        raise CommandSchemaError("remote command option contains unknown fields")
    if set(_OWNED_OPTION_FIELDS) - set(option):
        raise CommandSchemaError("remote command option is missing owned fields")
    normalized_option: dict[str, object] = {}
    for field in ("name", "description"):
        value = option.get(field)
        if not isinstance(value, str):
            raise CommandSchemaError("remote command option text is invalid")
        normalized_option[field] = value
    for field in ("type", "min_length", "max_length"):
        value = option.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CommandSchemaError("remote command option number is invalid")
        normalized_option[field] = value
    required = option.get("required")
    if not isinstance(required, bool):
        raise CommandSchemaError("remote command option required flag is invalid")
    normalized_option["required"] = required
    return {
        "name": name,
        "description": description,
        "type": command_type,
        "options": [normalized_option],
    }


def assert_remote_command_matches(command: Mapping[str, object]) -> None:
    """Require exact equality after projection onto owned command fields."""

    if normalize_remote_command(command) != canonical_command_payload():
        raise CommandSchemaError("remote command schema does not match")


def classify_command_inventory(
    commands: Iterable[Mapping[str, object]],
) -> CommandInventoryState:
    """Classify only empty or one known ``shittim`` command as safe to manage."""

    inventory = tuple(commands)
    if not inventory:
        return CommandInventoryState.EMPTY
    if len(inventory) != 1:
        raise CommandSchemaError("remote command inventory is not singular")
    command = inventory[0]
    if command.get("name") != COMMAND_NAME or command.get("type") != 1:
        raise CommandSchemaError("remote command inventory contains an unknown command")
    normalized = normalize_remote_command(command)
    if normalized == canonical_command_payload():
        return CommandInventoryState.MATCH
    return CommandInventoryState.RECONCILE


__all__ = (
    "COMMAND_DESCRIPTION",
    "COMMAND_NAME",
    "COMMAND_SCHEMA_VERSION",
    "QUESTION_DESCRIPTION",
    "CommandInventoryState",
    "CommandSchemaError",
    "assert_remote_command_matches",
    "canonical_command_json",
    "canonical_command_payload",
    "classify_command_inventory",
    "command_schema_hash",
    "normalize_remote_command",
)
