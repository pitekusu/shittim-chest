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
        initial_opinion=PhaseBudget(ReasoningEffort.MEDIUM, 1_200),
        final_proposal=PhaseBudget(ReasoningEffort.MEDIUM, 1_600),
        vote=PhaseBudget(ReasoningEffort.LOW, 800),
        decision=PhaseBudget(ReasoningEffort.MEDIUM, 1_200),
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
