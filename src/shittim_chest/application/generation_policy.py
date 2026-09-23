"""SDK-independent OpenAI generation policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final


@unique
class GenerationPolicyId(StrEnum):
    LUNA_STANDARD = "luna_standard"


@unique
class ReasoningEffort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@unique
class ReasoningMode(StrEnum):
    STANDARD = "standard"
    PRO = "pro"


@dataclass(frozen=True, slots=True)
class PhaseBudget:
    reasoning_effort: ReasoningEffort
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("max output tokens must be positive")


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    """One immutable model/mode/budget choice used for an entire generation run."""

    policy_id: GenerationPolicyId
    model: str
    reasoning_mode: ReasoningMode
    affection: PhaseBudget
    initial_opinion: PhaseBudget
    final_proposal: PhaseBudget
    vote: PhaseBudget
    decision: PhaseBudget
    preferences: PhaseBudget = PhaseBudget(ReasoningEffort.MEDIUM, 2_000)
    candidates: PhaseBudget = PhaseBudget(ReasoningEffort.HIGH, 4_000)

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("OpenAI model must not be empty")


def _policy(
    policy_id: GenerationPolicyId,
    model: str,
    reasoning_mode: ReasoningMode,
) -> GenerationPolicy:
    return GenerationPolicy(
        policy_id=policy_id,
        model=model,
        reasoning_mode=reasoning_mode,
        affection=PhaseBudget(ReasoningEffort.MEDIUM, 512),
        initial_opinion=PhaseBudget(ReasoningEffort.HIGH, 2_400),
        final_proposal=PhaseBudget(ReasoningEffort.HIGH, 4_000),
        vote=PhaseBudget(ReasoningEffort.MEDIUM, 800),
        decision=PhaseBudget(ReasoningEffort.HIGH, 2_400),
    )


LUNA_STANDARD: Final = _policy(
    GenerationPolicyId.LUNA_STANDARD,
    "gpt-6-luna",
    ReasoningMode.STANDARD,
)
# The production bootstrap must use this invariant.
PRODUCTION_POLICY: Final = LUNA_STANDARD
