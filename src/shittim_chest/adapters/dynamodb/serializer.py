"""Convert immutable application records to and from DynamoDB native values."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from shittim_chest.application.deployment_guard import (
    DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION,
    BreakGlassReason,
    DeploymentLock,
    DeploymentLockState,
    DeploymentMode,
)
from shittim_chest.application.discord import (
    DiscordBotSlot,
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
)
from shittim_chest.application.models import (
    DebateSnapshot,
    DeliveryAbandonReason,
    GenerationCheckpoint,
    GenerationStatus,
    LeaseGrant,
    PhaseDeliveryPlan,
    PhaseDeliveryStatus,
    TerminalDeliveryPlan,
)
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

CURRENT_SCHEMA_VERSION = 7
PREVIOUS_SCHEMA_VERSION = 6
MAX_ITEM_BYTES = 400 * 1024
INGRESS_ACTIVE_POINTER_RECORD_SCHEMA_VERSION = 1


class PersistenceFormatError(ValueError):
    """Raised when a persistence record cannot be validated or migrated."""


class ItemTooLarge(PersistenceFormatError):
    """Raised before an item can cross DynamoDB's 400 KB limit."""


@dataclass(frozen=True, slots=True)
class IngressActivePointer:
    """PII-free immutable pointer to one request that still occupies the FIFO."""

    interaction_id: str
    request_sort_key: str
    created_at: datetime
    schema_version: int = INGRESS_ACTIVE_POINTER_RECORD_SCHEMA_VERSION


