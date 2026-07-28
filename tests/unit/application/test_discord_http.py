"""Validation contracts for token-free Discord HTTP application inputs."""

from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

from shittim_chest.application import (
    SHITTIM_COMMAND_NAME,
    DiscordHttpOperation,
    DiscordHttpPing,
    IngressKind,
    PanelAction,
    PanelCustomId,
)
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 4, 0, tzinfo=UTC)


def command() -> DiscordHttpOperation:
    return DiscordHttpOperation(
        interaction_id="101",
        operation_id="101",
        kind=IngressKind.NEW_DEBATE,
        application_id="102",
        guild_id="103",
        channel_id="104",
        channel_type=0,
        parent_channel_id=None,
        requester_id="105",
        requester_username="private-user",
        requester_display_name="Private Display",
        can_manage_messages=False,
        received_at=NOW,
        command_name=SHITTIM_COMMAND_NAME,
        question="甘い朝ごはんは何がいい?",
    )


def test_http_input_has_no_interaction_token_field_or_private_repr() -> None:
    value = command()
    field_names = {item.name for item in fields(value)}
    representation = repr(value)

    assert "token" not in field_names
    assert "interaction_token" not in field_names
    assert "101" not in representation
    assert "102" not in representation
    assert "103" not in representation
    assert "104" not in representation
    assert "105" not in representation
    assert "甘い朝ごはん" not in representation
    assert "private-user" not in representation
    assert "Private Display" not in representation


def test_ping_requires_utc_timestamp() -> None:
    assert DiscordHttpPing(received_at=NOW).received_at is NOW
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        DiscordHttpPing(received_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"operation_id": "different"}, "operation must use"),
        ({"command_name": "another"}, "unsupported application command"),
        ({"question": " "}, "must not be blank"),
        ({"source_thread_id": "104"}, "cannot contain component context"),
        ({"channel_type": True}, "channel type"),
    ],
)
def test_command_shape_fails_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        replace(command(), **changes)


def test_component_requires_source_context_and_no_question() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    panel_id = PanelCustomId.for_attempt(
        debate_id=debate_id,
        attempt_id=attempt_id,
        action=PanelAction.CANCEL,
    )
    component = DiscordHttpOperation(
        interaction_id="201",
        operation_id=panel_id.operation_id,
        kind=IngressKind.CANCEL,
        application_id="202",
        guild_id="203",
        channel_id="204",
        channel_type=11,
        parent_channel_id="205",
        requester_id="206",
        requester_username="requester",
        requester_display_name="Requester",
        can_manage_messages=True,
        received_at=NOW,
        debate_id=debate_id,
        expected_attempt_id=attempt_id,
        custom_id=panel_id.encode(),
        source_message_id="207",
        source_thread_id="204",
    )

    assert component.kind is IngressKind.CANCEL
    assert component.debate_id == debate_id
    assert component.expected_attempt_id == attempt_id
    with pytest.raises(ValueError, match="cannot contain command input"):
        replace(component, question="question")
    with pytest.raises(ValueError, match="source message and thread"):
        replace(component, source_message_id=None)
    with pytest.raises(ValueError, match="source thread must match"):
        replace(component, source_thread_id="208")
    with pytest.raises(ValueError, match="immutable debate and attempt"):
        replace(component, expected_attempt_id=None)
    with pytest.raises(ValueError, match="immutable panel binding"):
        replace(component, debate_id=DebateId.new())
