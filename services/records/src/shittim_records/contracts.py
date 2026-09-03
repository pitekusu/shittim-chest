"""Public-safe API contracts for Shittim Chest Records."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    model_validator,
)

RECORDS_API_SCHEMA_VERSION = 1

ParticipantSlot = Literal["participant-a", "participant-b", "participant-c"]
CostStatus = Literal["partial", "final", "unavailable"]
CostPeriod = Literal["today", "week", "month", "all"]
AdminPromptAction = Literal["publish", "rollback"]
AdminHealthState = Literal["healthy", "warning", "critical", "unknown"]
MemorialStateName = Literal["locked", "unlocked", "queued", "generating", "ready", "failed"]
MemorialUploadContentType = Literal["image/jpeg", "image/png", "image/webp"]
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


def _admin_prompt_mode_json_schema() -> dict[str, object]:
    return {
        "allOf": [
            {
                "if": {
                    "properties": {"mode": {"const": "legacy"}},
                    "required": ["mode"],
                },
                "then": {
                    "properties": {
                        "activeRevision": {"type": "null"},
                        "createdAt": {"type": "null"},
                        "action": {"type": "null"},
                    }
                },
            },
            {
                "if": {
                    "properties": {"mode": {"const": "managed"}},
                    "required": ["mode"],
                },
                "then": {
                    "properties": {
                        "activeRevision": {"type": "string"},
                        "createdAt": {"type": "string"},
                        "action": {"type": "string"},
                    }
                },
            },
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


class AffectionParticipantView(PublicModel):
    participant: ParticipantSlot
    before: Annotated[int, Field(ge=0, le=1000)]
    question_score: Annotated[int, Field(ge=-100, le=100)] | None
    applied_delta: Annotated[int, Field(ge=-100, le=100)]
    after: Annotated[int, Field(ge=0, le=1000)]

    @model_validator(mode="after")
    def require_consistent_change(self) -> AffectionParticipantView:
        if self.after - self.before != self.applied_delta:
            raise ValueError("applied_delta must match before and after")
        if self.question_score is None and self.applied_delta != 0:
            raise ValueError("an unavailable score cannot change affection")
        return self


AffectionParticipantCollection = Annotated[
    tuple[AffectionParticipantView, AffectionParticipantView, AffectionParticipantView],
    Field(json_schema_extra=_complete_slot_json_schema("participant")),
]


class AffectionView(PublicModel):
    status: Literal["applied", "unavailable"]
    rubric_version: NonEmptyText
    participants: AffectionParticipantCollection

    @model_validator(mode="after")
    def require_complete_participants(self) -> AffectionView:
        _require_complete_slots(
            tuple(item.participant for item in self.participants),
            "participants",
        )
        if self.status == "applied" and any(
            item.question_score is None for item in self.participants
        ):
            raise ValueError("applied affection requires all question scores")
        if self.status == "unavailable" and any(
            item.question_score is not None for item in self.participants
        ):
            raise ValueError("unavailable affection cannot expose partial scores")
        return self


class RecordDetailResponse(PublicModel):
    schema_version: Literal[2]
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
    affection: AffectionView | None

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


class AffectionRankingEntry(PublicModel):
    rank: Annotated[int, Field(ge=1)]
    display_name: NonEmptyText
    avatar: AvatarRef
    score: Annotated[int, Field(ge=0, le=1000)]
    reset_count: Annotated[int, Field(ge=0)] = 0


class ParticipantAffectionRanking(PublicModel):
    participant: ParticipantSlot
    display_name: NonEmptyText
    entries: tuple[AffectionRankingEntry, ...]


AffectionRankingCollection = Annotated[
    tuple[
        ParticipantAffectionRanking,
        ParticipantAffectionRanking,
        ParticipantAffectionRanking,
    ],
    Field(json_schema_extra=_complete_slot_json_schema("participant")),
]


class AffectionRankingsResponse(PublicModel):
    schema_version: Literal[1]
    generated_at: AwareDatetime
    default_score: Literal[500]
    max_score: Literal[1000]
    rankings: AffectionRankingCollection
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None

    @model_validator(mode="after")
    def require_complete_rankings(self) -> AffectionRankingsResponse:
        _require_complete_slots(
            tuple(item.participant for item in self.rankings),
            "rankings",
        )
        return self


MemorialSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MemorialCycle = Annotated[int, Field(ge=1, le=1_000_000_000)]


def _require_exact_schema_version(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("schemaVersion must be the integer 1")
    return value


MemorialSchemaVersion = Annotated[Literal[1], BeforeValidator(_require_exact_schema_version)]


class MemorialMemorySummary(PublicModel):
    cycle: MemorialCycle
    participant: ParticipantSlot
    unlocked_at: AwareDatetime
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def require_generation_after_unlock(self) -> MemorialMemorySummary:
        if self.generated_at < self.unlocked_at:
            raise ValueError("memorial generation cannot precede its unlock")
        return self


class MemorialStateResponse(PublicModel):
    schema_version: Literal[1]
    state: MemorialStateName
    cycle: MemorialCycle
    reset_count: Annotated[int, Field(ge=0, le=999_999_999)]
    unlocked_participant: ParticipantSlot | None
    unlocked_at: AwareDatetime | None
    upload_ready: bool
    latest_ready_cycle: MemorialCycle | None
    memories: tuple[MemorialMemorySummary, ...]

    @model_validator(mode="after")
    def require_consistent_state(self) -> MemorialStateResponse:
        if self.cycle != self.reset_count + 1:
            raise ValueError("memorial cycle must follow the reset count")
        has_unlock = self.unlocked_participant is not None and self.unlocked_at is not None
        if (self.unlocked_participant is None) != (self.unlocked_at is None):
            raise ValueError("memorial unlock metadata must be complete")
        if self.state == "locked" and has_unlock:
            raise ValueError("a locked memorial cannot contain unlock metadata")
        if self.state != "locked" and not has_unlock:
            raise ValueError("an unlocked memorial requires unlock metadata")
        if self.upload_ready and self.state != "unlocked":
            raise ValueError("only an unlocked memorial can accept its reserved upload")
        if self.latest_ready_cycle is not None and self.latest_ready_cycle > self.cycle:
            raise ValueError("latest ready cycle cannot be in the future")
        if self.state == "ready" and self.latest_ready_cycle != self.cycle:
            raise ValueError("a ready memorial must expose its current cycle")
        cycles = tuple(memory.cycle for memory in self.memories)
        if cycles != tuple(sorted(set(cycles))) or any(cycle > self.cycle for cycle in cycles):
            raise ValueError("memories must contain unique ascending cycles")
        expected_latest = cycles[-1] if cycles else None
        if self.latest_ready_cycle != expected_latest:
            raise ValueError("latest ready cycle must match memories")
        if self.state == "ready":
            current = self.memories[-1]
            if (
                current.participant != self.unlocked_participant
                or current.unlocked_at != self.unlocked_at
            ):
                raise ValueError("ready memorial summary must match its unlock")
        return self


class MemorialUploadRequest(PublicModel):
    schema_version: MemorialSchemaVersion
    expected_cycle: MemorialCycle
    content_type: MemorialUploadContentType
    size_bytes: Annotated[int, Field(ge=1, le=10 * 1024 * 1024)]
    sha256: MemorialSha256


class MemorialUploadFields(PublicModel):
    key: Annotated[str, Field(min_length=1, max_length=1024, pattern=r"^[^\x00-\x1f\x7f]+$")]
    content_type: MemorialUploadContentType = Field(alias="Content-Type")
    checksum_sha256: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9+/]{43}=$"),
    ] = Field(alias="x-amz-checksum-sha256")
    algorithm: Literal["AWS4-HMAC-SHA256"] = Field(alias="x-amz-algorithm")
    credential: Annotated[str, Field(min_length=1, max_length=1024)] = Field(
        alias="x-amz-credential"
    )
    signing_date: Annotated[str, Field(pattern=r"^[0-9]{8}T[0-9]{6}Z$")] = Field(alias="x-amz-date")
    security_token: Annotated[str, Field(min_length=1, max_length=4096)] | None = Field(
        default=None,
        alias="x-amz-security-token",
    )
    policy: Annotated[str, Field(min_length=1, max_length=16384, pattern=r"^[A-Za-z0-9+/]+=*$")]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = Field(alias="x-amz-signature")


class MemorialUploadResponse(PublicModel):
    schema_version: Literal[1]
    cycle: MemorialCycle
    method: Literal["POST"]
    upload_url: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^https://")]
    expires_at: AwareDatetime
    fields: MemorialUploadFields


class MemorialGenerateRequest(PublicModel):
    schema_version: MemorialSchemaVersion
    expected_cycle: MemorialCycle
    confirmation: Literal["GENERATE MEMORIAL"]


class MemorialResetRequest(PublicModel):
    schema_version: MemorialSchemaVersion
    expected_cycle: MemorialCycle
    confirmation: Literal["RESET AFFECTION"]


class MemorialImage(PublicModel):
    url: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^https://")]
    width: Literal[1920]
    height: Literal[1080]
    alt: NonEmptyText


class MemorialMemoryResponse(PublicModel):
    schema_version: Literal[1]
    cycle: MemorialCycle
    participant: ParticipantSlot
    unlocked_at: AwareDatetime
    generated_at: AwareDatetime
    image: MemorialImage
    narrative: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]

    @model_validator(mode="after")
    def require_generation_after_unlock(self) -> MemorialMemoryResponse:
        if self.generated_at < self.unlocked_at:
            raise ValueError("memorial generation cannot precede its unlock")
        return self


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


PromptRevisionId = Annotated[
    str,
    Field(pattern=r"^r[0-9a-hjkmnp-tv-z]{26}$"),
]
PromptText = Annotated[str, Field(min_length=1, max_length=3500, pattern=r"\S")]
PromptChecksum = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AdminPromptValues(PublicModel):
    system: PromptText
    moderator: PromptText
    participant_a: PromptText
    participant_b: PromptText
    participant_c: PromptText


class AdminPromptsResponse(PublicModel):
    model_config = ConfigDict(json_schema_extra=_admin_prompt_mode_json_schema())

    schema_version: Literal[1]
    mode: Literal["legacy", "managed"]
    active_revision: PromptRevisionId | None
    created_at: AwareDatetime | None
    action: AdminPromptAction | None
    prompts: AdminPromptValues

    @model_validator(mode="after")
    def require_consistent_mode(self) -> AdminPromptsResponse:
        metadata = (self.active_revision, self.created_at, self.action)
        if (self.mode == "managed" and any(value is None for value in metadata)) or (
            self.mode == "legacy" and any(value is not None for value in metadata)
        ):
            raise ValueError("managed prompt metadata must be complete")
        return self


class AdminPromptApplyRequest(PublicModel):
    schema_version: Literal[1]
    base_revision: PromptRevisionId | None
    prompts: AdminPromptValues
    system_confirmation: Annotated[str, Field(max_length=64)] | None


class AdminPromptRollbackRequest(PublicModel):
    schema_version: Literal[1]
    base_revision: PromptRevisionId
    source_revision: PromptRevisionId
    system_confirmation: Annotated[str, Field(max_length=64)] | None


class AdminPromptApplyResponse(PublicModel):
    schema_version: Literal[1]
    revision: PromptRevisionId
    state: Literal["saved"]


class AdminPromptRevisionSummary(PublicModel):
    revision: PromptRevisionId
    created_at: AwareDatetime
    action: AdminPromptAction
    base_revision: PromptRevisionId | None
    source_revision: PromptRevisionId | None
    checksum: PromptChecksum


class AdminPromptRevisionsResponse(PublicModel):
    schema_version: Literal[1]
    items: tuple[AdminPromptRevisionSummary, ...]
    next_cursor: PromptRevisionId | None


class AdminPromptRevisionResponse(PublicModel):
    schema_version: Literal[1]
    revision: PromptRevisionId
    created_at: AwareDatetime
    action: AdminPromptAction
    base_revision: PromptRevisionId | None
    source_revision: PromptRevisionId | None
    checksum: PromptChecksum
    prompts: AdminPromptValues


class AdminStatusMetric(PublicModel):
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
    value: str | int | bool | None


AdminImageTag = Annotated[str, Field(min_length=1, max_length=300, pattern=r"\S")]
AdminPackageText = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class AdminEcsDetails(PublicModel):
    kind: Literal["ecs"]
    next_task_image_tags: Annotated[
        tuple[AdminImageTag, ...],
        Field(min_length=1, max_length=100),
    ]


class AdminEcrImage(PublicModel):
    tags: Annotated[tuple[AdminImageTag, ...], Field(min_length=1, max_length=100)]
    media_type: Literal["OCI_IMAGE", "OCI_INDEX", "DOCKER_V2", "DOCKER_LIST", "OTHER"]
    size_bytes: Annotated[int, Field(ge=0)] | None
    pushed_at: AwareDatetime | None
    last_pulled_at: AwareDatetime | None


class AdminEcrDetails(PublicModel):
    kind: Literal["ecr"]
    images: tuple[AdminEcrImage, ...]


class AdminInspectorSeverityCounts(PublicModel):
    total: Annotated[int, Field(ge=0)]
    critical: Annotated[int, Field(ge=0)]
    high: Annotated[int, Field(ge=0)]
    medium: Annotated[int, Field(ge=0)]
    low: Annotated[int, Field(ge=0)]
    untriaged: Annotated[int, Field(ge=0)]


class AdminInspectorAffectedPackage(PublicModel):
    name: AdminPackageText
    installed_version: AdminPackageText
    fixed_version: AdminPackageText | None
    package_manager: AdminPackageText | None


class AdminInspectorFinding(PublicModel):
    vulnerability_id: Annotated[
        str,
        Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
    ]
    severity: Literal["critical", "high"]
    summary_ja: Annotated[str, Field(min_length=100, max_length=300, pattern=r"\S")] | None
    affected_packages: Annotated[
        tuple[AdminInspectorAffectedPackage, ...],
        Field(min_length=1, max_length=100),
    ]
    fix_available: Literal["YES", "NO", "PARTIAL"] | None


class AdminInspectorImage(PublicModel):
    tags: Annotated[tuple[AdminImageTag, ...], Field(min_length=1, max_length=100)]
    scan_status: Literal["ACTIVE", "INACTIVE", "UNKNOWN"]
    last_scanned_at: AwareDatetime | None
    counts: AdminInspectorSeverityCounts
    findings: tuple[AdminInspectorFinding, ...]


class AdminInspectorDetails(PublicModel):
    kind: Literal["inspector"]
    images: tuple[AdminInspectorImage, ...]


AdminStatusDetails = AdminEcsDetails | AdminEcrDetails | AdminInspectorDetails


class AdminStatusSection(PublicModel):
    service: AdminServiceName
    state: AdminHealthState
    summary: Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
    metrics: tuple[AdminStatusMetric, ...]
    details: AdminStatusDetails | None = None


AdminAlarmCode = Literal[
    "bot-not-ready",
    "heartbeat-stale",
    "ingress-runtime-mismatch",
    "idle-still-running",
    "reconciler-failure",
    "status-publish-failure",
    "outbox-backlog",
    "dynamo-db-throttle",
]


class AdminActiveAlarm(PublicModel):
    code: AdminAlarmCode
    severity: Literal["critical", "warning"]
    service: AdminServiceName


class AdminStatusOverall(PublicModel):
    state: AdminHealthState
    critical_alarms: Annotated[int, Field(ge=0)]
    warning_alarms: Annotated[int, Field(ge=0)]
    partial: bool
    active_alarms: tuple[AdminActiveAlarm, ...] = ()


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
    AffectionRankingsResponse,
    CostsResponse,
    MemorialStateResponse,
    MemorialUploadResponse,
    MemorialMemoryResponse,
    SessionResponse,
    AdminPromptsResponse,
    AdminPromptApplyResponse,
    AdminPromptRevisionsResponse,
    AdminPromptRevisionResponse,
    AdminStatusResponse,
    ErrorResponse,
)

PUBLIC_REQUEST_MODELS: tuple[type[BaseModel], ...] = (
    MemorialUploadRequest,
    MemorialGenerateRequest,
    MemorialResetRequest,
    AdminPromptApplyRequest,
    AdminPromptRollbackRequest,
)
