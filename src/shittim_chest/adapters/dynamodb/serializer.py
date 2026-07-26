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
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressSemanticOperationBinding,
    IngressStatus,
    IngressStatusPublication,
    RuntimeState,
    RuntimeStatus,
    RuntimeWakeResult,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationState,
)
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

CURRENT_SCHEMA_VERSION = 6
PREVIOUS_SCHEMA_VERSION = 5
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
        # v5 debate_meta has no Discord name snapshots. Use the immutable
        # requester_id as a deterministic non-empty legacy fallback — not a
        # recovered Discord username or Guild display name.
        if migrated.get("record_type") == "debate_meta":
            requester_id = _text(migrated, "requester_id")
            migrated.setdefault("requester_username", requester_id)
            migrated.setdefault("requester_display_name", requester_id)
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
        "requester_username": snapshot.requester_username,
        "requester_display_name": snapshot.requester_display_name,
        "guild_id": snapshot.guild_id,
        "channel_id": snapshot.channel_id,
        "current_attempt_id": attempt_id,
        "current_phase": snapshot.state.phase.value,
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
        requester_username=_text(debate_meta, "requester_username"),
        requester_display_name=_text(debate_meta, "requester_display_name"),
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


def ingress_request_sort_key(request: IngressRequest) -> str:
    """Return the stable, UTC-sortable FIFO key for one ingress request."""

    timestamp = request.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"REQUEST#{timestamp}#{request.interaction_id}"


def serialize_ingress_request(request: IngressRequest) -> DynamoItem:
    """Serialize one active or historical ingress request into the shared table."""

    item: DynamoItem = {
        "PK": "CONTROL#INGRESS",
        "SK": ingress_request_sort_key(request),
        "record_type": "ingress_request",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": request.schema_version,
        "interaction_id": request.interaction_id,
        "operation_id": request.operation_id,
        "interaction_kind": request.kind.value,
        "application_id": request.application_id,
        "requester_id": request.requester_id,
        "requester_username": request.requester_username,
        "requester_display_name": request.requester_display_name,
        "requester_can_manage_messages": request.requester_can_manage_messages,
        "guild_id": request.guild_id,
        "channel_id": request.channel_id,
        "status_channel_id": request.status_channel_id,
        "status": request.status.value,
        "status_message_state": request.status_message_state.value,
        "created_at": _timestamp(request.created_at),
        "updated_at": _timestamp(request.updated_at),
        "startup_deadline_at": _timestamp(request.startup_deadline_at),
        "terminal_deadline_at": _timestamp(request.terminal_deadline_at),
        "delivery_attempt": request.delivery_attempt,
    }
    for field, value in (
        ("command_name", request.command_name),
        ("custom_id", request.custom_id),
        ("question", request.question),
        ("parent_channel_id", request.parent_channel_id),
        ("source_message_id", request.source_message_id),
        ("source_thread_id", request.source_thread_id),
        ("target_debate_id", _identifier(request.target_debate_id)),
        ("expected_attempt_id", _identifier(request.expected_attempt_id)),
        ("status_message_id", request.status_message_id),
        ("status_message_updated_at", _optional_timestamp(request.status_message_updated_at)),
        ("next_attempt_at", _optional_timestamp(request.next_attempt_at)),
        ("claim_owner", request.claim_owner),
        ("claim_expiry", _optional_timestamp(request.claim_expires_at)),
        ("error_code", request.error_code),
        ("error_detail_code", request.error_detail_code),
        ("accepted_debate_id", _identifier(request.accepted_debate_id)),
        ("accepted_attempt_id", _identifier(request.accepted_attempt_id)),
        ("completed_at", _optional_timestamp(request.completed_at)),
        ("ttl", request.ttl),
    ):
        _put_optional(item, field, value)
    if request.status.counts_toward_queue_limit:
        item["gsi2pk"] = "INGRESS#ACTIVE"
        item["gsi2sk"] = ingress_request_sort_key(request)
    return _validated_item(item)


