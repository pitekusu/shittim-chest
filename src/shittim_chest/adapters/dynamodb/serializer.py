"""Convert immutable application records to and from DynamoDB native values."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import cast

from shittim_chest.application.discord import (
    DiscordBotSlot,
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
)
from shittim_chest.application.models import DebateSnapshot, LeaseGrant
from shittim_chest.domain import (
    PARTICIPANTS,
    AttemptId,
    DebateId,
    DebatePhase,
    DebateState,
    EscalationAssessment,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSearchStatus,
    FinalDecision,
    FinalProposal,
    InitialOpinion,
    ParticipantSlot,
    RecoveryState,
    SearchRequirement,
    Vote,
)

type DynamoScalar = str | int | bool | None
type DynamoValue = DynamoScalar | list[DynamoValue] | dict[str, DynamoValue]
type DynamoItem = dict[str, DynamoValue]

CURRENT_SCHEMA_VERSION = 5
PREVIOUS_SCHEMA_VERSION = 4
MAX_ITEM_BYTES = 400 * 1024


class PersistenceFormatError(ValueError):
    """Raised when a persistence record cannot be validated or migrated."""


class ItemTooLarge(PersistenceFormatError):
    """Raised before an item can cross DynamoDB's 400 KB limit."""


def migrate_item(item: Mapping[str, DynamoValue]) -> DynamoItem:
    """Up-convert the previous record schema or validate the current one."""

    migrated = dict(item)
    version = _integer(migrated, "schema_version")
    if version == PREVIOUS_SCHEMA_VERSION:
        if migrated.get("record_type") == "outbox" and "bot_slot" not in migrated:
            migrated["bot_slot"] = migrated.pop("bot_id", None)
        migrated["schema_version"] = CURRENT_SCHEMA_VERSION
        version = CURRENT_SCHEMA_VERSION
    if version != CURRENT_SCHEMA_VERSION:
        raise PersistenceFormatError(f"unsupported schema version: {version}")
    _text(migrated, "record_type")
    _text(migrated, "PK")
    _text(migrated, "SK")
    return migrated


def serialize_snapshot(snapshot: DebateSnapshot) -> tuple[DynamoItem, ...]:
    """Vertically partition one current-attempt snapshot into table items."""

    debate_id = str(snapshot.state.debate_id)
    attempt_id = str(snapshot.state.attempt_id)
    pk = f"DEBATE#{debate_id}"
    common: DynamoItem = {
        "PK": pk,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "debate_id": debate_id,
        "created_at": _timestamp(snapshot.created_at),
        "updated_at": _timestamp(snapshot.state.updated_at),
    }
    debate_meta: DynamoItem = {
        **common,
        "SK": "META",
        "record_type": "debate_meta",
        "question": snapshot.question,
        "requester_id": snapshot.requester_id,
        "guild_id": snapshot.guild_id,
        "channel_id": snapshot.channel_id,
        "current_attempt_id": attempt_id,
    }
    _put_optional(debate_meta, "starter_message_id", snapshot.starter_message_id)
    _put_optional(debate_meta, "thread_id", snapshot.thread_id)
    _put_optional(
        debate_meta,
        "control_panel_message_id",
        snapshot.control_panel_message_id,
    )
    if snapshot.thread_id is not None:
        debate_meta["gsi1pk"] = f"THREAD#{snapshot.thread_id}"
        debate_meta["gsi1sk"] = f"DEBATE#{debate_id}"

    attempt_meta: DynamoItem = {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#META",
        "record_type": "attempt_meta",
        "attempt_id": attempt_id,
        "attempt_created_at": _timestamp(snapshot.attempt_created_at),
        "phase": snapshot.state.phase.value,
        "recovery_state": snapshot.state.recovery_state.value,
    }
    _put_optional(attempt_meta, "retry_of", _identifier(snapshot.state.retry_of))
    _put_optional(
        attempt_meta,
        "failed_from_phase",
        snapshot.state.failed_from_phase.value if snapshot.state.failed_from_phase else None,
    )
    _put_optional(attempt_meta, "error_code", snapshot.error_code)
    if snapshot.lease is not None:
        attempt_meta.update(
            {
                "lease_owner": snapshot.lease.owner_id,
                "lease_slot": snapshot.lease.slot,
                "lease_expiry": _timestamp(snapshot.lease.expires_at),
                "fencing_token": snapshot.lease.fencing_token,
            }
        )
    if not snapshot.state.phase.is_terminal:
        attempt_meta["gsi2pk"] = "RECOVERABLE"
        attempt_meta["gsi2sk"] = f"{_timestamp(snapshot.state.updated_at)}#{debate_id}#{attempt_id}"

    items = [debate_meta, attempt_meta]
    if snapshot.evidence is not None:
        items.append(
            {
                **common,
                "SK": f"ATTEMPT#{attempt_id}#EVIDENCE#META",
                "record_type": "evidence_meta",
                "attempt_id": attempt_id,
                "required_search_satisfied": snapshot.evidence.required_search_satisfied,
                "summary": snapshot.evidence.summary,
                "search_requirement": snapshot.evidence.search_requirement.value,
                "search_status": snapshot.evidence.search_status.value,
                "router_rules_version": snapshot.evidence.router_rules_version,
                "routing_reason": snapshot.evidence.routing_reason,
            }
        )
        _put_optional(items[-1], "search_response_id", snapshot.evidence.search_response_id)
        for sequence, evidence in enumerate(snapshot.evidence.items):
            items.append(_serialize_evidence(common, attempt_id, sequence, evidence))
    items.extend(
        _serialize_opinion(common, attempt_id, value) for value in snapshot.initial_opinions
    )
    items.extend(
        _serialize_proposal(common, attempt_id, value) for value in snapshot.final_proposals
    )
    items.extend(_serialize_vote(common, attempt_id, value) for value in snapshot.votes)
    if snapshot.escalation_assessment is not None:
        items.append(_serialize_escalation(common, attempt_id, snapshot.escalation_assessment))
    if snapshot.final_decision is not None:
        items.append(_serialize_decision(common, attempt_id, snapshot.final_decision))
    return tuple(_validated_item(item) for item in items)


