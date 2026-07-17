"""Strict Pydantic schemas supplied directly to Responses API parsing."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from shittim_chest.domain import ParticipantSlot


class StrictOutput(BaseModel):
    """Forbid provider fields that are not part of the versioned contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


ShortListItem = Annotated[str, Field(min_length=1, max_length=500)]


class OpinionOutputV1(StrictOutput):
    """Initial opinion fields persisted by the current domain schema."""

    summary: str = Field(min_length=1, max_length=1_600)
    proposal: str = Field(min_length=1, max_length=1_600)


class FinalProposalOutputV1(StrictOutput):
    """Final proposal fields persisted by the current domain schema."""

    title: str = Field(min_length=1, max_length=200)
    proposal: str = Field(min_length=1, max_length=2_000)


class VoteOutputV1(StrictOutput):
    """One anonymous vote that is revalidated by domain invariants."""

    candidate_id: ParticipantSlot
    accuracy_score: int = Field(ge=1, le=5)
    usefulness_score: int = Field(ge=1, le=5)
    safety_score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=500)


class DecisionOutputV1(StrictOutput):
    """Final wording constrained to the mechanically selected winner."""

    decision: str = Field(min_length=1, max_length=2_000)
    actions: tuple[ShortListItem, ...] = Field(max_length=10)
    caveats: tuple[ShortListItem, ...] = Field(max_length=10)