def deserialize_ingress_request(raw_item: Mapping[str, DynamoValue]) -> IngressRequest:
    """Validate one ingress request, including its independent record schema."""

    item = _validate_auxiliary_item(raw_item, expected_type="ingress_request")
    try:
        request = IngressRequest(
            interaction_id=_text(item, "interaction_id"),
            operation_id=_text(item, "operation_id"),
            kind=IngressKind(_text(item, "interaction_kind")),
            application_id=_text(item, "application_id"),
            requester_id=_text(item, "requester_id"),
            requester_username=_text(item, "requester_username"),
            requester_display_name=_text(item, "requester_display_name"),
            requester_can_manage_messages=_boolean(item, "requester_can_manage_messages"),
            guild_id=_text(item, "guild_id"),
            channel_id=_text(item, "channel_id"),
            status_channel_id=_text(item, "status_channel_id"),
            status=IngressStatus(_text(item, "status")),
            status_message_state=StatusMessageState(_text(item, "status_message_state")),
            created_at=_datetime(item, "created_at"),
            updated_at=_datetime(item, "updated_at"),
            startup_deadline_at=_datetime(item, "startup_deadline_at"),
            terminal_deadline_at=_datetime(item, "terminal_deadline_at"),
            command_name=_optional_text(item, "command_name"),
            custom_id=_optional_text(item, "custom_id"),
            question=_optional_text(item, "question"),
            parent_channel_id=_optional_text(item, "parent_channel_id"),
            source_message_id=_optional_text(item, "source_message_id"),
            source_thread_id=_optional_text(item, "source_thread_id"),
            target_debate_id=_optional_debate(item, "target_debate_id"),
            expected_attempt_id=_optional_attempt(item, "expected_attempt_id"),
            status_message_id=_optional_text(item, "status_message_id"),
            status_message_updated_at=_optional_datetime(item, "status_message_updated_at"),
            next_attempt_at=_optional_datetime(item, "next_attempt_at"),
            claim_owner=_optional_text(item, "claim_owner"),
            claim_expires_at=_optional_datetime(item, "claim_expiry"),
            delivery_attempt=_integer(item, "delivery_attempt"),
            error_code=_optional_text(item, "error_code"),
            error_detail_code=_optional_text(item, "error_detail_code"),
            accepted_debate_id=_optional_debate(item, "accepted_debate_id"),
            accepted_attempt_id=_optional_attempt(item, "accepted_attempt_id"),
            completed_at=_optional_datetime(item, "completed_at"),
            ttl=_optional_integer(item, "ttl"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid ingress request") from error
    if _text(item, "PK") != "CONTROL#INGRESS":
        raise PersistenceFormatError("ingress request has an invalid partition key")
    if _text(item, "SK") != ingress_request_sort_key(request):
        raise PersistenceFormatError("ingress request has an invalid sort key")
    is_indexed = "gsi2pk" in item or "gsi2sk" in item
    if request.status.counts_toward_queue_limit:
        if _text(item, "gsi2pk") != "INGRESS#ACTIVE":
            raise PersistenceFormatError("active ingress request has an invalid index key")
        if _text(item, "gsi2sk") != ingress_request_sort_key(request):
            raise PersistenceFormatError("active ingress request has an invalid index sort key")
    elif is_indexed:
        raise PersistenceFormatError("inactive ingress request must not be indexed as active")
    return request


def serialize_ingress_operation_result(operation: IngressOperationResult) -> DynamoItem:
    """Serialize the strong replay record associated with one ingress operation."""

    item: DynamoItem = {
        "PK": f"INGRESS_OPERATION#{operation.interaction_id}",
        "SK": "RESULT",
        "record_type": "ingress_operation_result",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": operation.schema_version,
        "operation_id": operation.operation_id,
        "interaction_id": operation.interaction_id,
        "request_sort_key": operation.request_sort_key,
        "status": operation.status.value,
        "created_at": _timestamp(operation.created_at),
        "updated_at": _timestamp(operation.updated_at),
    }
    for field, value in (
        ("accepted_debate_id", _identifier(operation.accepted_debate_id)),
        ("accepted_attempt_id", _identifier(operation.accepted_attempt_id)),
        ("error_code", operation.error_code),
    ):
        _put_optional(item, field, value)
    return _validated_item(item)


def deserialize_ingress_operation_result(
    raw_item: Mapping[str, DynamoValue],
) -> IngressOperationResult:
    """Validate and rebuild a strongly consistent ingress replay result."""

    item = _validate_auxiliary_item(raw_item, expected_type="ingress_operation_result")
    try:
        operation = IngressOperationResult(
            operation_id=_text(item, "operation_id"),
            interaction_id=_text(item, "interaction_id"),
            request_sort_key=_text(item, "request_sort_key"),
            status=IngressStatus(_text(item, "status")),
            created_at=_datetime(item, "created_at"),
            updated_at=_datetime(item, "updated_at"),
            accepted_debate_id=_optional_debate(item, "accepted_debate_id"),
            accepted_attempt_id=_optional_attempt(item, "accepted_attempt_id"),
            error_code=_optional_text(item, "error_code"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid ingress operation result") from error
    if _text(item, "PK") != f"INGRESS_OPERATION#{operation.interaction_id}":
        raise PersistenceFormatError("ingress operation has an invalid partition key")
    if _text(item, "SK") != "RESULT":
        raise PersistenceFormatError("ingress operation has an invalid sort key")
    return operation


def serialize_ingress_semantic_binding(
    binding: IngressSemanticOperationBinding,
) -> DynamoItem:
    """Serialize the first Interaction ID bound to a component operation."""

    return _validated_item(
        {
            "PK": f"INGRESS_SEMANTIC_OPERATION#{binding.operation_id}",
            "SK": "BINDING",
            "record_type": "ingress_semantic_operation_binding",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": binding.schema_version,
            "operation_id": binding.operation_id,
            "canonical_interaction_id": binding.canonical_interaction_id,
            "request_sort_key": binding.request_sort_key,
            "created_at": _timestamp(binding.created_at),
        }
    )


def deserialize_ingress_semantic_binding(
    raw_item: Mapping[str, DynamoValue],
) -> IngressSemanticOperationBinding:
    """Validate one semantic component-operation binding."""

    item = _validate_auxiliary_item(
        raw_item,
        expected_type="ingress_semantic_operation_binding",
    )
    try:
        binding = IngressSemanticOperationBinding(
            operation_id=_text(item, "operation_id"),
            canonical_interaction_id=_text(item, "canonical_interaction_id"),
            request_sort_key=_text(item, "request_sort_key"),
            created_at=_datetime(item, "created_at"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid ingress semantic binding") from error
    if _text(item, "PK") != f"INGRESS_SEMANTIC_OPERATION#{binding.operation_id}":
        raise PersistenceFormatError("ingress semantic binding has an invalid partition key")
    if _text(item, "SK") != "BINDING":
        raise PersistenceFormatError("ingress semantic binding has an invalid sort key")
    return binding


def serialize_ingress_status_publication(
    publication: IngressStatusPublication,
) -> DynamoItem:
    """Serialize a durable desired/delivered public-status operation."""

    item: DynamoItem = {
        "PK": f"INGRESS_OPERATION#{publication.canonical_interaction_id}",
        "SK": "STATUS_PUBLICATION",
        "record_type": "ingress_status_publication",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": publication.schema_version,
        "canonical_interaction_id": publication.canonical_interaction_id,
        "request_sort_key": publication.request_sort_key,
        "status_channel_id": publication.status_channel_id,
        "desired_state": publication.desired_state.value,
        "publication_state": publication.state.value,
        "nonce": publication.nonce,
        "content": publication.content,
        "content_hash": publication.content_hash,
        "history_reconciliation_required": publication.history_reconciliation_required,
        "created_at": _timestamp(publication.created_at),
        "updated_at": _timestamp(publication.updated_at),
        "delivery_attempt": publication.delivery_attempt,
        "incarnation": publication.incarnation,
    }
    for field, value in (
        (
            "delivered_state",
            publication.delivered_state.value if publication.delivered_state is not None else None,
        ),
        ("status_message_id", publication.status_message_id),
        (
            "status_message_updated_at",
            _optional_timestamp(publication.status_message_updated_at),
        ),
        (
            "history_cursor_message_id",
            (
                publication.history_checkpoint.history_cursor_message_id
                if publication.history_checkpoint is not None
                else None
            ),
        ),
        (
            "history_verified_head_message_id",
            (
                publication.history_checkpoint.history_verified_head_message_id
                if publication.history_checkpoint is not None
                else None
            ),
        ),
        (
            "history_gap_cursor_message_id",
            (
                publication.history_checkpoint.history_gap_cursor_message_id
                if publication.history_checkpoint is not None
                else None
            ),
        ),
        (
            "history_gap_upper_message_id",
            (
                publication.history_checkpoint.history_gap_upper_message_id
                if publication.history_checkpoint is not None
                else None
            ),
        ),
        ("next_attempt_at", _optional_timestamp(publication.next_attempt_at)),
        ("claim_owner", publication.claim_owner),
        ("claim_expiry", _optional_timestamp(publication.claim_expires_at)),
        ("error_code", publication.error_code),
    ):
        _put_optional(item, field, value)
    due_at = _status_publication_due_at(publication)
    if due_at is not None:
        item["gsi1pk"] = "INGRESS#STATUS_DUE"
        item["gsi1sk"] = f"{_timestamp(due_at)}#{publication.canonical_interaction_id}"
    return _validated_item(item)


def deserialize_ingress_status_publication(
    raw_item: Mapping[str, DynamoValue],
) -> IngressStatusPublication:
    """Validate one durable public-status operation and its sparse due index."""

    item = _validate_auxiliary_item(
        raw_item,
        expected_type="ingress_status_publication",
        expected_record_schema_version=3,
    )
    delivered = _optional_text(item, "delivered_state")
    history_checkpoint = _deserialize_status_history_checkpoint(item)
    try:
        publication = IngressStatusPublication(
            canonical_interaction_id=_text(item, "canonical_interaction_id"),
            request_sort_key=_text(item, "request_sort_key"),
            status_channel_id=_text(item, "status_channel_id"),
            desired_state=StatusMessageState(_text(item, "desired_state")),
            delivered_state=(StatusMessageState(delivered) if delivered is not None else None),
            state=StatusPublicationState(_text(item, "publication_state")),
            nonce=_text(item, "nonce"),
            content=_text(item, "content"),
            content_hash=_text(item, "content_hash"),
            created_at=_datetime(item, "created_at"),
            updated_at=_datetime(item, "updated_at"),
            status_message_id=_optional_text(item, "status_message_id"),
            status_message_updated_at=_optional_datetime(item, "status_message_updated_at"),
            history_checkpoint=history_checkpoint,
            history_reconciliation_required=_boolean(
                item,
                "history_reconciliation_required",
            ),
            next_attempt_at=_optional_datetime(item, "next_attempt_at"),
            claim_owner=_optional_text(item, "claim_owner"),
            claim_expires_at=_optional_datetime(item, "claim_expiry"),
            delivery_attempt=_integer(item, "delivery_attempt"),
            incarnation=_integer(item, "incarnation"),
            error_code=_optional_text(item, "error_code"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid ingress status publication") from error
    if _text(item, "PK") != f"INGRESS_OPERATION#{publication.canonical_interaction_id}":
        raise PersistenceFormatError("ingress status publication has an invalid partition key")
    if _text(item, "SK") != "STATUS_PUBLICATION":
        raise PersistenceFormatError("ingress status publication has an invalid sort key")
    due_at = _status_publication_due_at(publication)
    is_indexed = "gsi1pk" in item or "gsi1sk" in item
    if due_at is None:
        if is_indexed:
            raise PersistenceFormatError("settled status publication must not be due-indexed")
    else:
        if _text(item, "gsi1pk") != "INGRESS#STATUS_DUE":
            raise PersistenceFormatError("status publication has an invalid due index key")
        expected_sort_key = f"{_timestamp(due_at)}#{publication.canonical_interaction_id}"
        if _text(item, "gsi1sk") != expected_sort_key:
            raise PersistenceFormatError("status publication has an invalid due index sort key")
    return publication


def _deserialize_status_history_checkpoint(
    item: Mapping[str, DynamoValue],
) -> StatusHistoryCheckpoint | None:
    fields = (
        "history_cursor_message_id",
        "history_verified_head_message_id",
        "history_gap_cursor_message_id",
        "history_gap_upper_message_id",
    )
    values = {field: _optional_text(item, field) for field in fields}
    if all(value is None for value in values.values()):
        return None
    verified_head = values["history_verified_head_message_id"]
    if verified_head is None:
        raise PersistenceFormatError("status history checkpoint has no verified head")
    try:
        return StatusHistoryCheckpoint(
            history_cursor_message_id=values["history_cursor_message_id"],
            history_verified_head_message_id=verified_head,
            history_gap_cursor_message_id=values["history_gap_cursor_message_id"],
            history_gap_upper_message_id=values["history_gap_upper_message_id"],
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid status history checkpoint") from error


def _status_publication_due_at(publication: IngressStatusPublication) -> datetime | None:
    if not publication.state.counts_as_pending:
        return None
    if publication.state is StatusPublicationState.CLAIMED:
        return publication.claim_expires_at
    return publication.next_attempt_at


def serialize_runtime_state(state: RuntimeState) -> DynamoItem:
    """Serialize the singleton generation-fenced runtime control record."""

    item: DynamoItem = {
        "PK": "CONTROL#RUNTIME",
        "SK": "STATE",
        "record_type": "runtime_state",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": state.schema_version,
        "state": state.status.value,
        "generation": state.generation,
        "desired_count": state.desired_count,
        "version": state.version,
        "updated_at": _timestamp(state.updated_at),
    }
    for field, value in (
        ("runtime_instance_id", state.runtime_instance_id),
        ("wake_started_at", _optional_timestamp(state.wake_started_at)),
        ("last_request_at", _optional_timestamp(state.last_request_at)),
        ("started_at", _optional_timestamp(state.started_at)),
        ("ready_at", _optional_timestamp(state.ready_at)),
        ("busy_since", _optional_timestamp(state.busy_since)),
        ("idle_since", _optional_timestamp(state.idle_since)),
        ("stop_eligible_at", _optional_timestamp(state.stop_eligible_at)),
        ("stopping_at", _optional_timestamp(state.stopping_at)),
        ("stopped_at", _optional_timestamp(state.stopped_at)),
        ("last_error_code", state.last_error_code),
        ("last_reconciled_at", _optional_timestamp(state.last_reconciled_at)),
    ):
        _put_optional(item, field, value)
    return _validated_item(item)


def deserialize_runtime_state(raw_item: Mapping[str, DynamoValue]) -> RuntimeState:
    """Validate and rebuild the singleton runtime control record."""

    item = _validate_auxiliary_item(raw_item, expected_type="runtime_state")
    try:
        state = RuntimeState(
            status=RuntimeStatus(_text(item, "state")),
            generation=_integer(item, "generation"),
            desired_count=_integer(item, "desired_count"),
            version=_integer(item, "version"),
            updated_at=_datetime(item, "updated_at"),
            runtime_instance_id=_optional_text(item, "runtime_instance_id"),
            wake_started_at=_optional_datetime(item, "wake_started_at"),
            last_request_at=_optional_datetime(item, "last_request_at"),
            started_at=_optional_datetime(item, "started_at"),
            ready_at=_optional_datetime(item, "ready_at"),
            busy_since=_optional_datetime(item, "busy_since"),
            idle_since=_optional_datetime(item, "idle_since"),
            stop_eligible_at=_optional_datetime(item, "stop_eligible_at"),
            stopping_at=_optional_datetime(item, "stopping_at"),
            stopped_at=_optional_datetime(item, "stopped_at"),
            last_error_code=_optional_text(item, "last_error_code"),
            last_reconciled_at=_optional_datetime(item, "last_reconciled_at"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid runtime state") from error
    if _text(item, "PK") != "CONTROL#RUNTIME" or _text(item, "SK") != "STATE":
        raise PersistenceFormatError("runtime state has an invalid key")
    return state


def serialize_runtime_wake_result(result: RuntimeWakeResult) -> DynamoItem:
    """Serialize one immutable interaction-to-generation binding."""

    return _validated_item(
        {
            "PK": f"INGRESS_OPERATION#{result.interaction_id}",
            "SK": "RUNTIME_WAKE",
            "record_type": "runtime_wake_result",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": result.schema_version,
            "interaction_id": result.interaction_id,
            "generation": result.generation,
            "runtime_version": result.runtime_version,
            "recorded_at": _timestamp(result.recorded_at),
        }
    )


def deserialize_runtime_wake_result(
    raw_item: Mapping[str, DynamoValue],
) -> RuntimeWakeResult:
    """Validate one immutable interaction-to-generation binding."""

    item = _validate_auxiliary_item(raw_item, expected_type="runtime_wake_result")
    try:
        result = RuntimeWakeResult(
            interaction_id=_text(item, "interaction_id"),
            generation=_integer(item, "generation"),
            runtime_version=_integer(item, "runtime_version"),
            recorded_at=_datetime(item, "recorded_at"),
            schema_version=_integer(item, "record_schema_version"),
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid runtime wake result") from error
    if _text(item, "PK") != f"INGRESS_OPERATION#{result.interaction_id}":
        raise PersistenceFormatError("runtime wake result has an invalid partition key")
    if _text(item, "SK") != "RUNTIME_WAKE":
        raise PersistenceFormatError("runtime wake result has an invalid sort key")
    return result


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


def _validate_auxiliary_item(
    raw_item: Mapping[str, DynamoValue],
    *,
    expected_type: str,
    expected_record_schema_version: int = 1,
) -> DynamoItem:
    item = dict(raw_item)
    if _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION:
        raise PersistenceFormatError("unsupported shared table schema version")
    if _integer(item, "record_schema_version") != expected_record_schema_version:
        raise PersistenceFormatError("unsupported auxiliary record schema version")
    if _text(item, "record_type") != expected_type:
        raise PersistenceFormatError(f"record is not an {expected_type}")
    _text(item, "PK")
    _text(item, "SK")
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


def _identifier(value: AttemptId | DebateId | None) -> str | None:
    return str(value) if value is not None else None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise PersistenceFormatError("timestamp must be timezone-aware UTC")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


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


def _optional_integer(item: Mapping[str, DynamoValue], field: str) -> int | None:
    if field not in item:
        return None
    return _integer(item, field)


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


def _optional_debate(item: Mapping[str, DynamoValue], field: str) -> DebateId | None:
    value = _optional_text(item, field)
    return DebateId.parse(value) if value is not None else None


def _optional_phase(item: Mapping[str, DynamoValue], field: str) -> DebatePhase | None:
    value = _optional_text(item, field)
    return DebatePhase(value) if value is not None else None


def _put_optional(item: DynamoItem, field: str, value: DynamoValue) -> None:
    if value is not None:
        item[field] = value
