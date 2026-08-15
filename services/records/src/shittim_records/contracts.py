"""Public-safe API contracts for Shittim Chest Records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel

RECORDS_API_SCHEMA_VERSION = 1

ParticipantSlot = Literal["participant-a", "participant-b", "participant-c"]
AvatarKind = Literal["image", "placeholder"]
CostStatus = Literal["partial", "final", "unavailable"]
CostPeriod = Literal["today", "week", "month", "all"]

NonEmptyText = Annotated[str, Field(min_length=1)]


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class PublicModel(BaseModel):
    """Strict camel-case model used at the authenticated HTTP boundary."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class AvatarRef(PublicModel):
    kind: AvatarKind
    url: str | None = None
    alt: NonEmptyText
    fallback_variant: Literal["cyan", "pink", "lavender"]


class RequesterSummary(PublicModel):
    display_name: NonEmptyText
    avatar: AvatarRef


class ParticipantSummary(PublicModel):
    slot: ParticipantSlot
    display_name: NonEmptyText
    avatar: AvatarRef


class VoteCount(PublicModel):
    participant: ParticipantSlot
    count: Annotated[int, Field(ge=0, le=3)]


class RecordResultSummary(PublicModel):
    winner: ParticipantSlot
    vote_counts: tuple[VoteCount, VoteCount, VoteCount]
    tie_break_applied: bool


class RecordListItem(PublicModel):
    schema_version: Literal[1]
    record_id: NonEmptyText
    completed_at: AwareDatetime
    question_preview: NonEmptyText
    requester: RequesterSummary
    participants: tuple[ParticipantSummary, ParticipantSummary, ParticipantSummary]
    result: RecordResultSummary


class RecordListResponse(PublicModel):
    schema_version: Literal[1]
    items: tuple[RecordListItem, ...]
    next_cursor: str | None = None


class InitialOpinionView(PublicModel):
    participant: ParticipantSlot
    summary: NonEmptyText
    proposal: NonEmptyText


class FinalProposalView(PublicModel):
    participant: ParticipantSlot
    title: NonEmptyText
    proposal: NonEmptyText


class VoteView(PublicModel):
    voter: ParticipantSlot
    candidate: ParticipantSlot
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class FinalDecisionView(PublicModel):
    winner: ParticipantSlot
    victory_message: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    decision: NonEmptyText
    actions: tuple[NonEmptyText, ...]
    caveats: tuple[NonEmptyText, ...]


class RecordDetailResponse(PublicModel):
    schema_version: Literal[1]
    record_id: NonEmptyText
    completed_at: AwareDatetime
    question: NonEmptyText
    requester: RequesterSummary
    participants: tuple[ParticipantSummary, ParticipantSummary, ParticipantSummary]
    initial_opinions: tuple[InitialOpinionView, InitialOpinionView, InitialOpinionView]
    final_proposals: tuple[FinalProposalView, FinalProposalView, FinalProposalView]
    votes: tuple[VoteView, VoteView, VoteView]
    result: RecordResultSummary
    final_decision: FinalDecisionView


class SessionUser(PublicModel):
    display_name: NonEmptyText
    avatar: AvatarRef


class AuthenticatedSession(PublicModel):
    schema_version: Literal[1]
    authenticated: Literal[True]
    user: SessionUser
    csrf_token: NonEmptyText


class AnonymousSession(PublicModel):
    schema_version: Literal[1]
    authenticated: Literal[False]
    user: None = None
    csrf_token: None = None


SessionState = AuthenticatedSession | AnonymousSession


class SessionResponse(RootModel[SessionState]):
    """Authenticated or anonymous session state with no ambiguous combinations."""


class RankingEntry(PublicModel):
    rank: Annotated[int, Field(ge=1)]
    display_name: NonEmptyText
    avatar: AvatarRef
    count: Annotated[int, Field(ge=0)]


class RankingsResponse(PublicModel):
    schema_version: Literal[1]
    wins: tuple[RankingEntry, ...]
    requests: tuple[RankingEntry, ...]
    generated_at: AwareDatetime


class CostBreakdown(PublicModel):
    fargate: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]
    lambda_: Annotated[str, Field(alias="lambda", pattern=r"^[0-9]+(?:\.[0-9]+)?$")]
    openai: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]


class CostsResponse(PublicModel):
    schema_version: Literal[1]
    period: CostPeriod
    currency: Literal["USD"]
    total: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]
    breakdown: CostBreakdown
    updated_at: AwareDatetime
    status: CostStatus


class ErrorBody(PublicModel):
    code: NonEmptyText
    message: NonEmptyText
    request_id: NonEmptyText


class ErrorResponse(PublicModel):
    error: ErrorBody


PUBLIC_RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    RecordListResponse,
    RecordDetailResponse,
    SessionResponse,
    RankingsResponse,
    CostsResponse,
    ErrorResponse,
)