def deserialize_snapshot(raw_items: Iterable[Mapping[str, DynamoValue]]) -> DebateSnapshot:
    """Validate and rebuild the current attempt from a DynamoDB item collection."""

    items = tuple(migrate_item(item) for item in raw_items)
    debate_meta = _one(items, "debate_meta")
    partition_key = _text(debate_meta, "PK")
    debate_id_text = _text(debate_meta, "debate_id")
    for item in items:
        if _text(item, "PK") != partition_key:
            raise PersistenceFormatError("item collection contains multiple partition keys")
        item_debate_id = _optional_text(item, "debate_id")
        if item_debate_id is not None and item_debate_id != debate_id_text:
            raise PersistenceFormatError("item collection contains multiple debate IDs")
    attempt_id = _text(debate_meta, "current_attempt_id")
    attempt_meta = _one(items, "attempt_meta", attempt_id=attempt_id)
    debate_id = DebateId.parse(debate_id_text)
    state = DebateState(
        debate_id=debate_id,
        attempt_id=AttemptId.parse(attempt_id),
        phase=DebatePhase(_text(attempt_meta, "phase")),
        recovery_state=RecoveryState(_text(attempt_meta, "recovery_state")),
        updated_at=_datetime(attempt_meta, "updated_at"),
        retry_of=_optional_attempt(attempt_meta, "retry_of"),
        failed_from_phase=_optional_phase(attempt_meta, "failed_from_phase"),
        schema_version=CURRENT_SCHEMA_VERSION,
    )
    lease = _deserialize_lease(attempt_meta)
    evidence_meta = _optional_one(items, "evidence_meta", attempt_id=attempt_id)
    evidence = None
    if evidence_meta is not None:
        evidence_items = sorted(
            _many(items, "evidence", attempt_id=attempt_id),
            key=lambda item: _integer(item, "sequence"),
        )
        legacy_empty = (
            not evidence_items
            and _optional_text(evidence_meta, "search_response_id") == "legacy-v2"
        )
        evidence = EvidenceBundle(
            items=tuple(_deserialize_evidence(item) for item in evidence_items),
            required_search_satisfied=_boolean(
                evidence_meta,
                "required_search_satisfied",
            ),
            summary=(
                ""
                if legacy_empty
                else (
                    _text(evidence_meta, "summary", allow_empty=True)
                    if "summary" in evidence_meta
                    else ""
                )
            ),
            search_requirement=SearchRequirement(
                "none"
                if legacy_empty
                else (_optional_text(evidence_meta, "search_requirement") or "none")
            ),
            search_status=EvidenceSearchStatus(
                "not_requested"
                if legacy_empty
                else (_optional_text(evidence_meta, "search_status") or "not_requested")
            ),
            search_response_id=(
                None if legacy_empty else _optional_text(evidence_meta, "search_response_id")
            ),
            router_rules_version=(
                _optional_text(evidence_meta, "router_rules_version") or "legacy-router-v0"
            ),
            routing_reason=_optional_text(evidence_meta, "routing_reason") or "legacy_migration",
        )

    opinions = _by_participant(
        InitialOpinion(
            ParticipantSlot(_text(item, "participant")),
            _text(item, "summary"),
            _text(item, "proposal"),
        )
        for item in _many(items, "initial_opinion", attempt_id=attempt_id)
    )
    proposals = _by_participant(
        FinalProposal(
            ParticipantSlot(_text(item, "participant")),
            _text(item, "title"),
            _text(item, "proposal"),
        )
        for item in _many(items, "final_proposal", attempt_id=attempt_id)
    )
    votes = _by_voter(
        Vote(
            ParticipantSlot(_text(item, "voter")),
            ParticipantSlot(_text(item, "candidate")),
            _integer(item, "accuracy_score"),
            _integer(item, "usefulness_score"),
            _integer(item, "safety_score"),
            _text(item, "reason"),
        )
        for item in _many(items, "vote", attempt_id=attempt_id)
    )
    decision_item = _optional_one(items, "decision", attempt_id=attempt_id)
    decision = None
    if decision_item is not None:
        decision = FinalDecision(
            ParticipantSlot(_text(decision_item, "winner")),
            _text(decision_item, "decision"),
            _string_tuple(decision_item, "actions"),
            _string_tuple(decision_item, "caveats"),
        )
    escalation_item = _optional_one(items, "escalation_assessment", attempt_id=attempt_id)
    escalation_assessment = None
    if escalation_item is not None:
        escalation_assessment = EscalationAssessment(
            rules_version=_text(escalation_item, "rules_version"),
            split_vote=_boolean(escalation_item, "split_vote"),
            winning_axis_low=_boolean(escalation_item, "winning_axis_low"),
            winning_average_low=_boolean(escalation_item, "winning_average_low"),
            assessed_at=_datetime(escalation_item, "assessed_at"),
            recommended_restart_phase=DebatePhase(
                _text(escalation_item, "recommended_restart_phase")
            ),
            executed=_boolean(escalation_item, "executed"),
            executed_policy_id=_optional_text(escalation_item, "executed_policy_id"),
            execution_count=_integer(escalation_item, "execution_count"),
        )
    return DebateSnapshot(
        state=state,
        question=_text(debate_meta, "question"),
        requester_id=_text(debate_meta, "requester_id"),
        guild_id=_text(debate_meta, "guild_id"),
        channel_id=_text(debate_meta, "channel_id"),
        created_at=_datetime(debate_meta, "created_at"),
        attempt_created_at=_datetime(attempt_meta, "attempt_created_at"),
        starter_message_id=_optional_text(debate_meta, "starter_message_id"),
        thread_id=_optional_text(debate_meta, "thread_id"),
        control_panel_message_id=_optional_text(debate_meta, "control_panel_message_id"),
        lease=lease,
        evidence=evidence,
        initial_opinions=cast(tuple[InitialOpinion, ...], opinions),
        final_proposals=cast(tuple[FinalProposal, ...], proposals),
        votes=votes,
        final_decision=decision,
        escalation_assessment=escalation_assessment,
        error_code=_optional_text(attempt_meta, "error_code"),
    )


