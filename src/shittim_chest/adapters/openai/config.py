"""Validated configuration for the OpenAI adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
class ParticipantProfile:
    """One private participant identity supplied by validated runtime configuration."""

    display_name: str = field(repr=False)
    system_prompt: str = field(repr=False)

    def __post_init__(self) -> None:
        display_name = self.display_name.strip()
        if not display_name:
            raise ValueError("participant display name must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("participant system prompt must not be empty")
        if len(self.system_prompt.encode("utf-8")) > 3_500:
            raise ValueError("participant system prompt exceeds 3,500 UTF-8 bytes")
        object.__setattr__(self, "display_name", display_name)


@dataclass(frozen=True, slots=True)
class ParticipantProfiles:
    """Private names and persona instructions for exactly three participants."""

    values: Mapping[ParticipantSlot, ParticipantProfile] = field(repr=False)

    def __post_init__(self) -> None:
        copied = dict(self.values)
        if set(copied) != set(PARTICIPANTS):
            raise ValueError("profiles must contain exactly the three participant slots")
        normalized_names = [profile.display_name.strip().casefold() for profile in copied.values()]
        if len(set(normalized_names)) != len(PARTICIPANTS):
            raise ValueError("participant display names must be distinct")
        object.__setattr__(self, "values", MappingProxyType(copied))

    def for_participant(self, participant: ParticipantSlot) -> ParticipantProfile:
        """Return one private profile for a stable participant slot."""

        try:
            return self.values[participant]
        except KeyError as error:
            raise ValueError("unknown participant slot") from error