def migrate_item(item: Mapping[str, DynamoValue]) -> DynamoItem:
    """Up-convert the previous record schema or validate the current one."""

    migrated = dict(item)
    version = _integer(migrated, "schema_version")
    if version == PREVIOUS_SCHEMA_VERSION:
        # v6 predates staged terminal delivery. Missing optional delivery
        # fields remain absent; deployment validation rejects unsafe legacy
        # activity before the scale-to-zero control records are initialized.
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
        "origin_ingress_interaction_id",
        snapshot.origin_ingress_interaction_id,
    )
    _put_optional(
        attempt_meta,
        "failed_from_phase",
        snapshot.state.failed_from_phase.value if snapshot.state.failed_from_phase else None,
    )
    _put_optional(attempt_meta, "error_code", snapshot.error_code)
    if snapshot.generation_checkpoints:
        attempt_meta["generation_checkpoints"] = [
            _serialize_generation_checkpoint(checkpoint)
            for checkpoint in snapshot.generation_checkpoints
        ]
    terminal_delivery = snapshot.terminal_delivery
    if terminal_delivery is not None:
        legacy_target = terminal_delivery.target_phase
        legacy_completed_at = terminal_delivery.completed_at
        if (
            isinstance(terminal_delivery, PhaseDeliveryPlan)
            and terminal_delivery.status is PhaseDeliveryStatus.ABANDONED
            and snapshot.state.phase.is_terminal
        ):
            legacy_target = snapshot.state.phase
            legacy_completed_at = terminal_delivery.settled_at
        attempt_meta.update(
            {
                "terminal_delivery_target": legacy_target.value,
                "terminal_delivery_operation_ids": list(terminal_delivery.operation_ids),
                "terminal_delivery_content_hashes": list(terminal_delivery.content_hashes),
                "terminal_delivery_staged_at": _timestamp(terminal_delivery.staged_at),
            }
        )
        _put_optional(
            attempt_meta,
            "terminal_delivery_completed_at",
            _optional_timestamp(legacy_completed_at),
        )
        if isinstance(terminal_delivery, PhaseDeliveryPlan):
            attempt_meta.update(
                {
                    "terminal_delivery_plan_id": terminal_delivery.plan_id,
                    "terminal_delivery_source": terminal_delivery.source_phase.value,
                    "terminal_delivery_sequences": list(terminal_delivery.delivery_sequences),
                    "terminal_delivery_deadline_at": _timestamp(terminal_delivery.deadline_at),
                    "terminal_delivery_plan_status": terminal_delivery.status.value,
                }
            )
            _put_optional(
                attempt_meta,
                "terminal_delivery_abandon_reason",
                (
                    terminal_delivery.abandon_reason.value
                    if terminal_delivery.abandon_reason is not None
                    else None
                ),
            )
    _put_optional(
        attempt_meta,
        "panel_refresh_required_at",
        _optional_timestamp(snapshot.panel_refresh_required_at),
    )
    _put_optional(
        attempt_meta,
        "panel_refreshed_at",
        _optional_timestamp(snapshot.panel_refreshed_at),
    )
    _put_optional(
        attempt_meta,
        "panel_refresh_claim_owner",
        snapshot.panel_refresh_claim_owner,
    )
    _put_optional(
        attempt_meta,
        "panel_refresh_claim_expiry",
        _optional_timestamp(snapshot.panel_refresh_claim_expires_at),
    )
    _put_optional(
        attempt_meta,
        "panel_refresh_next_attempt_at",
        _optional_timestamp(snapshot.panel_refresh_next_attempt_at),
    )
    _put_optional(
        attempt_meta,
        "panel_refresh_failed_at",
        _optional_timestamp(snapshot.panel_refresh_failed_at),
    )
    _put_optional(
        attempt_meta,
        "panel_refresh_error_code",
        snapshot.panel_refresh_error_code,
    )
    if snapshot.panel_refresh_delivery_attempt:
        attempt_meta["panel_refresh_delivery_attempt"] = snapshot.panel_refresh_delivery_attempt
    if snapshot.lease is not None:
        attempt_meta.update(
            {
                "lease_owner": snapshot.lease.owner_id,
                "lease_slot": snapshot.lease.slot,
                "lease_expiry": _timestamp(snapshot.lease.expires_at),
                "fencing_token": snapshot.lease.fencing_token,
            }
        )
    if snapshot.panel_refresh_pending:
        due_at = (
            snapshot.panel_refresh_claim_expires_at
            or snapshot.panel_refresh_next_attempt_at
            or snapshot.panel_refresh_required_at
        )
        if due_at is None:  # pragma: no cover - model invariant narrows this
            raise AssertionError("pending panel refresh has no due timestamp")
        attempt_meta["gsi2pk"] = "PANEL_REFRESH"
        attempt_meta["gsi2sk"] = f"{_timestamp(due_at)}#{debate_id}#{attempt_id}"
    elif not snapshot.state.phase.is_terminal and all(
        value is not None
        for value in (
            snapshot.starter_message_id,
            snapshot.thread_id,
            snapshot.control_panel_message_id,
        )
    ):
        attempt_meta["gsi2pk"] = "RECOVERABLE"
        attempt_meta["gsi2sk"] = f"{_timestamp(snapshot.state.updated_at)}#{debate_id}#{attempt_id}"

    items = [debate_meta, attempt_meta]
    if isinstance(terminal_delivery, PhaseDeliveryPlan):
        items.append(_serialize_phase_delivery_plan(common, attempt_id, terminal_delivery))
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
            _optional_text(decision_item, "victory_message"),
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
    generation_checkpoints = _deserialize_generation_checkpoints(attempt_meta)

    terminal_fields = frozenset(
        {
            "terminal_delivery_target",
            "terminal_delivery_operation_ids",
            "terminal_delivery_content_hashes",
            "terminal_delivery_staged_at",
        }
    )
    present_terminal_fields = terminal_fields.intersection(attempt_meta)
    completed_field_present = "terminal_delivery_completed_at" in attempt_meta
    pointer_fields = frozenset(
        {
            "terminal_delivery_plan_id",
            "terminal_delivery_source",
            "terminal_delivery_sequences",
            "terminal_delivery_deadline_at",
            "terminal_delivery_plan_status",
        }
    )
    present_pointer_fields = pointer_fields.intersection(attempt_meta)
    if present_terminal_fields and present_terminal_fields != terminal_fields:
        raise PersistenceFormatError("terminal delivery fields are incomplete")
    if completed_field_present and present_terminal_fields != terminal_fields:
        raise PersistenceFormatError("terminal delivery completion has no staged plan")
    if present_pointer_fields and present_pointer_fields != pointer_fields:
        raise PersistenceFormatError("phase delivery plan pointer is incomplete")
    if present_pointer_fields and present_terminal_fields != terminal_fields:
        raise PersistenceFormatError("phase delivery plan pointer has no staged plan")
    if (
        "terminal_delivery_abandon_reason" in attempt_meta
        and present_pointer_fields != pointer_fields
    ):
        raise PersistenceFormatError("phase delivery abandonment has no plan pointer")

    terminal_delivery: TerminalDeliveryPlan | PhaseDeliveryPlan | None = None
    if present_terminal_fields:
        if not present_pointer_fields:
            terminal_delivery = TerminalDeliveryPlan(
                target_phase=DebatePhase(_text(attempt_meta, "terminal_delivery_target")),
                operation_ids=_string_tuple(attempt_meta, "terminal_delivery_operation_ids"),
                content_hashes=_string_tuple(attempt_meta, "terminal_delivery_content_hashes"),
                staged_at=_datetime(attempt_meta, "terminal_delivery_staged_at"),
                completed_at=_optional_datetime(
                    attempt_meta,
                    "terminal_delivery_completed_at",
                ),
            )
        else:
            plan_id = _text(attempt_meta, "terminal_delivery_plan_id")
            matching = tuple(
                item
                for item in _many(items, "phase_delivery_plan", attempt_id=attempt_id)
                if _text(item, "plan_id") == plan_id
            )
            if len(matching) != 1:
                raise PersistenceFormatError("phase delivery plan pointer is unresolved")
            terminal_delivery = _deserialize_phase_delivery_plan(matching[0])
            if (
                terminal_delivery.operation_ids
                != _string_tuple(attempt_meta, "terminal_delivery_operation_ids")
                or terminal_delivery.content_hashes
                != _string_tuple(attempt_meta, "terminal_delivery_content_hashes")
                or terminal_delivery.staged_at
                != _datetime(attempt_meta, "terminal_delivery_staged_at")
                or terminal_delivery.source_phase
                is not DebatePhase(_text(attempt_meta, "terminal_delivery_source"))
                or terminal_delivery.delivery_sequences
                != _integer_tuple(attempt_meta, "terminal_delivery_sequences")
                or terminal_delivery.deadline_at
                != _datetime(attempt_meta, "terminal_delivery_deadline_at")
                or terminal_delivery.status
                is not PhaseDeliveryStatus(_text(attempt_meta, "terminal_delivery_plan_status"))
                or terminal_delivery.abandon_reason
                != (
                    None
                    if "terminal_delivery_abandon_reason" not in attempt_meta
                    else DeliveryAbandonReason(
                        _text(attempt_meta, "terminal_delivery_abandon_reason")
                    )
                )
            ):
                raise PersistenceFormatError("phase delivery plan pointer conflicts with attempt")
            projected_target = (
                state.phase
                if terminal_delivery.status is PhaseDeliveryStatus.ABANDONED
                and state.phase.is_terminal
                else terminal_delivery.target_phase
            )
            projected_completed_at = (
                terminal_delivery.settled_at
                if terminal_delivery.status is PhaseDeliveryStatus.ABANDONED
                and state.phase.is_terminal
                else terminal_delivery.completed_at
            )
            if projected_target is not DebatePhase(
                _text(attempt_meta, "terminal_delivery_target")
            ) or projected_completed_at != _optional_datetime(
                attempt_meta, "terminal_delivery_completed_at"
            ):
                raise PersistenceFormatError(
                    "phase delivery plan projection conflicts with attempt"
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
        origin_ingress_interaction_id=_optional_text(
            attempt_meta,
            "origin_ingress_interaction_id",
        ),
        starter_message_id=_optional_text(debate_meta, "starter_message_id"),
        thread_id=_optional_text(debate_meta, "thread_id"),
        control_panel_message_id=_optional_text(debate_meta, "control_panel_message_id"),
        lease=lease,
        panel_refresh_required_at=_optional_datetime(
            attempt_meta,
            "panel_refresh_required_at",
        ),
        panel_refreshed_at=_optional_datetime(attempt_meta, "panel_refreshed_at"),
        panel_refresh_claim_owner=_optional_text(
            attempt_meta,
            "panel_refresh_claim_owner",
        ),
        panel_refresh_claim_expires_at=_optional_datetime(
            attempt_meta,
            "panel_refresh_claim_expiry",
        ),
        panel_refresh_next_attempt_at=_optional_datetime(
            attempt_meta,
            "panel_refresh_next_attempt_at",
        ),
        panel_refresh_failed_at=_optional_datetime(
            attempt_meta,
            "panel_refresh_failed_at",
        ),
        panel_refresh_error_code=_optional_text(
            attempt_meta,
            "panel_refresh_error_code",
        ),
        panel_refresh_delivery_attempt=(
            _integer(attempt_meta, "panel_refresh_delivery_attempt")
            if "panel_refresh_delivery_attempt" in attempt_meta
            else 0
        ),
        evidence=evidence,
        initial_opinions=cast(tuple[InitialOpinion, ...], opinions),
        final_proposals=cast(tuple[FinalProposal, ...], proposals),
        votes=votes,
        final_decision=decision,
        escalation_assessment=escalation_assessment,
        generation_checkpoints=generation_checkpoints,
        error_code=_optional_text(attempt_meta, "error_code"),
        terminal_delivery=terminal_delivery,
    )


