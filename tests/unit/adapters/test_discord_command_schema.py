from __future__ import annotations

import copy
import hashlib
from typing import cast

import pytest

from shittim_chest.adapters.discord.command_schema import (
    COMMAND_DESCRIPTION,
    QUESTION_DESCRIPTION,
    CommandInventoryState,
    CommandSchemaError,
    assert_remote_command_matches,
    canonical_command_json,
    canonical_command_payload,
    classify_command_inventory,
    command_schema_hash,
    normalize_remote_command,
)


def test_canonical_schema_preserves_the_existing_command_semantics() -> None:
    assert canonical_command_payload() == {
        "name": "shittim",
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
    encoded = canonical_command_json().encode("utf-8")
    assert b"\\u" not in encoded
    assert command_schema_hash() == hashlib.sha256(encoded).hexdigest()
    assert len(command_schema_hash()) == 64


def test_normalize_ignores_only_known_discord_generated_fields() -> None:
    remote = canonical_command_payload()
    remote.update(
        {
            "id": "1",
            "application_id": "2",
            "guild_id": "3",
            "version": "4",
            "default_member_permissions": None,
            "contexts": [0],
            "integration_types": [0],
        }
    )
    options = cast(list[dict[str, object]], remote["options"])
    option = options[0]
    assert isinstance(option, dict)
    option["name_localizations"] = None
    option["description_localizations"] = None

    assert normalize_remote_command(remote) == canonical_command_payload()
    assert_remote_command_matches(remote)


def test_normalize_rejects_unknown_owned_semantics() -> None:
    remote = canonical_command_payload()
    remote["unexpected"] = True
    with pytest.raises(CommandSchemaError, match="unknown fields"):
        normalize_remote_command(remote)

    option_remote = canonical_command_payload()
    options = cast(list[dict[str, object]], option_remote["options"])
    option = options[0]
    assert isinstance(option, dict)
    option["choices"] = [{"name": "unsafe", "value": "unsafe"}]
    with pytest.raises(CommandSchemaError, match="option contains unknown"):
        normalize_remote_command(option_remote)


def test_inventory_accepts_only_empty_or_one_known_chat_input_command() -> None:
    assert classify_command_inventory(()) is CommandInventoryState.EMPTY
    assert classify_command_inventory((canonical_command_payload(),)) is CommandInventoryState.MATCH

    old = copy.deepcopy(canonical_command_payload())
    old["description"] = "previous known description"
    assert classify_command_inventory((old,)) is CommandInventoryState.RECONCILE

    with pytest.raises(CommandSchemaError, match="not singular"):
        classify_command_inventory((canonical_command_payload(), canonical_command_payload()))
    with pytest.raises(CommandSchemaError, match="unknown command"):
        classify_command_inventory(({"name": "other", "type": 1},))


def test_semantic_changes_change_the_hash_or_fail_exact_match() -> None:
    changed = canonical_command_payload()
    options = cast(list[dict[str, object]], changed["options"])
    option = options[0]
    assert isinstance(option, dict)
    option["max_length"] = 999
    with pytest.raises(CommandSchemaError, match="does not match"):
        assert_remote_command_matches(changed)
