"""Validated configuration for the OpenAI adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from shittim_chest.application.generation_policy import (
    PRODUCTION_POLICY,
    GenerationPolicy,
    PhaseBudget,
)
from shittim_chest.domain import PARTICIPANTS, ParticipantSlot

PhaseSettings = PhaseBudget


@dataclass(frozen=True, slots=True)
class OpenAIAdapterConfig:
    """Non-secret settings shared by one process-level OpenAI client."""

    policy: GenerationPolicy = PRODUCTION_POLICY
    max_concurrency: int = 6

    def __post_init__(self) -> None:
        if not 1 <= self.max_concurrency <= 6:
            raise ValueError("OpenAI concurrency must be between 1 and 6")

    @property
    def model(self) -> str:
        return self.policy.model

    @property
    def initial_opinion(self) -> PhaseBudget:
        return self.policy.initial_opinion

    @property
    def final_proposal(self) -> PhaseBudget:
        return self.policy.final_proposal

    @property
    def vote(self) -> PhaseBudget:
        return self.policy.vote

    @property
    def decision(self) -> PhaseBudget:
        return self.policy.decision


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