_GENERATION_REQUIRED_FIELDS = frozenset(
    {
        "record_schema_version",
        "phase",
        "participant",
        "status",
        "logical_attempt",
        "planned_at",
    }
)
_GENERATION_OPTIONAL_FIELDS = frozenset(
    {
        "claim_owner",
        "claim_slot",
        "claim_fencing_token",
        "claimed_at",
        "settled_at",
        "error_code",
    }
)


def _serialize_generation_checkpoint(
    checkpoint: GenerationCheckpoint,
) -> dict[str, DynamoValue]:
    item: dict[str, DynamoValue] = {
        "record_schema_version": checkpoint.record_schema_version,
        "phase": checkpoint.phase.value,
        "participant": checkpoint.participant.value,
        "status": checkpoint.status.value,
        "logical_attempt": checkpoint.logical_attempt,
        "planned_at": _timestamp(checkpoint.planned_at),
    }
    _put_optional(item, "claim_owner", checkpoint.claim_owner)
    _put_optional(item, "claim_slot", checkpoint.claim_slot)
    _put_optional(item, "claim_fencing_token", checkpoint.claim_fencing_token)
    _put_optional(item, "claimed_at", _optional_timestamp(checkpoint.claimed_at))
    _put_optional(item, "settled_at", _optional_timestamp(checkpoint.settled_at))
    _put_optional(item, "error_code", checkpoint.error_code)
    return item


