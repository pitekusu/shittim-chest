"""Validated configuration for the OpenAI adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

DEFAULT_MODEL: Final = "gpt-5.6-luna"
ReasoningEffort = Literal["low", "medium"]


@dataclass(frozen=True, slots=True)
class PhaseSettings:
    """One Responses API phase budget."""

    reasoning_effort: ReasoningEffort
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be positive")


@dataclass(frozen=True, slots=True)
class OpenAIAdapterConfig:
    """Non-secret settings shared by one process-level OpenAI client."""

    model: str = DEFAULT_MODEL
    max_concurrency: int = 6
    initial_opinion: PhaseSettings = field(default_factory=lambda: PhaseSettings("medium", 1_200))
    final_proposal: PhaseSettings = field(default_factory=lambda: PhaseSettings("medium", 1_600))
    vote: PhaseSettings = field(default_factory=lambda: PhaseSettings("low", 800))
    decision: PhaseSettings = field(default_factory=lambda: PhaseSettings("medium", 1_200))

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("OpenAI model must not be empty")
        if not 1 <= self.max_concurrency <= 6:
            raise ValueError("OpenAI concurrency must be between 1 and 6")


@dataclass(frozen=True, slots=True)
class PersonaPrompts:
    """Private persona instructions keyed by stable public participant slots."""

    values: Mapping[ParticipantSlot, str]

    def __post_init__(self) -> None:
        copied = dict(self.values)
        if set(copied) != set(PARTICIPANTS):
            raise ValueError("persona prompts must contain exactly the three participant slots")
        for slot, prompt in copied.items():
            if not prompt.strip():
                raise ValueError(f"persona prompt must not be empty: {slot.value}")
            if len(prompt.encode("utf-8")) > 3_500:
                raise ValueError(f"persona prompt exceeds 3,500 UTF-8 bytes: {slot.value}")
        object.__setattr__(self, "values", MappingProxyType(copied))

    def for_participant(self, participant: ParticipantSlot) -> str:
        """Return the private prompt for one stable participant slot."""

        try:
            return self.values[participant]
        except KeyError as error:
            raise ValueError("unknown participant slot") from error