def serialize_outbox(operation: OutboxOperation) -> DynamoItem:
    """Serialize one outbox operation without requiring boto3."""

    attempt_id = str(operation.attempt_id)
    item: DynamoItem = {
        "PK": f"DEBATE#{operation.debate_id}",
        "SK": f"ATTEMPT#{attempt_id}#OUTBOX#{operation.operation_id}",
        "record_type": "outbox",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "debate_id": str(operation.debate_id),
        "attempt_id": attempt_id,
        "operation_id": operation.operation_id,
        "bot_slot": operation.bot_slot.value,
        "thread_id": operation.thread_id,
        "content": operation.content,
        "content_hash": operation.content_hash,
        "nonce": operation.nonce,
        "chunk_sequence": operation.chunk_sequence,
        "status": operation.status.value,
        "created_at": _timestamp(operation.created_at),
        "updated_at": _timestamp(operation.sent_at or operation.created_at),
        "delivery_attempt": operation.delivery_attempt,
    }
    _put_optional(item, "claim_owner", operation.claim_owner)
    _put_optional(item, "claim_expiry", _optional_timestamp(operation.claim_expires_at))
    _put_optional(item, "next_retry_at", _optional_timestamp(operation.next_retry_at))
    _put_optional(item, "message_id", operation.message_id)
    _put_optional(item, "sent_at", _optional_timestamp(operation.sent_at))
    return _validated_item(item)