def _deserialize_generation_checkpoints(
    attempt_meta: Mapping[str, DynamoValue],
) -> tuple[GenerationCheckpoint, ...]:
    unexpected_generation_fields = {
        field
        for field in attempt_meta
        if field.startswith("generation_") and field != "generation_checkpoints"
    }
    if unexpected_generation_fields:
        raise PersistenceFormatError("generation checkpoint schema is unsupported")
    value = attempt_meta.get("generation_checkpoints")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PersistenceFormatError("generation checkpoints must be a list")
    checkpoints: list[GenerationCheckpoint] = []
    for raw in value:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise PersistenceFormatError("generation checkpoint must be a string-keyed map")
        item = raw
        fields = frozenset(item)
        if not _GENERATION_REQUIRED_FIELDS.issubset(fields):
            raise PersistenceFormatError("generation checkpoint fields are incomplete")
        if not fields.issubset(_GENERATION_REQUIRED_FIELDS | _GENERATION_OPTIONAL_FIELDS):
            raise PersistenceFormatError("generation checkpoint contains unknown fields")
        try:
            checkpoints.append(
                GenerationCheckpoint(
                    phase=DebatePhase(_text(item, "phase")),
                    participant=ParticipantSlot(_text(item, "participant")),
                    status=GenerationStatus(_text(item, "status")),
                    logical_attempt=_integer(item, "logical_attempt"),
                    planned_at=_datetime(item, "planned_at"),
                    claim_owner=_optional_text(item, "claim_owner"),
                    claim_slot=_optional_integer(item, "claim_slot"),
                    claim_fencing_token=_optional_integer(
                        item,
                        "claim_fencing_token",
                    ),
                    claimed_at=_optional_datetime(item, "claimed_at"),
                    settled_at=_optional_datetime(item, "settled_at"),
                    error_code=_optional_text(item, "error_code"),
                    record_schema_version=_integer(item, "record_schema_version"),
                )
            )
        except (TypeError, ValueError) as error:
            raise PersistenceFormatError("invalid generation checkpoint") from error
    keys = tuple((checkpoint.phase, checkpoint.participant) for checkpoint in checkpoints)
    if len(keys) != len(set(keys)):
        raise PersistenceFormatError("generation checkpoint identity is duplicated")
    return tuple(checkpoints)


