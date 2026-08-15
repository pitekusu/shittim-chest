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
from shittim_chest.adapters.dynamodb.serializer import DynamoItem, DynamoValue
from shittim_chest.application import DebateSnapshot
from shittim_chest.domain import PARTICIPANTS, DebatePhase, ParticipantSlot, select_winner

ARCHIVE_SCHEMA_VERSION = 1
PROJECTION_VERSION = 1


class ProjectionRejected(ValueError):
    """Raised when a source aggregate is not safe to expose."""


class ParticipantPresentation(BaseModel):
    """Public-safe presentation fields for one participant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str = Field(min_length=1, max_length=100)
    accent: str = Field(min_length=1, max_length=32)
    avatar_asset_key: str | None = Field(default=None, max_length=256)


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


def project_completed_debate(
    snapshot: DebateSnapshot,
    *,
    identity_hmac_key: bytes,
    presentation: RecordsPresentationConfig,
    projected_at: datetime,
) -> ArchiveProjection:
    """Validate and project one completed aggregate without external I/O."""

    if len(identity_hmac_key) < 32:
        raise ProjectionRejected("identity HMAC key must contain at least 32 bytes")
    if projected_at.tzinfo is None or projected_at.utcoffset() is None:
        raise ProjectionRejected("projection timestamp must be timezone-aware")
    projected_at = projected_at.astimezone(UTC)

    if snapshot.state.schema_version != SOURCE_SCHEMA_VERSION:
        raise ProjectionRejected("source aggregate schema is not current")
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
    record_id = _opaque_key(identity_hmac_key, f"record:{debate_id}")
    requester_key = _opaque_key(identity_hmac_key, f"requester:{snapshot.requester_id}")
    completed_at = snapshot.state.updated_at.astimezone(UTC).isoformat()
    projected_at_text = projected_at.isoformat()
    vote_counts = Counter(vote.candidate for vote in snapshot.votes)
    highest_votes = max(vote_counts.values())
    tie_break_applied = sum(count == highest_votes for count in vote_counts.values()) > 1

    participant_snapshot: dict[str, DynamoValue] = {
        slot.value: cast(
            DynamoValue,
            {
                "display_name": presentation.participants[slot].display_name,
                "accent": presentation.participants[slot].accent,
                "avatar_asset_key": presentation.participants[slot].avatar_asset_key,
            },
        )
        for slot in PARTICIPANTS
    }
    canonical_source = {
        "source_schema_version": snapshot.state.schema_version,
        "debate_id": debate_id,
        "attempt_id": attempt_id,
        "completed_at": completed_at,
        "question": snapshot.question,
        "requester_display_name": snapshot.requester_display_name,
        "requester_key": requester_key,
        "presentation_version": presentation.presentation_version,
        "participants": participant_snapshot,
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
    fingerprint = hashlib.sha256(_canonical_json(canonical_source)).hexdigest()
    pk = f"RECORD#{record_id}"
    common: DynamoItem = {
        "PK": pk,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
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
        "SK": f"PROJECTION#V{PROJECTION_VERSION}",
        "record_type": "projection_marker",
        "source_schema_version": snapshot.state.schema_version,
        "source_fingerprint": fingerprint,
        "presentation_version": presentation.presentation_version,
        "projected_at": projected_at_text,
    }
    items.extend((decision_item, marker_item))
    if len(items) != 12:
        raise AssertionError("archive projection must contain exactly twelve items")
    return ArchiveProjection(
        record_id=record_id, source_fingerprint=fingerprint, items=tuple(items)
    )


def _opaque_key(key: bytes, value: str) -> str:
    digest = hmac.new(key, value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
