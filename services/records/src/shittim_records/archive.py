"""Pure Records Archive projection from a validated debate snapshot."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION as SOURCE_SCHEMA_VERSION,
)
from shittim_chest.adapters.dynamodb.serializer import (
    PREVIOUS_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
)
from shittim_chest.application import DebateSnapshot
from shittim_chest.domain import (
    PARTICIPANTS,
    DebateId,
    DebatePhase,
    ParticipantSlot,
    select_winner,
)

ARCHIVE_SCHEMA_VERSION = 2
ARCHIVE_V1_SOURCE_SCHEMA_VERSION = 7


class ProjectionRejected(ValueError):
    """Raised when a source aggregate is not safe to expose."""


class ParticipantPresentation(BaseModel):
    """Public-safe presentation fields for one participant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str = Field(min_length=1, max_length=100)
    accent: str = Field(min_length=1, max_length=32)
    # Kept as a null-only compatibility member for the deployed v0001 SSM document.
    # Participant avatars are current profile assets and are never archived per debate.
    avatar_asset_key: None = None


class RecordsPresentationConfig(BaseModel):
    """Versioned public presentation configuration loaded from SSM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    presentation_version: str = Field(pattern=r"^v[0-9]{4}$")
    participants: dict[ParticipantSlot, ParticipantPresentation]

    @model_validator(mode="after")
    def validate_participants(self) -> RecordsPresentationConfig:
        if set(self.participants) != set(PARTICIPANTS):
            raise ValueError("presentation must contain exactly the three participant slots")
        names = tuple(profile.display_name.strip() for profile in self.participants.values())
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("participant display names must be non-empty and unique")
        return self


@dataclass(frozen=True, slots=True)
class ArchiveProjection:
    """One immutable set of DynamoDB items for a completed debate."""

    record_id: str
    source_fingerprint: str
    items: tuple[DynamoItem, ...]
    schema_version: int = 1


def project_completed_debate(
    snapshot: DebateSnapshot,
    *,
    identity_hmac_key: bytes,
    presentation: RecordsPresentationConfig,
    projected_at: datetime,
    source_schema_version: int | None = None,
) -> ArchiveProjection:
    """Validate and project one completed aggregate without external I/O."""

    if len(identity_hmac_key) < 32:
        raise ProjectionRejected("identity HMAC key must contain at least 32 bytes")
    if projected_at.tzinfo is None or projected_at.utcoffset() is None:
        raise ProjectionRejected("projection timestamp must be timezone-aware")
    projected_at = projected_at.astimezone(UTC)

    if snapshot.state.schema_version not in {PREVIOUS_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION}:
        raise ProjectionRejected("source aggregate schema is not compatible")
    persisted_schema_version = source_schema_version or snapshot.state.schema_version
    if persisted_schema_version not in {PREVIOUS_SCHEMA_VERSION, SOURCE_SCHEMA_VERSION}:
        raise ProjectionRejected("persisted source aggregate schema is not compatible")
    if snapshot.state.phase is not DebatePhase.COMPLETED:
        raise ProjectionRejected("only completed debates may be projected")
    if not snapshot.terminal_delivery_complete:
        raise ProjectionRejected("terminal delivery is not complete")
    if snapshot.final_decision is None:
        raise ProjectionRejected("completed debate has no final decision")
    if len(snapshot.initial_opinions) != len(PARTICIPANTS):
        raise ProjectionRejected("completed debate must have three initial opinions")
    if len(snapshot.final_proposals) != len(PARTICIPANTS):
        raise ProjectionRejected("completed debate must have three final proposals")
    if len(snapshot.votes) != len(PARTICIPANTS):
        raise ProjectionRejected("completed debate must have three votes")

    opinions = {opinion.participant: opinion for opinion in snapshot.initial_opinions}
    proposals = {proposal.participant: proposal for proposal in snapshot.final_proposals}
    votes = {vote.voter: vote for vote in snapshot.votes}
    if set(opinions) != set(PARTICIPANTS) or set(proposals) != set(PARTICIPANTS):
        raise ProjectionRejected("participant content is incomplete or duplicated")
    if set(votes) != set(PARTICIPANTS):
        raise ProjectionRejected("ballot is incomplete or duplicated")

    voting_result = select_winner(snapshot.votes)
    if voting_result.winner is not snapshot.final_decision.winner:
        raise ProjectionRejected("stored winner does not match the Python winner rule")

    debate_id = str(snapshot.state.debate_id)
    attempt_id = str(snapshot.state.attempt_id)
    record_id = derive_record_key(identity_hmac_key, debate_id)
    requester_key = derive_requester_key(identity_hmac_key, snapshot.requester_id)
    completed_at = snapshot.state.updated_at.astimezone(UTC).isoformat()
    projected_at_text = projected_at.isoformat()
    vote_counts = Counter(vote.candidate for vote in snapshot.votes)
    highest_votes = max(vote_counts.values())
    tie_break_applied = sum(count == highest_votes for count in vote_counts.values()) > 1
    archive_schema_version = (
        ARCHIVE_SCHEMA_VERSION if snapshot.affection_assessment is not None else 1
    )
    canonical_source_schema_version = (
        persisted_schema_version
        if archive_schema_version == ARCHIVE_SCHEMA_VERSION
        else ARCHIVE_V1_SOURCE_SCHEMA_VERSION
    )

    participant_snapshot: dict[str, DynamoValue] = {
        slot.value: cast(
            DynamoValue,
            {
                "display_name": presentation.participants[slot].display_name,
                "accent": presentation.participants[slot].accent,
            },
        )
        for slot in PARTICIPANTS
    }
    # Preserve the v1 fingerprint input used by already-projected records. The deployed
    # presentation has always required this compatibility member to be null, while new
    # Archive META items intentionally omit it.
    fingerprint_participant_snapshot: dict[str, DynamoValue] = {
        slot.value: cast(
            DynamoValue,
            {
                "display_name": presentation.participants[slot].display_name,
                "accent": presentation.participants[slot].accent,
                "avatar_asset_key": None,
            },
        )
        for slot in PARTICIPANTS
    }
    affection: dict[str, DynamoValue] | None = None
    if snapshot.affection_assessment is not None:
        assessment = snapshot.affection_assessment
        affection = {
            "status": assessment.status.value,
            "rubric_version": assessment.rules_version,
            "participants": cast(
                list[DynamoValue],
                [
                    {
                        "participant": entry.participant.value,
                        "before": entry.before,
                        "question_score": entry.question_score,
                        "applied_delta": entry.applied_delta,
                        "after": entry.after,
                    }
                    for entry in assessment.participants
                ],
            ),
        }
    canonical_source = {
        "source_schema_version": canonical_source_schema_version,
        "debate_id": debate_id,
        "attempt_id": attempt_id,
        "completed_at": completed_at,
        "question": snapshot.question,
        "requester_display_name": snapshot.requester_display_name,
        "requester_key": requester_key,
        "presentation_version": presentation.presentation_version,
        "participants": fingerprint_participant_snapshot,
        "initial_opinions": [
            {
                "participant": slot.value,
                "summary": opinions[slot].summary,
                "proposal": opinions[slot].proposal,
            }
            for slot in PARTICIPANTS
        ],
        "final_proposals": [
            {
                "participant": slot.value,
                "title": proposals[slot].title,
                "proposal": proposals[slot].proposal,
            }
            for slot in PARTICIPANTS
        ],
        "votes": [
            {
                "voter": slot.value,
                "candidate": votes[slot].candidate.value,
                "accuracy_score": votes[slot].accuracy_score,
                "usefulness_score": votes[slot].usefulness_score,
                "safety_score": votes[slot].safety_score,
                "reason": votes[slot].reason,
            }
            for slot in PARTICIPANTS
        ],
        "final_decision": {
            "winner": snapshot.final_decision.winner.value,
            "victory_message": snapshot.final_decision.victory_message,
            "decision": snapshot.final_decision.decision,
            "actions": list(snapshot.final_decision.actions),
            "caveats": list(snapshot.final_decision.caveats),
        },
    }
    if affection is not None:
        canonical_source["affection"] = affection
    fingerprint = hashlib.sha256(_canonical_json(canonical_source)).hexdigest()
    pk = f"RECORD#{record_id}"
    common: DynamoItem = {
        "PK": pk,
        "schema_version": archive_schema_version,
        "record_id": record_id,
    }
    meta_item: DynamoItem = {
        **common,
        "SK": "META",
        "record_type": "archive_meta",
        "completed_at": completed_at,
        "question": snapshot.question,
        "requester_display_name": snapshot.requester_display_name,
        "requester_key": requester_key,
        "participants": participant_snapshot,
        "winner": snapshot.final_decision.winner.value,
        "vote_counts": {slot.value: vote_counts.get(slot, 0) for slot in PARTICIPANTS},
        "tie_break_applied": tie_break_applied,
        "presentation_version": presentation.presentation_version,
        "gsi1pk": "ARCHIVE#COMPLETED",
        "gsi1sk": f"{completed_at}#{record_id}",
        "gsi2pk": f"WINNER#{snapshot.final_decision.winner.value}",
        "gsi2sk": f"{completed_at}#{record_id}",
        "gsi3pk": f"REQUESTER#{requester_key}",
        "gsi3sk": f"{completed_at}#{record_id}",
    }
    if affection is not None:
        meta_item["affection"] = affection
    items: list[DynamoItem] = [meta_item]
    items.extend(
        {
            **common,
            "SK": f"INITIAL#{slot.value}",
            "record_type": "initial_opinion",
            "participant": slot.value,
            "summary": opinions[slot].summary,
            "proposal": opinions[slot].proposal,
        }
        for slot in PARTICIPANTS
    )
    items.extend(
        {
            **common,
            "SK": f"FINAL#{slot.value}",
            "record_type": "final_proposal",
            "participant": slot.value,
            "title": proposals[slot].title,
            "proposal": proposals[slot].proposal,
        }
        for slot in PARTICIPANTS
    )
    items.extend(
        {
            **common,
            "SK": f"VOTE#{slot.value}",
            "record_type": "vote",
            "voter": slot.value,
            "candidate": votes[slot].candidate.value,
            "accuracy_score": votes[slot].accuracy_score,
            "usefulness_score": votes[slot].usefulness_score,
            "safety_score": votes[slot].safety_score,
            "reason": votes[slot].reason,
        }
        for slot in PARTICIPANTS
    )
    decision_item: DynamoItem = {
        **common,
        "SK": "DECISION",
        "record_type": "final_decision",
        "winner": snapshot.final_decision.winner.value,
        "victory_message": snapshot.final_decision.victory_message,
        "decision": snapshot.final_decision.decision,
        "actions": cast(list[DynamoValue], list(snapshot.final_decision.actions)),
        "caveats": cast(list[DynamoValue], list(snapshot.final_decision.caveats)),
    }
    marker_item: DynamoItem = {
        **common,
        "SK": f"PROJECTION#V{archive_schema_version}",
        "record_type": "projection_marker",
        "source_schema_version": canonical_source_schema_version,
        "source_fingerprint": fingerprint,
        "presentation_version": presentation.presentation_version,
        "projected_at": projected_at_text,
    }
    items.extend((decision_item, marker_item))
    if len(items) != 12:
        raise AssertionError("archive projection must contain exactly twelve items")
    return ArchiveProjection(
        record_id=record_id,
        source_fingerprint=fingerprint,
        items=tuple(items),
        schema_version=archive_schema_version,
    )


def _opaque_key(key: bytes, value: str) -> str:
    digest = hmac.new(key, value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def derive_requester_key(key: bytes, requester_id: str) -> str:
    """Derive the requester identity shared by projection and OAuth profiles."""

    if len(key) < 32 or not requester_id:
        raise ValueError("requester key input is invalid")
    return _opaque_key(key, f"requester:{requester_id}")


def derive_record_key(key: bytes, debate_id: str) -> str:
    """Derive the public Records identity for one private source debate ID."""

    if len(key) < 32 or not debate_id:
        raise ValueError("record key input is invalid")
    try:
        canonical_debate_id = str(DebateId.parse(debate_id))
    except ValueError:
        raise ValueError("record key debate ID is invalid") from None
    if canonical_debate_id != debate_id:
        raise ValueError("record key debate ID is not canonical")
    return _opaque_key(key, f"record:{debate_id}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