def _serialize_phase_delivery_plan(
    common: DynamoItem,
    attempt_id: str,
    plan: PhaseDeliveryPlan,
) -> DynamoItem:
    item: DynamoItem = {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#DELIVERY#{plan.plan_id}",
        "record_type": "phase_delivery_plan",
        "record_schema_version": plan.record_schema_version,
        "attempt_id": attempt_id,
        "plan_id": plan.plan_id,
        "source_phase": plan.source_phase.value,
        "target_phase": plan.target_phase.value,
        "status": plan.status.value,
        "operation_ids": list(plan.operation_ids),
        "content_hashes": list(plan.content_hashes),
        "delivery_sequences": list(plan.delivery_sequences),
        "staged_at": _timestamp(plan.staged_at),
        "deadline_at": _timestamp(plan.deadline_at),
    }
    _put_optional(item, "settled_at", _optional_timestamp(plan.settled_at))
    _put_optional(
        item,
        "abandon_reason",
        plan.abandon_reason.value if plan.abandon_reason is not None else None,
    )
    return item


def _deserialize_phase_delivery_plan(item: Mapping[str, DynamoValue]) -> PhaseDeliveryPlan:
    required_fields = frozenset(
        {
            "PK",
            "SK",
            "schema_version",
            "record_type",
            "record_schema_version",
            "debate_id",
            "attempt_id",
            "plan_id",
            "source_phase",
            "target_phase",
            "status",
            "operation_ids",
            "content_hashes",
            "delivery_sequences",
            "staged_at",
            "deadline_at",
            "created_at",
            "updated_at",
        }
    )
    optional_fields = frozenset({"settled_at", "abandon_reason"})
    fields = frozenset(item)
    if not required_fields.issubset(fields):
        raise PersistenceFormatError("phase delivery plan fields are incomplete")
    if not fields.issubset(required_fields | optional_fields):
        raise PersistenceFormatError("phase delivery plan contains unknown fields")
    debate_id = _text(item, "debate_id")
    attempt_id = _text(item, "attempt_id")
    plan_id = _text(item, "plan_id")
    if (
        _text(item, "PK") != f"DEBATE#{debate_id}"
        or _text(item, "SK") != f"ATTEMPT#{attempt_id}#DELIVERY#{plan_id}"
    ):
        raise PersistenceFormatError("phase delivery plan key conflicts with its identity")
    if _text(item, "record_type") != "phase_delivery_plan":
        raise PersistenceFormatError("record is not a phase delivery plan")
    try:
        return PhaseDeliveryPlan(
            plan_id=plan_id,
            source_phase=DebatePhase(_text(item, "source_phase")),
            target_phase=DebatePhase(_text(item, "target_phase")),
            operation_ids=_string_tuple(item, "operation_ids"),
            content_hashes=_string_tuple(item, "content_hashes"),
            delivery_sequences=_integer_tuple(item, "delivery_sequences"),
            staged_at=_datetime(item, "staged_at"),
            deadline_at=_datetime(item, "deadline_at"),
            status=PhaseDeliveryStatus(_text(item, "status")),
            settled_at=_optional_datetime(item, "settled_at"),
            abandon_reason=(
                None
                if "abandon_reason" not in item
                else DeliveryAbandonReason(_text(item, "abandon_reason"))
            ),
            record_schema_version=_integer(item, "record_schema_version"),
        )
    except (TypeError, ValueError) as error:
        raise PersistenceFormatError("invalid phase delivery plan") from error