def deserialize_outbox(raw_item: Mapping[str, DynamoValue]) -> OutboxOperation:
    """Validate and rebuild one persisted outbox operation."""

    item = migrate_item(raw_item)
    if _text(item, "record_type") != "outbox":
        raise PersistenceFormatError("record is not an outbox operation")
    return OutboxOperation(
        operation_id=_text(item, "operation_id"),
        debate_id=DebateId.parse(_text(item, "debate_id")),
        attempt_id=AttemptId.parse(_text(item, "attempt_id")),
        bot_slot=DiscordBotSlot(_text(item, "bot_slot")),
        thread_id=_text(item, "thread_id"),
        content=_text(item, "content"),
        content_hash=_text(item, "content_hash"),
        nonce=_text(item, "nonce"),
        chunk_sequence=_integer(item, "chunk_sequence"),
        status=OutboxStatus(_text(item, "status")),
        created_at=_datetime(item, "created_at"),
        claim_owner=_optional_text(item, "claim_owner"),
        claim_expires_at=_optional_datetime(item, "claim_expiry"),
        delivery_attempt=_integer(item, "delivery_attempt"),
        next_retry_at=_optional_datetime(item, "next_retry_at"),
        message_id=_optional_text(item, "message_id"),
        sent_at=_optional_datetime(item, "sent_at"),
    )


def serialize_panel_operation(operation: PanelOperation) -> DynamoItem:
    """Serialize one idempotency and authorization binding for a Discord operation."""

    item: DynamoItem = {
        "PK": f"OPERATION#{operation.operation_id}",
        "SK": "RESULT",
        "record_type": "panel_operation",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "operation_id": operation.operation_id,
        "kind": operation.kind.value,
        "debate_id": str(operation.debate_id),
        "source_attempt_id": str(operation.source_attempt_id),
        "result_attempt_id": str(operation.result_attempt_id),
        "guild_id": operation.guild_id,
        "channel_id": operation.channel_id,
        "requester_id": operation.requester_id,
        "created_at": _timestamp(operation.created_at),
        "updated_at": _timestamp(operation.created_at),
    }
    _put_optional(item, "thread_id", operation.thread_id)
    _put_optional(item, "message_id", operation.message_id)
    return _validated_item(item)


def deserialize_panel_operation(raw_item: Mapping[str, DynamoValue]) -> PanelOperation:
    """Validate and rebuild one persisted Discord panel operation."""

    item = migrate_item(raw_item)
    if _text(item, "record_type") != "panel_operation":
        raise PersistenceFormatError("record is not a panel operation")
    return PanelOperation(
        operation_id=_text(item, "operation_id"),
        kind=PanelOperationKind(_text(item, "kind")),
        debate_id=DebateId.parse(_text(item, "debate_id")),
        source_attempt_id=AttemptId.parse(_text(item, "source_attempt_id")),
        result_attempt_id=AttemptId.parse(_text(item, "result_attempt_id")),
        guild_id=_text(item, "guild_id"),
        channel_id=_text(item, "channel_id"),
        requester_id=_text(item, "requester_id"),
        created_at=_datetime(item, "created_at"),
        thread_id=_optional_text(item, "thread_id"),
        message_id=_optional_text(item, "message_id"),
    )


