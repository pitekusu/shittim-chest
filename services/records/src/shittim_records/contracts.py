"""Public-safe API contracts for Shittim Chest Records."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, model_validator

RECORDS_API_SCHEMA_VERSION = 1

ParticipantSlot = Literal["participant-a", "participant-b", "participant-c"]
CostStatus = Literal["partial", "final", "unavailable"]
CostPeriod = Literal["today", "week", "month", "all"]
AdminHealthState = Literal["healthy", "warning", "critical", "unknown"]
AdminServiceName = Literal[
    "ecs",
    "ecr",
    "inspector",
    "s3",
    "dynamodb",
    "lambda",
    "cloudfront",
    "sqs",
    "apigateway",
    "eventbridge",
    "cloudformation",
    "sns",
    "ssm",
    "cost_governance",
    "signer",
    "external",
]

NonEmptyText = Annotated[str, Field(min_length=1, pattern=r"\S")]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]
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


def _no_self_vote_json_schema() -> dict[str, object]:
    return {
        "allOf": [
            {
                "if": {
                    "properties": {"voter": {"const": slot}},
                    "required": ["voter"],
                },
                "then": {"properties": {"candidate": {"not": {"const": slot}}}},
            }
            for slot in sorted(_ALL_PARTICIPANT_SLOTS)
        ]
    }


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


class ImageAvatarRef(PublicModel):
    kind: Literal["image"]
    url: NonEmptyText
    alt: NonEmptyText
    fallback_variant: Literal["cyan", "pink", "lavender"]


class PlaceholderAvatarRef(PublicModel):
    kind: Literal["placeholder"]
    url: None = None
    alt: NonEmptyText
    fallback_variant: Literal["cyan", "pink", "lavender"]


AvatarRef = ImageAvatarRef | PlaceholderAvatarRef


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


VoteCountCollection = Annotated[
    tuple[VoteCount, VoteCount, VoteCount],
    Field(json_schema_extra=_complete_slot_json_schema("participant")),
]


class RecordResultSummary(PublicModel):
    winner: ParticipantSlot
    vote_counts: VoteCountCollection
    tie_break_applied: bool

    @model_validator(mode="after")
    def require_consistent_vote_summary(self) -> RecordResultSummary:
        _require_complete_slots(
            tuple(item.participant for item in self.vote_counts),
            "vote_counts",
        )
        counts = {item.participant: item.count for item in self.vote_counts}
        if sum(counts.values()) != len(_ALL_PARTICIPANT_SLOTS):
            raise ValueError("vote_counts must account for the complete ballot")
        highest_count = max(counts.values())
        leaders = {slot for slot, count in counts.items() if count == highest_count}
        if self.winner not in leaders:
            raise ValueError("winner must be supported by the highest vote count")
        if self.tie_break_applied != (len(leaders) > 1):
            raise ValueError("tie_break_applied must match the vote count tie")
        return self


class RecordListItem(PublicModel):
    schema_version: Literal[1]
    record_id: RecordId
    completed_at: AwareDatetime
    question_preview: Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
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
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


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
    model_config = ConfigDict(json_schema_extra=_no_self_vote_json_schema())

    voter: ParticipantSlot
    candidate: ParticipantSlot
    reason: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]

    @model_validator(mode="after")
    def reject_self_vote(self) -> VoteView:
        if self.voter == self.candidate:
            raise ValueError("a participant cannot vote for itself")
        return self


VoteCollection = Annotated[
    tuple[VoteView, VoteView, VoteView],
    Field(json_schema_extra=_complete_slot_json_schema("voter")),
]


class FinalDecisionView(PublicModel):
    winner: ParticipantSlot
    victory_message: (
        Annotated[
            str,
            Field(min_length=1, max_length=500, pattern=r"\S"),
        ]
        | None
    ) = None
    decision: NonEmptyText
    actions: tuple[NonEmptyText, ...]
    caveats: tuple[NonEmptyText, ...]


class RecordDetailResponse(PublicModel):
    schema_version: Literal[1]
    record_id: RecordId
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
        ballot_counts = {slot: 0 for slot in _ALL_PARTICIPANT_SLOTS}
        for vote in self.votes:
            ballot_counts[vote.candidate] += 1
        summary_counts = {item.participant: item.count for item in self.result.vote_counts}
        if summary_counts != ballot_counts:
            raise ValueError("vote_counts must match the complete ballot")
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
    is_admin: bool = False


class AnonymousSession(PublicModel):
    schema_version: Literal[1]
    authenticated: Literal[False]
    user: None = None
    csrf_token: None = None
    is_admin: Literal[False] = False


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


CanonicalJpyAmount = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]{6}$")]


class CostBreakdown(PublicModel):
    fargate: CanonicalJpyAmount
    lambda_: Annotated[CanonicalJpyAmount, Field(alias="lambda")]
    openai: CanonicalJpyAmount
    other_aws: CanonicalJpyAmount


class CostConversion(PublicModel):
    source: Literal["frankfurter-v2"]
    method: Literal["daily-reference-rate"]
    base_currency: Literal["USD"]
    updated_at: AwareDatetime | None


class CostsResponse(PublicModel):
    schema_version: Literal[1]
    period: CostPeriod
    time_zone: Literal["Asia/Tokyo"]
    start_date: date
    end_date: date
    currency: Literal["JPY"]
    total: CanonicalJpyAmount
    breakdown: CostBreakdown
    conversion: CostConversion
    updated_at: AwareDatetime | None
    status: CostStatus


class AdminStatusMetric(PublicModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    value: str | int | bool | None


class AdminStatusSection(PublicModel):
    service: AdminServiceName
    state: AdminHealthState
    summary: Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
    metrics: tuple[AdminStatusMetric, ...]


class AdminStatusOverall(PublicModel):
    state: AdminHealthState
    critical_alarms: Annotated[int, Field(ge=0)]
    warning_alarms: Annotated[int, Field(ge=0)]
    partial: bool


class AdminStatusResponse(PublicModel):
    schema_version: Literal[1]
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    stale: bool
    overall: AdminStatusOverall
    sections: tuple[AdminStatusSection, ...]


class ErrorBody(PublicModel):
    code: NonEmptyText
    message: NonEmptyText
    request_id: NonEmptyText


class ErrorResponse(PublicModel):
    error: ErrorBody


PUBLIC_RESPONSE_MODELS: tuple[type[BaseModel], ...] = (
    RecordListResponse,
    RecordDetailResponse,
    RankingsResponse,
    CostsResponse,
    SessionResponse,
    AdminStatusResponse,
    ErrorResponse,
)