def serialize_outbox(operation: OutboxOperation) -> DynamoItem:
    """Serialize one outbox operation without requiring boto3."""

    attempt_id = str(operation.attempt_id)
    item: DynamoItem = {
        "PK": f"DEBATE#{operation.debate_id}",
        "SK": f"ATTEMPT#{attempt_id}#OUTBOX#{operation.operation_id}",
        "record_type": "outbox",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": operation.record_schema_version,
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
        "updated_at": _timestamp(
            operation.sent_at or operation.abandoned_at or operation.created_at
        ),
        "delivery_attempt": operation.delivery_attempt,
    }
    _put_optional(item, "claim_owner", operation.claim_owner)
    _put_optional(item, "claim_expiry", _optional_timestamp(operation.claim_expires_at))
    _put_optional(item, "next_retry_at", _optional_timestamp(operation.next_retry_at))
    _put_optional(item, "message_id", operation.message_id)
    _put_optional(item, "sent_at", _optional_timestamp(operation.sent_at))
    if operation.record_schema_version == 2:
        if operation.phase is None or operation.delivery_sequence is None:
            raise PersistenceFormatError("outbox v2 fields disappeared during serialization")
        item.update(
            {
                "phase": operation.phase.value,
                "plan_id": operation.plan_id,
                "delivery_sequence": operation.delivery_sequence,
                "deadline_at": _optional_timestamp(operation.deadline_at),
            }
        )
    _put_optional(item, "abandoned_at", _optional_timestamp(operation.abandoned_at))
    _put_optional(
        item,
        "abandon_reason",
        operation.abandon_reason.value if operation.abandon_reason is not None else None,
    )
    return _validated_item(item)


_OUTBOX_V1_REQUIRED_FIELDS = frozenset(
    {
        "PK",
        "SK",
        "record_type",
        "schema_version",
        "debate_id",
        "attempt_id",
        "operation_id",
        "bot_slot",
        "thread_id",
        "content",
        "content_hash",
        "nonce",
        "chunk_sequence",
        "status",
        "created_at",
        "updated_at",
        "delivery_attempt",
    }
)
_OUTBOX_DELIVERY_OPTIONAL_FIELDS = frozenset(
    {
        "claim_owner",
        "claim_expiry",
        "next_retry_at",
        "message_id",
        "sent_at",
    }
)
_OUTBOX_V1_OPTIONAL_FIELDS = _OUTBOX_DELIVERY_OPTIONAL_FIELDS | {"record_schema_version"}
_OUTBOX_V2_REQUIRED_FIELDS = _OUTBOX_V1_REQUIRED_FIELDS | {
    "record_schema_version",
    "phase",
    "plan_id",
    "delivery_sequence",
    "deadline_at",
}
_OUTBOX_V2_OPTIONAL_FIELDS = _OUTBOX_DELIVERY_OPTIONAL_FIELDS | {
    "abandoned_at",
    "abandon_reason",
}


