"""Tests for strongly typed domain identifiers."""

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from shittim_chest.domain import AttemptId, DebateId


def test_new_debate_id_is_uuid7_and_unique() -> None:
    debate_ids = {DebateId.new() for _ in range(100)}

    assert len(debate_ids) == 100
    assert all(debate_id.value.version == 7 for debate_id in debate_ids)


def test_parse_round_trips_canonical_uuid7() -> None:
    original = DebateId.new()

    parsed = DebateId.parse(str(original))

    assert parsed == original
    assert str(parsed) == str(parsed.value)


def test_non_uuid7_is_rejected() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        DebateId(uuid4())


def test_invalid_uuid_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        DebateId.parse("not-a-uuid")


def test_identifier_is_frozen_and_slotted() -> None:
    debate_id = DebateId.new()

    assert not hasattr(debate_id, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(debate_id, "value", UUID(int=0))  # noqa: B010


def test_attempt_id_is_uuid7_and_round_trips() -> None:
    attempt_id = AttemptId.new()

    assert attempt_id.value.version == 7
    assert AttemptId.parse(str(attempt_id)) == attempt_id


def test_attempt_id_rejects_non_uuid7() -> None:
    with pytest.raises(ValueError, match=r"attempt ID.*UUIDv7"):
        AttemptId(uuid4())


def test_attempt_id_is_a_distinct_frozen_type() -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId(debate_id.value)

    assert type(attempt_id) is AttemptId
    assert attempt_id.value == debate_id.value
    assert not hasattr(attempt_id, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(attempt_id, "value", UUID(int=0))  # noqa: B010
