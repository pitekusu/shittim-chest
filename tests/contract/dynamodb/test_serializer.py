"""Contract tests for SDK-independent DynamoDB records."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from shittim_chest.adapters.dynamodb import (
    CURRENT_SCHEMA_VERSION,
    ItemTooLarge,
    OutboxOperation,
    OutboxStatus,
    PanelOperation,
    PanelOperationKind,
    PersistenceFormatError,
    deserialize_outbox,
    deserialize_panel_operation,
    deserialize_snapshot,
    migrate_item,
    serialize_outbox,
    serialize_panel_operation,
    serialize_snapshot,
)
from shittim_chest.adapters.dynamodb.serializer import DynamoItem
from shittim_chest.application import (
    DebateSnapshot,
    DiscordBotSlot,
    LeaseGrant,
    PanelRefreshState,
    TerminalDeliveryPlan,
    content_sha256,
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
    SearchRequirement,
    Vote,
)

NOW = datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC)


def _as_previous_schema(item: DynamoItem) -> DynamoItem:
    """Build a v6 item from a current snapshot item for migration tests."""

    migrated: DynamoItem = {key: value for key, value in item.items()}
    migrated["schema_version"] = 6
    return migrated


def snapshot() -> DebateSnapshot:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    state = DebateState.accepted(debate_id, attempt_id, at=NOW)
    state = state.transition_to(DebatePhase.PREPARING_EVIDENCE, at=NOW + timedelta(seconds=1))
    state = state.transition_to(
        DebatePhase.COLLECTING_INITIAL_OPINIONS,
        at=NOW + timedelta(seconds=2),
    )
    state = state.transition_to(DebatePhase.DISCUSSING, at=NOW + timedelta(seconds=3))
    state = state.transition_to(
        DebatePhase.COLLECTING_FINAL_PROPOSALS,
        at=NOW + timedelta(seconds=4),
    )
    state = state.transition_to(DebatePhase.SELECTING_WINNER, at=NOW + timedelta(seconds=5))
    state = state.transition_to(DebatePhase.GENERATING_DECISION, at=NOW + timedelta(seconds=6))
    opinions = tuple(
        InitialOpinion(slot, f"summary-{slot.value}", "proposal") for slot in PARTICIPANTS
    )
    proposals = tuple(
        FinalProposal(slot, f"title-{slot.value}", "proposal") for slot in PARTICIPANTS
    )
    votes = (
        Vote(ParticipantSlot.PARTICIPANT_A, ParticipantSlot.PARTICIPANT_C, 4, 3, 5, "reason-a"),
        Vote(ParticipantSlot.PARTICIPANT_B, ParticipantSlot.PARTICIPANT_A, 4, 4, 4, "reason-b"),
        Vote(ParticipantSlot.PARTICIPANT_C, ParticipantSlot.PARTICIPANT_B, 5, 3, 4, "reason-c"),
    )
    return DebateSnapshot(
        state=state,
        question="甘い朝食は何がいい?",
        requester_id="requester",
        requester_username="pitekusu",
        requester_display_name="ぬし",
        guild_id="guild",
        channel_id="channel",
        created_at=NOW,
        attempt_created_at=NOW,
        starter_message_id="101",
        thread_id="102",
        control_panel_message_id="103",
        lease=LeaseGrant("worker-1", 1, 42, NOW + timedelta(seconds=60)),
        evidence=EvidenceBundle(
            (
                EvidenceItem(
                    "https://example.test/source",
                    "Source",
                    "publisher=example",
                    "2026-07-17T01:02:03Z",
                    "a" * 64,
                ),
            ),
            required_search_satisfied=True,
            summary="Source-backed summary",
            search_requirement=SearchRequirement.OPTIONAL,
            search_status=EvidenceSearchStatus.COMPLETED,
            search_response_id="resp_evidence",
        ),
        initial_opinions=opinions,
        final_proposals=proposals,
        votes=votes,
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "フレンチトースト",
            ("パンを卵液に浸す",),
            ("アレルギーを確認する",),
        ),
        escalation_assessment=EscalationAssessment(
            rules_version="escalation-shadow-v1",
            split_vote=True,
            winning_axis_low=False,
            winning_average_low=False,
            assessed_at=NOW,
        ),
    )


def test_snapshot_round_trip_preserves_current_attempt_and_vertical_items() -> None:
    source = snapshot()

    items = serialize_snapshot(source)
    restored = deserialize_snapshot(reversed(items))

    assert restored == source
    assert {str(item["record_type"]) for item in items} == {
        "debate_meta",
        "attempt_meta",
        "evidence_meta",
        "evidence",
        "initial_opinion",
        "final_proposal",
        "vote",
        "decision",
        "escalation_assessment",
    }
    debate_meta = next(item for item in items if item["record_type"] == "debate_meta")
    attempt_meta = next(item for item in items if item["record_type"] == "attempt_meta")
    assert debate_meta["gsi1pk"] == "THREAD#102"
    assert debate_meta["control_panel_message_id"] == "103"
    assert attempt_meta["gsi2pk"] == "RECOVERABLE"
    assert attempt_meta["fencing_token"] == 42


def test_terminal_snapshot_removes_recoverable_index() -> None:
    source = snapshot()
    completed_state = source.state.transition_to(
        DebatePhase.COMPLETED,
        at=NOW + timedelta(seconds=7),
    )

    items = serialize_snapshot(replace(source, state=completed_state, lease=None))
    attempt_meta = next(item for item in items if item["record_type"] == "attempt_meta")

    assert "gsi2pk" not in attempt_meta
    assert deserialize_snapshot(items).state.phase is DebatePhase.COMPLETED


def test_terminal_delivery_round_trip_and_partial_fields_fail_closed() -> None:
    source = snapshot()
    staged_at = NOW + timedelta(seconds=7)
    plan = TerminalDeliveryPlan(
        target_phase=DebatePhase.COMPLETED,
        operation_ids=("terminal-completed-0000",),
        content_hashes=("a" * 64,),
        staged_at=staged_at,
    )
    staged = replace(
        source,
        state=replace(source.state, updated_at=staged_at),
        terminal_delivery=plan,
    )
    staged_items = serialize_snapshot(staged)

    assert deserialize_snapshot(staged_items) == staged
    terminal_fields = (
        "terminal_delivery_target",
        "terminal_delivery_operation_ids",
        "terminal_delivery_content_hashes",
        "terminal_delivery_staged_at",
    )
    for field in terminal_fields:
        malformed = tuple(
            {key: value for key, value in item.items() if key != field}
            if item["record_type"] == "attempt_meta"
            else item
            for item in staged_items
        )
        with pytest.raises(PersistenceFormatError, match="terminal delivery"):
            deserialize_snapshot(malformed)

    without_plan = tuple(
        {
            key: value
            for key, value in item.items()
            if key not in {*terminal_fields, "terminal_delivery_completed_at"}
        }
        if item["record_type"] == "attempt_meta"
        else item
        for item in staged_items
    )
    completed_without_plan = tuple(
        {
            **item,
            "terminal_delivery_completed_at": staged_at.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
        }
        if item["record_type"] == "attempt_meta"
        else item
        for item in without_plan
    )
    with pytest.raises(PersistenceFormatError, match="no staged plan"):
        deserialize_snapshot(completed_without_plan)

    completed_at = NOW + timedelta(seconds=8)
    completed = replace(
        staged,
        state=staged.state.transition_to(DebatePhase.COMPLETED, at=completed_at),
        lease=None,
        terminal_delivery=plan.complete(at=completed_at),
    )
    restored = deserialize_snapshot(serialize_snapshot(completed))
    assert restored == completed
    assert restored.terminal_delivery_complete


def test_pending_terminal_panel_refresh_uses_distinct_due_index_and_round_trips() -> None:
    source = snapshot()
    completed_at = NOW + timedelta(seconds=7)
    pending = replace(
        source,
        state=source.state.transition_to(DebatePhase.COMPLETED, at=completed_at),
        lease=None,
        panel_refresh_required_at=completed_at,
        panel_refresh_claim_owner="panel-worker",
        panel_refresh_claim_expires_at=completed_at + timedelta(seconds=60),
        panel_refresh_delivery_attempt=1,
    )

    items = serialize_snapshot(pending)
    attempt_meta = next(item for item in items if item["record_type"] == "attempt_meta")

    assert attempt_meta["gsi2pk"] == "PANEL_REFRESH"
    assert str(attempt_meta["gsi2sk"]).startswith("2026-07-17T01:03:10")
    assert deserialize_snapshot(items) == pending


def test_abandoned_panel_refresh_round_trips_without_a_due_index() -> None:
    source = snapshot()
    completed_at = NOW + timedelta(seconds=7)
    failed_at = completed_at + timedelta(seconds=30)
    abandoned = replace(
        source,
        state=source.state.transition_to(DebatePhase.COMPLETED, at=completed_at),
        lease=None,
        panel_refresh_required_at=completed_at,
        panel_refresh_delivery_attempt=3,
        panel_refresh_failed_at=failed_at,
        panel_refresh_error_code="discord_permission_denied",
    )

    items = serialize_snapshot(abandoned)
    attempt_meta = next(item for item in items if item["record_type"] == "attempt_meta")
    restored = deserialize_snapshot(items)

    assert attempt_meta["panel_refresh_failed_at"] == failed_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert attempt_meta["panel_refresh_error_code"] == "discord_permission_denied"
    assert "gsi2pk" not in attempt_meta
    assert "gsi2sk" not in attempt_meta
    assert restored == abandoned
    assert restored.panel_refresh_state is PanelRefreshState.ABANDONED


def test_empty_evidence_bundle_is_distinct_from_missing_evidence() -> None:
    unavailable = EvidenceBundle(
        required_search_satisfied=False,
        search_requirement=SearchRequirement.OPTIONAL,
        search_status=EvidenceSearchStatus.OPTIONAL_UNAVAILABLE,
    )
    source = replace(snapshot(), evidence=unavailable)
    restored = deserialize_snapshot(serialize_snapshot(source))
    assert restored.evidence == unavailable

    without_evidence = replace(source, evidence=None)
    assert deserialize_snapshot(serialize_snapshot(without_evidence)).evidence is None


def test_previous_schema_is_upconverted_and_unknown_schema_fails_closed() -> None:
    current = serialize_snapshot(snapshot())
    previous = tuple(_as_previous_schema(item) for item in current)
    restored = deserialize_snapshot(previous)
    assert restored.state.schema_version == CURRENT_SCHEMA_VERSION
    assert restored.requester_username == "pitekusu"
    assert restored.requester_display_name == "ぬし"
    assert all(migrate_item(item)["schema_version"] == CURRENT_SCHEMA_VERSION for item in previous)

    re_serialized = serialize_snapshot(restored)
    assert all(item["schema_version"] == CURRENT_SCHEMA_VERSION for item in re_serialized)
    debate_meta = next(item for item in re_serialized if item["record_type"] == "debate_meta")
    assert debate_meta["requester_username"] == restored.requester_username
    assert debate_meta["requester_display_name"] == restored.requester_display_name

    with pytest.raises(PersistenceFormatError, match="unsupported schema"):
        migrate_item({**current[0], "schema_version": 5})
    with pytest.raises(PersistenceFormatError, match="unsupported schema"):
        migrate_item({**current[0], "schema_version": 99})


def test_current_debate_meta_requires_requester_name_snapshots() -> None:
    items = list(serialize_snapshot(snapshot()))
    debate_meta = next(item for item in items if item["record_type"] == "debate_meta")
    for field in ("requester_username", "requester_display_name"):
        missing = tuple(
            {key: value for key, value in item.items() if key != field}
            if item["record_type"] == "debate_meta"
            else item
            for item in items
        )
        with pytest.raises(PersistenceFormatError, match=field):
            deserialize_snapshot(missing)

        empty = tuple(
            {**item, field: ""} if item["record_type"] == "debate_meta" else item for item in items
        )
        with pytest.raises(
            (PersistenceFormatError, ValueError),
            match=r"non-empty|must not be empty",
        ):
            deserialize_snapshot(empty)

    assert debate_meta["requester_username"] == "pitekusu"
    assert debate_meta["requester_display_name"] == "ぬし"


def test_malformed_item_collections_fail_closed() -> None:
    items = serialize_snapshot(snapshot())
    attempt_without_creation = tuple(
        {key: value for key, value in item.items() if key != "attempt_created_at"}
        if item["record_type"] == "attempt_meta"
        else item
        for item in items
    )
    invalid_lease_slot = tuple(
        {**item, "lease_slot": "zero"} if item["record_type"] == "attempt_meta" else item
        for item in items
    )
    mixed_partition = tuple(
        {**item, "PK": "DEBATE#another"} if item["record_type"] == "evidence" else item
        for item in items
    )
    opinion = next(item for item in items if item["record_type"] == "initial_opinion")

    for malformed, message in (
        (items[1:], "debate_meta"),
        ((*items, items[0]), "debate_meta"),
        (attempt_without_creation, "attempt_created_at"),
        (invalid_lease_slot, "lease_slot"),
        (mixed_partition, "multiple partition"),
        ((*items, opinion), "duplicate participant"),
    ):
        with pytest.raises(PersistenceFormatError, match=message):
            deserialize_snapshot(malformed)


def test_outbox_and_panel_records_have_stable_keys_and_versions() -> None:
    source = snapshot()
    outbox = OutboxOperation(
        operation_id="post-decision-0001",
        debate_id=source.state.debate_id,
        attempt_id=source.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="102",
        content="message",
        content_hash=content_sha256("message"),
        nonce="A" * 22,
        chunk_sequence=0,
        status=OutboxStatus.PREPARED,
        created_at=NOW,
    )
    panel = PanelOperation(
        operation_id="retry-operation",
        kind=PanelOperationKind.RETRY,
        debate_id=source.state.debate_id,
        source_attempt_id=source.state.attempt_id,
        result_attempt_id=AttemptId.new(),
        guild_id="guild",
        channel_id="channel",
        requester_id="requester",
        created_at=NOW,
        thread_id="102",
        message_id="103",
    )

    outbox_item = serialize_outbox(outbox)
    panel_item = serialize_panel_operation(panel)

    assert outbox_item["SK"] == f"ATTEMPT#{source.state.attempt_id}#OUTBOX#post-decision-0001"
    assert panel_item["PK"] == "OPERATION#retry-operation"
    assert panel_item["SK"] == "RESULT"
    assert outbox_item["schema_version"] == panel_item["schema_version"] == CURRENT_SCHEMA_VERSION
    assert deserialize_outbox(outbox_item) == outbox
    assert deserialize_panel_operation(panel_item) == panel
    with pytest.raises(PersistenceFormatError, match="not an outbox"):
        deserialize_outbox(panel_item)
    with pytest.raises(PersistenceFormatError, match="not a panel"):
        deserialize_panel_operation(outbox_item)

    previous_outbox = {**outbox_item, "schema_version": 6}
    assert previous_outbox["bot_slot"] == DiscordBotSlot.MODERATOR.value
    assert deserialize_outbox(previous_outbox) == outbox
    with pytest.raises(PersistenceFormatError, match="unsupported schema"):
        deserialize_outbox(
            {
                **{key: value for key, value in outbox_item.items() if key != "bot_slot"},
                "bot_id": "moderator",
                "schema_version": 5,
            }
        )


def test_item_larger_than_400_kb_is_rejected_before_sdk_call() -> None:
    source = snapshot()
    oversized = replace(
        source,
        final_decision=FinalDecision(
            ParticipantSlot.PARTICIPANT_B,
            "x" * (400 * 1024),
            (),
            (),
        ),
    )

    with pytest.raises(ItemTooLarge, match="400 KB"):
        serialize_snapshot(oversized)


def test_number_larger_than_dynamodb_precision_is_rejected() -> None:
    source = snapshot()
    invalid = replace(
        source,
        lease=LeaseGrant("worker", 0, 10**38, NOW + timedelta(seconds=60)),
    )
    with pytest.raises(PersistenceFormatError, match="38 digits"):
        serialize_snapshot(invalid)


def test_outbox_validation_rejects_inconsistent_records() -> None:
    source = snapshot()
    valid = OutboxOperation(
        operation_id="operation",
        debate_id=source.state.debate_id,
        attempt_id=source.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="102",
        content="message",
        content_hash=content_sha256("message"),
        nonce="A" * 22,
        chunk_sequence=0,
        status=OutboxStatus.PREPARED,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="nonce"):
        replace(valid, nonce="short")
    with pytest.raises(ValueError, match="content hash"):
        replace(valid, content_hash="BAD")
    with pytest.raises(ValueError, match="must match"):
        replace(valid, content_hash="d" * 64)
    with pytest.raises(ValueError, match="chunk sequence"):
        replace(valid, chunk_sequence=-1)
    with pytest.raises(ValueError, match="delivery attempt"):
        replace(valid, delivery_attempt=-1)
    with pytest.raises(ValueError, match="owner and expiry"):
        replace(valid, status=OutboxStatus.CLAIMED)
    with pytest.raises(ValueError, match="only a sent"):
        replace(valid, message_id="104")
    with pytest.raises(ValueError, match="2000"):
        replace(valid, content="x" * 2_001)


def test_sent_outbox_requires_complete_delivery_result() -> None:
    source = snapshot()
    sent = OutboxOperation(
        operation_id="sent",
        debate_id=source.state.debate_id,
        attempt_id=source.state.attempt_id,
        bot_slot=DiscordBotSlot.MODERATOR,
        thread_id="102",
        content="message",
        content_hash=content_sha256("message"),
        nonce="A" * 22,
        chunk_sequence=0,
        status=OutboxStatus.SENT,
        created_at=NOW,
        delivery_attempt=1,
        message_id="104",
        sent_at=NOW + timedelta(seconds=1),
    )
    assert serialize_outbox(sent)["status"] == "sent"


def test_panel_retry_and_non_retry_attempt_rules() -> None:
    source = snapshot()
    with pytest.raises(ValueError, match="new result"):
        PanelOperation(
            "operation",
            PanelOperationKind.RETRY,
            source.state.debate_id,
            source.state.attempt_id,
            source.state.attempt_id,
            "guild",
            "channel",
            "requester",
            NOW,
        )
    with pytest.raises(ValueError, match="preserve"):
        PanelOperation(
            "operation",
            PanelOperationKind.CANCEL,
            source.state.debate_id,
            source.state.attempt_id,
            AttemptId.new(),
            "guild",
            "channel",
            "requester",
            NOW,
        )