def deserialize_outbox(raw_item: Mapping[str, DynamoValue]) -> OutboxOperation:
    """Validate and rebuild one persisted outbox operation."""

    item = migrate_item(raw_item)
    if _text(item, "record_type") != "outbox":
        raise PersistenceFormatError("record is not an outbox operation")
    record_schema_version = (
        1 if "record_schema_version" not in item else _integer(item, "record_schema_version")
    )
    if record_schema_version not in {1, 2}:
        raise PersistenceFormatError("invalid outbox operation")
    required_fields = (
        _OUTBOX_V2_REQUIRED_FIELDS if record_schema_version == 2 else _OUTBOX_V1_REQUIRED_FIELDS
    )
    optional_fields = (
        _OUTBOX_V2_OPTIONAL_FIELDS if record_schema_version == 2 else _OUTBOX_V1_OPTIONAL_FIELDS
    )
    fields = frozenset(item)
    if required_fields - fields or fields - required_fields - optional_fields:
        raise PersistenceFormatError("invalid outbox operation fields")
    debate_id_text = _text(item, "debate_id")
    attempt_id_text = _text(item, "attempt_id")
    operation_id = _text(item, "operation_id")
    if (
        _text(item, "PK") != f"DEBATE#{debate_id_text}"
        or _text(item, "SK") != f"ATTEMPT#{attempt_id_text}#OUTBOX#{operation_id}"
    ):
        raise PersistenceFormatError("outbox key conflicts with its identity")
    try:
        return OutboxOperation(
            operation_id=operation_id,
            debate_id=DebateId.parse(debate_id_text),
            attempt_id=AttemptId.parse(attempt_id_text),
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
            record_schema_version=record_schema_version,
            phase=(DebatePhase(_text(item, "phase")) if record_schema_version == 2 else None),
            plan_id=(_text(item, "plan_id") if record_schema_version == 2 else None),
            delivery_sequence=(
                _integer(item, "delivery_sequence") if record_schema_version == 2 else None
            ),
            deadline_at=(_datetime(item, "deadline_at") if record_schema_version == 2 else None),
            abandoned_at=_optional_datetime(item, "abandoned_at"),
            abandon_reason=(
                None
                if "abandon_reason" not in item
                else DeliveryAbandonReason(_text(item, "abandon_reason"))
            ),
        )
    except (TypeError, ValueError) as error:
        raise PersistenceFormatError("invalid outbox operation") from error


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

    return ingress_request_sort_key_from_identity(
        created_at=request.created_at,
        interaction_id=request.interaction_id,
    )


def ingress_request_sort_key_from_identity(*, created_at: datetime, interaction_id: str) -> str:
    """Build the stable request key from its PII-free immutable identity."""

    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(created_at):
        raise ValueError("ingress creation timestamp must be timezone-aware UTC")
    if not interaction_id.strip():
        raise ValueError("interaction ID must not be empty")
    timestamp = created_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"REQUEST#{timestamp}#{interaction_id}"


def serialize_ingress_active_pointer(request: IngressRequest) -> DynamoItem:
    """Serialize the immutable bounded-workset pointer for one queued request."""

    request_sort_key = ingress_request_sort_key(request)
    return _validated_item(
        {
            "PK": "CONTROL#INGRESS#ACTIVE",
            "SK": request_sort_key,
            "record_type": "ingress_active_pointer",
            "schema_version": CURRENT_SCHEMA_VERSION,
            "record_schema_version": INGRESS_ACTIVE_POINTER_RECORD_SCHEMA_VERSION,
            "interaction_id": request.interaction_id,
            "request_sort_key": request_sort_key,
            "created_at": _timestamp(request.created_at),
        }
    )


def deserialize_ingress_active_pointer(
    raw_item: Mapping[str, DynamoValue],
) -> IngressActivePointer:
    """Validate an active pointer without loading request payload or PII."""

    item = _validate_auxiliary_item(
        raw_item,
        expected_type="ingress_active_pointer",
        expected_record_schema_version=INGRESS_ACTIVE_POINTER_RECORD_SCHEMA_VERSION,
    )
    pointer = IngressActivePointer(
        interaction_id=_text(item, "interaction_id"),
        request_sort_key=_text(item, "request_sort_key"),
        created_at=_datetime(item, "created_at"),
        schema_version=_integer(item, "record_schema_version"),
    )
    expected_sort_key = ingress_request_sort_key_from_identity(
        created_at=pointer.created_at,
        interaction_id=pointer.interaction_id,
    )
    if _text(item, "PK") != "CONTROL#INGRESS#ACTIVE":
        raise PersistenceFormatError("ingress active pointer has an invalid partition key")
    if _text(item, "SK") != expected_sort_key:
        raise PersistenceFormatError("ingress active pointer has an invalid sort key")
    if pointer.request_sort_key != expected_sort_key:
        raise PersistenceFormatError("ingress active pointer targets another request")
    return pointer


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
        ("processing_started_at", _optional_timestamp(request.processing_started_at)),
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
            processing_started_at=_optional_datetime(item, "processing_started_at"),
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
    if "gsi2pk" in item or "gsi2sk" in item:
        raise PersistenceFormatError("ingress request must not use the recoverable debate index")
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


