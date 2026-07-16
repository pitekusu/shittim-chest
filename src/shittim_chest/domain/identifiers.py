"""Strong identifiers used by the debate domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self
from uuid import RFC_4122, UUID, uuid7


def _validate_uuid7(value: UUID, *, label: str) -> None:
    if value.version != 7 or value.variant != RFC_4122:
        raise ValueError(f"{label} must be an RFC 9562 UUIDv7")


@dataclass(frozen=True, slots=True)
class DebateId:
    """A UUIDv7 debate identifier.

    Debate IDs are correlation identifiers, not authentication credentials.
    """

    value: UUID

    def __post_init__(self) -> None:
        _validate_uuid7(self.value, label="debate ID")

    @classmethod
    def new(cls) -> Self:
        """Generate a new UUIDv7-backed debate ID."""

        return cls(uuid7())

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse and validate a UUIDv7 string."""

        return cls(UUID(value))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class AttemptId:
    """A UUIDv7 identifier for one immutable execution attempt."""

    value: UUID

    def __post_init__(self) -> None:
        _validate_uuid7(self.value, label="attempt ID")

    @classmethod
    def new(cls) -> Self:
        """Generate a new UUIDv7-backed attempt ID."""

        return cls(uuid7())

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse and validate a UUIDv7 string."""

        return cls(UUID(value))

    def __str__(self) -> str:
        return str(self.value)