def _serialize_evidence(
    common: DynamoItem,
    attempt_id: str,
    sequence: int,
    evidence: EvidenceItem,
) -> DynamoItem:
    return {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#EVIDENCE#{sequence:04d}",
        "record_type": "evidence",
        "attempt_id": attempt_id,
        "sequence": sequence,
        "source_url": evidence.source_url,
        "title": evidence.title,
        "source_metadata": evidence.source_metadata,
        "retrieved_at": evidence.retrieved_at,
        "content_hash": evidence.content_hash,
    }


def _serialize_opinion(common: DynamoItem, attempt_id: str, value: InitialOpinion) -> DynamoItem:
    return {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#INITIAL#{value.participant.value}",
        "record_type": "initial_opinion",
        "attempt_id": attempt_id,
        "participant": value.participant.value,
        "summary": value.summary,
        "proposal": value.proposal,
    }


def _serialize_proposal(common: DynamoItem, attempt_id: str, value: FinalProposal) -> DynamoItem:
    return {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#FINAL#{value.participant.value}",
        "record_type": "final_proposal",
        "attempt_id": attempt_id,
        "participant": value.participant.value,
        "title": value.title,
        "proposal": value.proposal,
    }


def _serialize_vote(common: DynamoItem, attempt_id: str, value: Vote) -> DynamoItem:
    return {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#VOTE#{value.voter.value}",
        "record_type": "vote",
        "attempt_id": attempt_id,
        "voter": value.voter.value,
        "candidate": value.candidate.value,
        "accuracy_score": value.accuracy_score,
        "usefulness_score": value.usefulness_score,
        "safety_score": value.safety_score,
        "reason": value.reason,
    }


def _serialize_decision(
    common: DynamoItem,
    attempt_id: str,
    value: FinalDecision,
) -> DynamoItem:
    return {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#DECISION",
        "record_type": "decision",
        "attempt_id": attempt_id,
        "winner": value.winner.value,
        "decision": value.decision,
        "actions": list(value.actions),
        "caveats": list(value.caveats),
    }


def _serialize_escalation(
    common: DynamoItem,
    attempt_id: str,
    value: EscalationAssessment,
) -> DynamoItem:
    item: DynamoItem = {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#ESCALATION",
        "record_type": "escalation_assessment",
        "attempt_id": attempt_id,
        "rules_version": value.rules_version,
        "split_vote": value.split_vote,
        "winning_axis_low": value.winning_axis_low,
        "winning_average_low": value.winning_average_low,
        "assessed_at": _timestamp(value.assessed_at),
        "recommended_restart_phase": value.recommended_restart_phase.value,
        "executed": value.executed,
        "execution_count": value.execution_count,
    }
    _put_optional(item, "executed_policy_id", value.executed_policy_id)
    return item


def _deserialize_evidence(item: DynamoItem) -> EvidenceItem:
    return EvidenceItem(
        source_url=_text(item, "source_url"),
        title=_text(item, "title"),
        source_metadata=_text(item, "source_metadata", allow_empty=True),
        retrieved_at=_text(item, "retrieved_at"),
        content_hash=_text(item, "content_hash"),
    )


def _deserialize_lease(item: DynamoItem) -> LeaseGrant | None:
    owner = _optional_text(item, "lease_owner")
    if owner is None:
        for field in ("lease_slot", "lease_expiry", "fencing_token"):
            if field in item:
                raise PersistenceFormatError("partial lease attributes are not allowed")
        return None
    return LeaseGrant(
        owner_id=owner,
        slot=_integer(item, "lease_slot"),
        fencing_token=_integer(item, "fencing_token"),
        expires_at=_datetime(item, "lease_expiry"),
    )


def _validated_item(item: DynamoItem) -> DynamoItem:
    size = sum(len(name.encode("utf-8")) + _value_size(value) for name, value in item.items())
    if size > MAX_ITEM_BYTES:
        raise ItemTooLarge(f"serialized item exceeds DynamoDB 400 KB limit: {size} bytes")
    return item


