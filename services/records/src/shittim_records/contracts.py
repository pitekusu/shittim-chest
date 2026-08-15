"""Public-safe API contracts for Shittim Chest Records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, model_validator

RECORDS_API_SCHEMA_VERSION = 1

ParticipantSlot = Literal["participant-a", "participant-b", "participant-c"]
AvatarKind = Literal["image", "placeholder"]
CostStatus = Literal["partial", "final", "unavailable"]
CostPeriod = Literal["today", "week", "month", "all"]

NonEmptyText = Annotated[str, Field(min_length=1)]
_ALL_PARTICIPANT_SLOTS = frozenset[ParticipantSlot](
    {"participant-a", "participant-b", "participant-c"}
)


def _complete_slot_json_schema(field_name: str) -> dict[str, object]:
    return {
        "allOf": [
            {
                "contains": {
                    "properties": {field_name: {"const": slot}},
                    "required": [field_name],
                    "type": "object",
                },
                "maxContains": 1,
                "minContains": 1,
            }
            for slot in sorted(_ALL_PARTICIPANT_SLOTS)
        ]
    }


def _require_complete_slots(slots: tuple[ParticipantSlot, ...], field_name: str) -> None:
    if frozenset(slots) != _ALL_PARTICIPANT_SLOTS:
        raise ValueError(f"{field_name} must contain every participant slot exactly once")


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


ParticipantCollection = Annotated[
    tuple[ParticipantSummary, ParticipantSummary, ParticipantSummary],
    Field(json_schema_extra=_complete_slot_json_schema("slot")),
]


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
    participants: ParticipantCollection
    result: RecordResultSummary

    @model_validator(mode="after")
    def require_complete_participants(self) -> RecordListItem:
        _require_complete_slots(tuple(item.slot for item in self.participants), "participants")
        return self


class RecordListResponse(PublicModel):
    schema_version: Literal[1]
    items: tuple[RecordListItem, ...]
    next_cursor: str | None = None


class InitialOpinionView(PublicModel):
    participant: ParticipantSlot
    summary: NonEmptyText
    proposal: NonEmptyText


InitialOpinionCollection = Annotated[
    tuple[InitialOpinionView, InitialOpinionView, InitialOpinionView],
    Field(json_schema_extra=_complete_slot_json_schema("participant")),
]


class FinalProposalView(PublicModel):
    participant: ParticipantSlot
    title: NonEmptyText
    proposal: NonEmptyText


FinalProposalCollection = Annotated[
    tuple[FinalProposalView, FinalProposalView, FinalProposalView],
    Field(json_schema_extra=_complete_slot_json_schema("participant")),
]


class VoteView(PublicModel):
    voter: ParticipantSlot
    candidate: ParticipantSlot
    reason: Annotated[str, Field(min_length=1, max_length=500)]


VoteCollection = Annotated[
    tuple[VoteView, VoteView, VoteView],
    Field(json_schema_extra=_complete_slot_json_schema("voter")),
]


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
    participants: ParticipantCollection
    initial_opinions: InitialOpinionCollection
    final_proposals: FinalProposalCollection
    votes: VoteCollection
    result: RecordResultSummary
    final_decision: FinalDecisionView

    @model_validator(mode="after")
    def require_consistent_participants_and_winner(self) -> RecordDetailResponse:
        _require_complete_slots(tuple(item.slot for item in self.participants), "participants")
        _require_complete_slots(
            tuple(item.participant for item in self.initial_opinions),
            "initial_opinions",
        )
        _require_complete_slots(
            tuple(item.participant for item in self.final_proposals),
            "final_proposals",
        )
        _require_complete_slots(tuple(item.voter for item in self.votes), "votes")
        if self.result.winner != self.final_decision.winner:
            raise ValueError("result and final_decision must identify the same winner")
        return self


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
