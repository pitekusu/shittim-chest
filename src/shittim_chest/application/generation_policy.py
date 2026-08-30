"""SDK-independent production and evaluation-only OpenAI generation policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final


@unique
class GenerationPolicyId(StrEnum):
    LUNA_STANDARD = "luna_standard"
    TERRA_STANDARD = "terra_standard"
    LUNA_PRO = "luna_pro"


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
    "gpt-5.6-luna",
    ReasoningMode.STANDARD,
)
# The production bootstrap must use this invariant. Alternative policies below
# remain available only to the explicit, repository-external evaluation tool.
PRODUCTION_POLICY: Final = LUNA_STANDARD
TERRA_STANDARD: Final = _policy(
    GenerationPolicyId.TERRA_STANDARD,
    "gpt-5.6-terra",
    ReasoningMode.STANDARD,
)
LUNA_PRO: Final = _policy(
    GenerationPolicyId.LUNA_PRO,
    "gpt-5.6-luna",
    ReasoningMode.PRO,
)