def _value_size(value: DynamoValue) -> int:
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, bool) or value is None:
        return 1
    if isinstance(value, int):
        digits = len(str(abs(value)))
        if digits > 38:
            raise PersistenceFormatError("DynamoDB number exceeds 38 digits of precision")
        return ((digits + 1) // 2) + 1
    if isinstance(value, list):
        return 3 + len(value) + sum(_value_size(entry) for entry in value)
    return (
        3
        + len(value)
        + sum(len(name.encode("utf-8")) + _value_size(entry) for name, entry in value.items())
    )


def _one(
    items: tuple[DynamoItem, ...], record_type: str, *, attempt_id: str | None = None
) -> DynamoItem:
    matches = _many(items, record_type, attempt_id=attempt_id)
    if len(matches) != 1:
        raise PersistenceFormatError(f"expected one {record_type} item, found {len(matches)}")
    return matches[0]


def _optional_one(
    items: tuple[DynamoItem, ...],
    record_type: str,
    *,
    attempt_id: str | None = None,
) -> DynamoItem | None:
    matches = _many(items, record_type, attempt_id=attempt_id)
    if len(matches) > 1:
        raise PersistenceFormatError(f"expected at most one {record_type} item")
    return matches[0] if matches else None


def _many(
    items: tuple[DynamoItem, ...],
    record_type: str,
    *,
    attempt_id: str | None = None,
) -> tuple[DynamoItem, ...]:
    return tuple(
        item
        for item in items
        if _text(item, "record_type") == record_type
        and (attempt_id is None or _optional_text(item, "attempt_id") == attempt_id)
    )


def _by_participant(values: Iterable[InitialOpinion | FinalProposal]) -> tuple[object, ...]:
    entries = tuple(values)
    by_slot = {value.participant: value for value in entries}
    if len(by_slot) != len(entries):
        raise PersistenceFormatError("duplicate participant artifact")
    return tuple(by_slot[slot] for slot in PARTICIPANTS if slot in by_slot)


def _by_voter(values: Iterable[Vote]) -> tuple[Vote, ...]:
    entries = tuple(values)
    by_slot = {value.voter: value for value in entries}
    if len(by_slot) != len(entries):
        raise PersistenceFormatError("duplicate vote artifact")
    return tuple(by_slot[slot] for slot in PARTICIPANTS if slot in by_slot)


def _identifier(value: AttemptId | None) -> str | None:
    return str(value) if value is not None else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise PersistenceFormatError("timestamp must be timezone-aware UTC")
    return value.isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return _timestamp(value) if value is not None else None


def _datetime(item: Mapping[str, DynamoValue], field: str) -> datetime:
    value = _text(item, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PersistenceFormatError(f"{field} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PersistenceFormatError(f"{field} must be UTC")
    return parsed


def _optional_datetime(item: Mapping[str, DynamoValue], field: str) -> datetime | None:
    if field not in item:
        return None
    return _datetime(item, field)


def _text(item: Mapping[str, DynamoValue], field: str, *, allow_empty: bool = False) -> str:
    value = item.get(field)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise PersistenceFormatError(f"{field} must be a non-empty string")
    return value


def _optional_text(item: Mapping[str, DynamoValue], field: str) -> str | None:
    if field not in item:
        return None
    return _text(item, field)


def _integer(item: Mapping[str, DynamoValue], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersistenceFormatError(f"{field} must be an integer")
    return value


def _boolean(item: Mapping[str, DynamoValue], field: str) -> bool:
    value = item.get(field)
    if not isinstance(value, bool):
        raise PersistenceFormatError(f"{field} must be a boolean")
    return value


def _string_tuple(item: Mapping[str, DynamoValue], field: str) -> tuple[str, ...]:
    value = item.get(field)
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise PersistenceFormatError(f"{field} must be a list of strings")
    return tuple(cast(list[str], value))


def _optional_attempt(item: Mapping[str, DynamoValue], field: str) -> AttemptId | None:
    value = _optional_text(item, field)
    return AttemptId.parse(value) if value is not None else None


def _optional_phase(item: Mapping[str, DynamoValue], field: str) -> DebatePhase | None:
    value = _optional_text(item, field)
    return DebatePhase(value) if value is not None else None


def _put_optional(item: DynamoItem, field: str, value: DynamoValue) -> None:
    if value is not None:
        item[field] = value