def serialize_deployment_lock(lock: DeploymentLock) -> DynamoItem:
    """Serialize the fixed deployment lock without workflow secrets or free text."""

    item: DynamoItem = {
        "PK": "CONTROL#DEPLOYMENT",
        "SK": "LOCK",
        "record_type": "deployment_lock",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "record_schema_version": DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION,
        "lock_state": lock.state.value,
        "fencing_token": lock.fencing_token,
        "version": lock.version,
        "updated_at": _timestamp(lock.updated_at),
    }
    for field, value in (
        ("guard_id", lock.guard_id),
        ("lock_owner", lock.owner),
        ("locked_at", _optional_timestamp(lock.acquired_at)),
        ("lock_expires_at", _optional_timestamp(lock.expires_at)),
        ("deployment_mode", lock.mode.value if lock.mode is not None else None),
        ("break_glass_reason", lock.reason.value if lock.reason is not None else None),
    ):
        _put_optional(item, field, value)
    return _validated_item(item)


def deserialize_deployment_lock(raw_item: Mapping[str, DynamoValue]) -> DeploymentLock:
    """Validate and rebuild the fixed deployment lock control record."""

    item = _validate_auxiliary_item(
        raw_item,
        expected_type="deployment_lock",
        expected_record_schema_version=DEPLOYMENT_LOCK_RECORD_SCHEMA_VERSION,
    )
    raw_mode = _optional_text(item, "deployment_mode")
    raw_reason = _optional_text(item, "break_glass_reason")
    try:
        lock = DeploymentLock(
            state=DeploymentLockState(_text(item, "lock_state")),
            fencing_token=_integer(item, "fencing_token"),
            version=_integer(item, "version"),
            updated_at=_datetime(item, "updated_at"),
            guard_id=_optional_text(item, "guard_id"),
            owner=_optional_text(item, "lock_owner"),
            acquired_at=_optional_datetime(item, "locked_at"),
            expires_at=_optional_datetime(item, "lock_expires_at"),
            mode=DeploymentMode(raw_mode) if raw_mode is not None else None,
            reason=BreakGlassReason(raw_reason) if raw_reason is not None else None,
        )
    except ValueError as error:
        raise PersistenceFormatError("invalid deployment lock") from error
    if _text(item, "PK") != "CONTROL#DEPLOYMENT" or _text(item, "SK") != "LOCK":
        raise PersistenceFormatError("deployment lock has an invalid key")
    if item != serialize_deployment_lock(lock):
        raise PersistenceFormatError("deployment lock has unknown attributes")
    return lock


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
    item: DynamoItem = {
        **common,
        "SK": f"ATTEMPT#{attempt_id}#DECISION",
        "record_type": "decision",
        "attempt_id": attempt_id,
        "winner": value.winner.value,
        "decision": value.decision,
        "actions": list(value.actions),
        "caveats": list(value.caveats),
    }
    _put_optional(item, "victory_message", value.victory_message)
    return item


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
    item = migrate_item(raw_item)
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


def _integer_tuple(item: Mapping[str, DynamoValue], field: str) -> tuple[int, ...]:
    value = item.get(field)
    if not isinstance(value, list) or any(
        isinstance(entry, bool) or not isinstance(entry, int) for entry in value
    ):
        raise PersistenceFormatError(f"{field} must be a list of integers")
    return tuple(cast(list[int], value))


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
