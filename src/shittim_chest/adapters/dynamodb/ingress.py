"""Bounded, idempotent DynamoDB ingress queue implementation."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import (
        AttributeValueTypeDef,
        QueryInputTypeDef,
        TransactGetItemTypeDef,
        TransactWriteItemTypeDef,
    )
else:
    TransactGetItemTypeDef = object
    TransactWriteItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.deployment_lock import deployment_lock_open_check
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
    IngressActivePointer,
    PersistenceFormatError,
    deserialize_ingress_active_pointer,
    deserialize_ingress_operation_result,
    deserialize_ingress_request,
    deserialize_ingress_semantic_binding,
    deserialize_ingress_status_publication,
    ingress_request_sort_key,
    serialize_ingress_active_pointer,
    serialize_ingress_operation_result,
    serialize_ingress_request,
    serialize_ingress_semantic_binding,
    serialize_ingress_status_publication,
)
from shittim_chest.adapters.dynamodb.transaction_errors import (
    is_condition_only_cancellation,
)
from shittim_chest.application.models import DebateSnapshot
from shittim_chest.application.ports import (
    RepositoryConflict,
    RepositoryIdentityConflict,
    RepositoryQueueFull,
    RepositoryTransactionAction,
    RepositoryUnavailable,
)
from shittim_chest.application.scale_to_zero import (
    INGRESS_CLAIM_SECONDS,
    INGRESS_QUEUE_LIMIT,
    STATUS_PUBLICATION_CLAIM_SECONDS,
    EnqueuedIngress,
    IngressKind,
    IngressOperationResult,
    IngressRequest,
    IngressSemanticOperationBinding,
    IngressStatus,
    IngressStatusPublication,
    IngressWakeCandidate,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationState,
    StatusPublicationWork,
    status_publication_nonce,
)
from shittim_chest.application.status_publication import (
    render_public_status,
    status_content_hash,
)
from shittim_chest.domain import AttemptId, DebateId, DebatePhase

INGRESS_RECORD_SCHEMA_VERSION = 1
INGRESS_STATUS_PUBLICATION_RECORD_SCHEMA_VERSION = 3
INGRESS_ACTIVE_POINTER_PARTITION = "CONTROL#INGRESS#ACTIVE"

P = ParamSpec("P")
T = TypeVar("T")


class DynamoDbIngressRepository:
    """Store a twenty-entry FIFO with transactional replay and counter records."""

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        return await self._run(self._enqueue, request)

    def terminal_projection_actions(
        self,
        *,
        snapshot: DebateSnapshot,
        at: datetime,
    ) -> tuple[
        tuple[TransactWriteItemTypeDef, ...],
        tuple[RepositoryTransactionAction, ...],
    ]:
        """Build fenced ingress/status actions for one terminal debate transaction."""

        interaction_id = snapshot.origin_ingress_interaction_id
        if interaction_id is None:
            return (), ()
        operation_item = self._get_item(_operation_key(interaction_id))
        if operation_item is None:
            raise RepositoryConflict("terminal debate origin operation is missing")
        operation = deserialize_ingress_operation_result(operation_item)
        request_item = self._get_item(_request_key(operation.request_sort_key))
        if request_item is None:
            raise RepositoryConflict("terminal debate origin request is missing")
        request = deserialize_ingress_request(request_item)
        if (
            operation.status is not IngressStatus.ACCEPTED
            or request.status is not IngressStatus.ACCEPTED
            or request.interaction_id != interaction_id
            or request.kind not in {IngressKind.NEW_DEBATE, IngressKind.RETRY}
            or request.accepted_debate_id != snapshot.state.debate_id
            or request.accepted_attempt_id != snapshot.state.attempt_id
            or operation.accepted_debate_id != snapshot.state.debate_id
            or operation.accepted_attempt_id != snapshot.state.attempt_id
            or operation.operation_id != request.operation_id
            or operation.request_sort_key != ingress_request_sort_key(request)
        ):
            raise RepositoryConflict("terminal debate origin identity is inconsistent")
        terminal_status, message_state, error_code = _terminal_ingress_state(snapshot)
        updated = replace(
            request,
            status=terminal_status,
            status_message_state=message_state,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
            completed_at=at,
        )
        status_actions = self._rearm_status_actions(
            request=updated,
            state=message_state,
            at=at,
        )
        request_actions = self._replace_request_and_operation_actions(
            previous=request,
            updated=updated,
            condition=self._request_condition(request),
            values=self._expected_values(request),
        )
        actions = (*request_actions, *status_actions)
        action_kinds = (
            RepositoryTransactionAction.INGRESS_REQUEST,
            RepositoryTransactionAction.INGRESS_OPERATION,
            *(RepositoryTransactionAction.STATUS_PUBLICATION for _ in status_actions),
        )
        return actions, action_kinds

    async def get_replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        return await self._run(self._replay, request)

    async def get_operation_result(
        self,
        interaction_id: str,
    ) -> IngressOperationResult | None:
        return await self._run(self._get_operation_result, interaction_id)

    async def list_ready(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_ready, at)

    async def list_active_wake_candidates(self) -> tuple[IngressWakeCandidate, ...]:
        return await self._run(self._list_active_wake_candidates)

    async def claim(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        return await self._run(self._claim, request, claim_owner, at)

    async def reschedule(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        return await self._run(
            self._reschedule,
            request,
            claim_owner,
            at,
            next_attempt_at,
            error_code,
        )

    async def mark_accepted(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest:
        return await self._run(
            self._mark_accepted,
            request,
            claim_owner,
            at,
            debate_id,
            attempt_id,
        )

    async def mark_startup_timeout(
        self,
        *,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest:
        return await self._run(self._mark_startup_timeout, request, at)

    async def mark_terminal(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        return await self._run(
            self._mark_terminal,
            request,
            at,
            status,
            error_code,
        )

    async def mark_terminal_deadline(
        self,
        *,
        request: IngressRequest,
        at: datetime,
        error_code: str,
    ) -> IngressRequest:
        return await self._run(self._mark_terminal_deadline, request, at, error_code)

    async def mark_claim_terminal(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        status: IngressStatus,
        error_code: str,
    ) -> IngressRequest:
        return await self._run(
            self._mark_claim_terminal,
            request,
            claim_owner,
            at,
            status,
            error_code,
        )

    async def list_startup_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_startup_deadlines, at)

    async def list_terminal_deadlines(self, *, at: datetime) -> tuple[IngressRequest, ...]:
        return await self._run(self._list_terminal_deadlines, at)

    async def active_count(self) -> int:
        return await self._run(self._active_count)

    async def get_status_publication(
        self,
        interaction_id: str,
    ) -> IngressStatusPublication | None:
        return await self._run(self._get_status_publication, interaction_id)

    async def pending_status_count(self) -> int:
        return await self._run(self._pending_status_count)

    async def claim_status_publication(
        self,
        *,
        interaction_id: str,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None:
        return await self._run(
            self._claim_status_publication,
            interaction_id,
            claim_owner,
            at,
        )

    async def reschedule_status_publication(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
        history_checkpoint: StatusHistoryCheckpoint | None = None,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication:
        return await self._run(
            self._reschedule_status_publication,
            work,
            claim_owner,
            at,
            next_attempt_at,
            error_code,
            history_checkpoint,
            message_may_exist,
        )

    async def mark_status_delivered(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> IngressStatusPublication:
        return await self._run(
            self._mark_status_delivered,
            work,
            claim_owner,
            message_id,
            at,
        )

    async def mark_status_failed(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        error_code: str,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication:
        return await self._run(
            self._mark_status_failed,
            work,
            claim_owner,
            at,
            error_code,
            message_may_exist,
        )

    async def replace_missing_status_message(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork:
        return await self._run(
            self._replace_missing_status_message,
            work,
            claim_owner,
            at,
        )

    async def list_due_status_publications(
        self,
        *,
        at: datetime,
        limit: int,
    ) -> tuple[IngressStatusPublication, ...]:
        return await self._run(self._list_due_status_publications, at, limit)

    async def request_status_publication(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        at: datetime,
    ) -> IngressRequest:
        return await self._run(self._request_status_publication, request, state, at)

    async def _run(self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except BotoCoreError, ClientError:
            raise RepositoryUnavailable from None
        except PersistenceFormatError:
            raise RepositoryConflict("ingress repository record is invalid") from None

    def _enqueue(self, request: IngressRequest) -> EnqueuedIngress:
        if request.status is not IngressStatus.PENDING:
            raise ValueError("new ingress request must be pending")
        operation = _operation_for_request(request)
        publication = IngressStatusPublication.prepared(
            request,
            content=render_public_status(request, StatusMessageState.STARTING),
        )
        actions = [
            self._increment_counter_action(request.created_at),
            self._increment_status_counter_action(request.created_at),
            self._put_new(serialize_ingress_request(request)),
            self._put_new(serialize_ingress_active_pointer(request)),
            self._put_new(serialize_ingress_operation_result(operation)),
            self._put_new(serialize_ingress_status_publication(publication)),
        ]
        if request.kind is not IngressKind.NEW_DEBATE:
            actions.append(
                self._put_new(serialize_ingress_semantic_binding(_binding_for_request(request)))
            )
        try:
            wrote_items = self._transact(
                actions,
                token=_client_token(
                    f"{self._table_name}:ingress:{request.interaction_id}:{request.operation_id}"
                ),
                aggregate_write_floor=len(actions) - 1,
            )
        except RepositoryConflict:
            replay, active_count = self._resolve_enqueue_conflict(request)
            if replay is not None:
                return replay
            if active_count >= INGRESS_QUEUE_LIMIT:
                raise RepositoryQueueFull(
                    "ingress queue already contains twenty requests"
                ) from None
            raise RepositoryUnavailable from None
        if not wrote_items:
            replay = self._replay(request)
            if replay is None:
                raise RepositoryConflict("idempotent ingress transaction has no replay result")
            return replay
        return EnqueuedIngress(request=request, operation=operation, created=True)

    def _resolve_enqueue_conflict(
        self,
        request: IngressRequest,
    ) -> tuple[EnqueuedIngress | None, int]:
        replay_key = (
            _operation_key(request.interaction_id)
            if request.kind is IngressKind.NEW_DEBATE
            else _semantic_binding_key(request.operation_id)
        )
        replay_item, counter_item = self._transact_get_items((replay_key, _counter_key()))
        active_count = _active_count_from_item(counter_item)
        if replay_item is None:
            return None, active_count
        if request.kind is IngressKind.NEW_DEBATE:
            operation = deserialize_ingress_operation_result(replay_item)
            persisted = self._load_replay_request(operation)
            _assert_exact_identity(request, persisted)
        else:
            binding = deserialize_ingress_semantic_binding(replay_item)
            operation, persisted = self._load_bound_replay(binding)
            _validate_binding(binding, operation)
            _assert_semantic_identity(request, persisted)
        return (
            EnqueuedIngress(request=persisted, operation=operation, created=False),
            active_count,
        )

    def _replay(self, request: IngressRequest) -> EnqueuedIngress | None:
        if request.kind is IngressKind.NEW_DEBATE:
            operation = self._get_operation_result(request.interaction_id)
            if operation is None:
                return None
            persisted = self._load_replay_request(operation)
            _assert_exact_identity(request, persisted)
            return EnqueuedIngress(request=persisted, operation=operation, created=False)
        binding = self._get_semantic_binding(request.operation_id)
        if binding is None:
            return None
        operation, persisted = self._load_bound_replay(binding)
        _validate_binding(binding, operation)
        _assert_semantic_identity(request, persisted)
        return EnqueuedIngress(request=persisted, operation=operation, created=False)

    def _load_replay_request(self, operation: IngressOperationResult) -> IngressRequest:
        request_item, publication_item = self._transact_get_items(
            (
                _request_key(operation.request_sort_key),
                _status_publication_key(operation.interaction_id),
            )
        )
        if request_item is None:
            raise RepositoryConflict("ingress operation result points to a missing request")
        if publication_item is None:
            raise RepositoryConflict("ingress operation result has no status publication")
        persisted = deserialize_ingress_request(request_item)
        publication = deserialize_ingress_status_publication(publication_item)
        self._validate_replay_bundle(operation, persisted, publication)
        return persisted

    def _load_bound_replay(
        self,
        binding: IngressSemanticOperationBinding,
    ) -> tuple[IngressOperationResult, IngressRequest]:
        operation_item, request_item, publication_item = self._transact_get_items(
            (
                _operation_key(binding.canonical_interaction_id),
                _request_key(binding.request_sort_key),
                _status_publication_key(binding.canonical_interaction_id),
            )
        )
        if operation_item is None:
            raise RepositoryConflict("semantic operation binding points to a missing result")
        if request_item is None:
            raise RepositoryConflict("ingress operation result points to a missing request")
        if publication_item is None:
            raise RepositoryConflict("ingress operation result has no status publication")
        operation = deserialize_ingress_operation_result(operation_item)
        persisted = deserialize_ingress_request(request_item)
        publication = deserialize_ingress_status_publication(publication_item)
        self._validate_replay_bundle(operation, persisted, publication)
        return operation, persisted

    @staticmethod
    def _validate_replay_bundle(
        operation: IngressOperationResult,
        persisted: IngressRequest,
        publication: IngressStatusPublication,
    ) -> None:
        if persisted.operation_id != operation.operation_id:
            raise RepositoryConflict("ingress operation result points to another operation")
        if persisted.interaction_id != operation.interaction_id:
            raise RepositoryConflict("ingress operation result points to another interaction")
        if (
            persisted.status != operation.status
            or persisted.created_at != operation.created_at
            or persisted.updated_at != operation.updated_at
            or persisted.accepted_debate_id != operation.accepted_debate_id
            or persisted.accepted_attempt_id != operation.accepted_attempt_id
            or persisted.error_code != operation.error_code
        ):
            raise RepositoryConflict("ingress operation result disagrees with its request")
        if (
            publication.canonical_interaction_id != operation.interaction_id
            or publication.request_sort_key != operation.request_sort_key
            or publication.status_channel_id != persisted.status_channel_id
        ):
            raise RepositoryConflict("ingress status publication points to another request")
        try:
            StatusPublicationWork(request=persisted, publication=publication)
        except ValueError:
            raise RepositoryConflict("ingress status publication bundle is inconsistent") from None

    def _get_operation_result(self, interaction_id: str) -> IngressOperationResult | None:
        if not interaction_id.strip():
            raise ValueError("interaction ID must not be empty")
        item = self._get_item(_operation_key(interaction_id))
        return None if item is None else deserialize_ingress_operation_result(item)

    def _get_semantic_binding(
        self,
        operation_id: str,
    ) -> IngressSemanticOperationBinding | None:
        if not operation_id.strip():
            raise ValueError("operation ID must not be empty")
        item = self._get_item(_semantic_binding_key(operation_id))
        return None if item is None else deserialize_ingress_semantic_binding(item)

    def _get_status_publication(
        self,
        interaction_id: str,
    ) -> IngressStatusPublication | None:
        if not interaction_id.strip():
            raise ValueError("canonical interaction ID must not be empty")
        item = self._get_item(_status_publication_key(interaction_id))
        return None if item is None else deserialize_ingress_status_publication(item)

    def _status_work(self, interaction_id: str) -> StatusPublicationWork | None:
        publication = self._get_status_publication(interaction_id)
        if publication is None:
            return None
        request_item, current_publication_item = self._transact_get_items(
            (
                _request_key(publication.request_sort_key),
                _status_publication_key(interaction_id),
            )
        )
        if request_item is None or current_publication_item is None:
            raise RepositoryConflict("status publication bundle is incomplete")
        request = deserialize_ingress_request(request_item)
        current = deserialize_ingress_status_publication(current_publication_item)
        try:
            return StatusPublicationWork(request=request, publication=current)
        except ValueError:
            raise RepositoryConflict("status publication bundle is inconsistent") from None

    def _latest_owned_status_work(
        self,
        *,
        stale_work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None:
        latest = self._status_work(stale_work.publication.canonical_interaction_id)
        if latest is None:
            return None
        publication = latest.publication
        if (
            publication.state is not StatusPublicationState.CLAIMED
            or publication.claim_owner != claim_owner
            or publication.claim_expires_at is None
            or publication.claim_expires_at <= at
            or publication.nonce != stale_work.publication.nonce
            or publication.incarnation != stale_work.publication.incarnation
            or publication.delivery_attempt != stale_work.publication.delivery_attempt
            or publication.status_channel_id != stale_work.publication.status_channel_id
        ):
            return None
        return latest

    def _claim_status_publication(
        self,
        interaction_id: str,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None:
        _require_utc(at)
        if not interaction_id.strip() or not claim_owner.strip():
            raise ValueError("status publication identity and claim owner must not be empty")
        work = self._status_work(interaction_id)
        if work is None or not _status_publication_is_due(work.publication, at):
            return None
        publication = work.publication
        claimed = replace(
            publication,
            state=StatusPublicationState.CLAIMED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(seconds=STATUS_PUBLICATION_CLAIM_SECONDS),
            delivery_attempt=publication.delivery_attempt + 1,
            history_reconciliation_required=(
                publication.history_reconciliation_required
                or (
                    publication.state is StatusPublicationState.CLAIMED
                    and publication.status_message_id is None
                )
            ),
            error_code=None,
        )
        actions = (
            self._put_status_publication_action(
                previous=publication,
                updated=claimed,
                extra_condition=_status_due_condition(publication),
                extra_values={":claim_at": _timestamp(at)},
            ),
            self._check_status_request_action(work.request),
        )
        try:
            self._transact(
                actions,
                token=_client_token(
                    f"{self._table_name}:status:claim:{interaction_id}:{claim_owner}:{at}"
                ),
            )
        except RepositoryConflict:
            return None
        return StatusPublicationWork(request=work.request, publication=claimed)

    def _reschedule_status_publication(
        self,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
        history_checkpoint: StatusHistoryCheckpoint | None = None,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication:
        _require_utc(at)
        _require_utc(next_attempt_at)
        if next_attempt_at <= at:
            raise ValueError("status publication retry must be scheduled in the future")
        _require_status_claim(work.publication, claim_owner)
        if not error_code.strip():
            raise ValueError("status publication retry error code must not be empty")
        if not isinstance(message_may_exist, bool):
            raise ValueError("status publication message ambiguity flag must be a boolean")
        history_progressed = (
            history_checkpoint is not None
            and history_checkpoint != work.publication.history_checkpoint
        )
        if history_checkpoint is not None:
            _validate_history_checkpoint_progress(
                work.publication.history_checkpoint,
                history_checkpoint,
            )
        unbound_message_may_exist = message_may_exist and work.publication.status_message_id is None
        updated = replace(
            work.publication,
            state=StatusPublicationState.RETRYING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=(0 if history_progressed else work.publication.delivery_attempt),
            history_checkpoint=(
                history_checkpoint
                if history_checkpoint is not None
                else work.publication.history_checkpoint
            ),
            history_reconciliation_required=(
                True
                if history_checkpoint is not None or unbound_message_may_exist
                else work.publication.history_reconciliation_required
            ),
            error_code=error_code,
        )
        try:
            self._transact(
                (
                    self._put_status_publication_action(
                        previous=work.publication,
                        updated=updated,
                        extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                        extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
                    ),
                    self._check_status_request_action(work.request),
                ),
                token=_client_token(
                    f"{self._table_name}:status:retry:"
                    f"{work.publication.canonical_interaction_id}:{claim_owner}:{at}:"
                    f"{next_attempt_at}:{error_code}:"
                    f"{_history_checkpoint_token(updated.history_checkpoint)}:"
                    f"{message_may_exist}"
                ),
            )
        except RepositoryConflict:
            current = self._get_status_publication(work.publication.canonical_interaction_id)
            if (
                current is not None
                and current.state is StatusPublicationState.RETRYING
                and current.next_attempt_at == next_attempt_at
                and current.error_code == error_code
                and current.history_checkpoint == updated.history_checkpoint
                and current.history_reconciliation_required
                == updated.history_reconciliation_required
                and current.delivery_attempt == updated.delivery_attempt
                and current.content_hash == updated.content_hash
                and current.nonce == updated.nonce
            ):
                return current
            released = self._release_latest_status_claim(
                stale_work=work,
                claim_owner=claim_owner,
                at=at,
                next_attempt_at=next_attempt_at,
                retry_error_code=error_code,
                history_checkpoint=history_checkpoint,
                message_may_exist=message_may_exist,
            )
            if released is not None:
                return released
            raise
        return updated

    def _release_latest_status_claim(
        self,
        *,
        stale_work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime | None = None,
        retry_error_code: str | None = None,
        permanent_error_code: str | None = None,
        history_checkpoint: StatusHistoryCheckpoint | None = None,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication | None:
        if (next_attempt_at is None) is not (retry_error_code is None):
            raise ValueError("stale status retry time and error code must be set together")
        if (next_attempt_at is None) is (permanent_error_code is None):
            raise ValueError("exactly one stale status settlement mode must be selected")
        if next_attempt_at is not None:
            _require_utc(next_attempt_at)
        if not isinstance(message_may_exist, bool):
            raise ValueError("stale status message ambiguity flag must be a boolean")
        latest = self._latest_owned_status_work(
            stale_work=stale_work,
            claim_owner=claim_owner,
            at=at,
        )
        if latest is None:
            return None
        publication = latest.publication
        same_revision = (
            publication.desired_state is stale_work.publication.desired_state
            and publication.content_hash == stale_work.publication.content_hash
        )
        updated_at = max(at, publication.updated_at)
        retry_at = max(next_attempt_at, updated_at) if next_attempt_at is not None else None
        permanently_failed = permanent_error_code is not None and same_revision
        history_progressed = (
            history_checkpoint is not None and history_checkpoint != publication.history_checkpoint
        )
        if history_checkpoint is not None:
            _validate_history_checkpoint_progress(
                publication.history_checkpoint,
                history_checkpoint,
            )
        released = replace(
            publication,
            state=(
                StatusPublicationState.RETRYING
                if next_attempt_at is not None
                else (
                    StatusPublicationState.FAILED
                    if permanently_failed
                    else StatusPublicationState.PREPARED
                )
            ),
            updated_at=updated_at,
            next_attempt_at=(
                retry_at
                if next_attempt_at is not None
                else (None if permanently_failed else updated_at)
            ),
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=(
                0 if history_progressed or not same_revision else publication.delivery_attempt
            ),
            history_checkpoint=(
                history_checkpoint
                if history_checkpoint is not None
                else publication.history_checkpoint
            ),
            history_reconciliation_required=(
                publication.status_message_id is None
                and (
                    publication.history_reconciliation_required
                    or history_checkpoint is not None
                    or message_may_exist
                )
            ),
            error_code=(
                retry_error_code
                if next_attempt_at is not None
                else (permanent_error_code if permanently_failed else None)
            ),
        )
        actions = [
            self._put_status_publication_action(
                previous=publication,
                updated=released,
                extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
            ),
            self._check_status_request_action(latest.request),
        ]
        if permanently_failed:
            actions.append(self._decrement_status_counter_action(at))
        self._transact(
            actions,
            token=_client_token(
                f"{self._table_name}:status:release-stale:"
                f"{publication.canonical_interaction_id}:{claim_owner}:"
                f"{publication.content_hash}:{retry_at}:"
                f"{retry_error_code}:{permanent_error_code}:"
                f"{_history_checkpoint_token(released.history_checkpoint)}:"
                f"{message_may_exist}:{publication.delivery_attempt}:{publication.nonce}:"
                f"{updated_at}"
            ),
        )
        return released

    def _mark_status_delivered(
        self,
        work: StatusPublicationWork,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> IngressStatusPublication:
        _require_utc(at)
        _require_status_claim(work.publication, claim_owner)
        if not message_id.strip():
            raise ValueError("status message ID must not be empty")
        if (
            work.publication.status_message_id is not None
            and work.publication.status_message_id != message_id
        ):
            raise RepositoryConflict("status delivery cannot rebind a known message")
        delivered = replace(
            work.publication,
            state=StatusPublicationState.DELIVERED,
            delivered_state=work.publication.desired_state,
            status_message_id=message_id,
            status_message_updated_at=at,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            history_checkpoint=None,
            history_reconciliation_required=False,
            error_code=None,
        )
        request = replace(
            work.request,
            status_message_id=message_id,
            status_message_updated_at=at,
        )
        try:
            self._transact(
                (
                    self._put_status_publication_action(
                        previous=work.publication,
                        updated=delivered,
                        extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                        extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
                    ),
                    self._put_status_request_metadata_action(
                        previous=work.request,
                        updated=request,
                    ),
                    self._decrement_status_counter_action(at),
                ),
                token=_client_token(
                    f"{self._table_name}:status:delivered:"
                    f"{work.publication.canonical_interaction_id}:{claim_owner}:"
                    f"{work.publication.desired_state.value}:{message_id}:"
                    f"{work.publication.delivery_attempt}:{work.publication.nonce}:"
                    f"{work.publication.content_hash}:{at}"
                ),
            )
        except RepositoryConflict:
            current = self._get_status_publication(work.publication.canonical_interaction_id)
            if (
                current is not None
                and current.state is StatusPublicationState.DELIVERED
                and current.desired_state is work.publication.desired_state
                and current.status_message_id == message_id
                and current.content_hash == work.publication.content_hash
            ):
                return current
            converged = self._settle_latest_status_delivery(
                stale_work=work,
                claim_owner=claim_owner,
                message_id=message_id,
                at=at,
            )
            if converged is not None:
                return converged
            raise
        return delivered

    def _settle_latest_status_delivery(
        self,
        *,
        stale_work: StatusPublicationWork,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> IngressStatusPublication | None:
        latest = self._latest_owned_status_work(
            stale_work=stale_work,
            claim_owner=claim_owner,
            at=at,
        )
        if latest is None:
            return None
        publication = latest.publication
        if (
            publication.status_message_id is not None
            and publication.status_message_id != message_id
        ):
            raise RepositoryConflict("stale status delivery cannot rebind a known message")
        same_revision_content = (
            publication.desired_state is stale_work.publication.desired_state
            and publication.content_hash == stale_work.publication.content_hash
        )
        updated_at = max(at, publication.updated_at)
        updated = replace(
            publication,
            state=(
                StatusPublicationState.DELIVERED
                if same_revision_content
                else StatusPublicationState.PREPARED
            ),
            delivered_state=stale_work.publication.desired_state,
            status_message_id=message_id,
            status_message_updated_at=at,
            updated_at=updated_at,
            next_attempt_at=None if same_revision_content else updated_at,
            claim_owner=None,
            claim_expires_at=None,
            history_checkpoint=None,
            delivery_attempt=0 if not same_revision_content else publication.delivery_attempt,
            history_reconciliation_required=False,
            error_code=None,
        )
        request = replace(
            latest.request,
            status_message_id=message_id,
            status_message_updated_at=at,
        )
        actions = [
            self._put_status_publication_action(
                previous=publication,
                updated=updated,
                extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
            ),
            self._put_status_request_metadata_action(
                previous=latest.request,
                updated=request,
            ),
        ]
        if same_revision_content:
            actions.append(self._decrement_status_counter_action(at))
        self._transact(
            actions,
            token=_client_token(
                f"{self._table_name}:status:stale-delivery:"
                f"{publication.canonical_interaction_id}:{claim_owner}:"
                f"{publication.content_hash}:{message_id}:"
                f"{publication.delivery_attempt}:{publication.nonce}:{updated_at}"
            ),
        )
        return updated

    def _mark_status_failed(
        self,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        error_code: str,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication:
        _require_utc(at)
        _require_status_claim(work.publication, claim_owner)
        if not error_code.strip():
            raise ValueError("status publication failure code must not be empty")
        if not isinstance(message_may_exist, bool):
            raise ValueError("status publication message ambiguity flag must be a boolean")
        unbound_message_may_exist = message_may_exist and work.publication.status_message_id is None
        failed = replace(
            work.publication,
            state=StatusPublicationState.FAILED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            history_reconciliation_required=(
                work.publication.history_reconciliation_required or unbound_message_may_exist
            ),
            error_code=error_code,
        )
        try:
            self._transact(
                (
                    self._put_status_publication_action(
                        previous=work.publication,
                        updated=failed,
                        extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                        extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
                    ),
                    self._check_status_request_action(work.request),
                    self._decrement_status_counter_action(at),
                ),
                token=_client_token(
                    f"{self._table_name}:status:failed:"
                    f"{work.publication.canonical_interaction_id}:{claim_owner}:"
                    f"{work.publication.desired_state.value}:{error_code}:"
                    f"{work.publication.delivery_attempt}:{work.publication.nonce}:"
                    f"{work.publication.content_hash}:{at}:{message_may_exist}"
                ),
            )
        except RepositoryConflict:
            current = self._get_status_publication(work.publication.canonical_interaction_id)
            if (
                current is not None
                and current.state is StatusPublicationState.FAILED
                and current.desired_state is work.publication.desired_state
                and current.error_code == error_code
                and current.history_reconciliation_required
                == failed.history_reconciliation_required
            ):
                return current
            released = self._release_latest_status_claim(
                stale_work=work,
                claim_owner=claim_owner,
                at=at,
                permanent_error_code=error_code,
                message_may_exist=message_may_exist,
            )
            if released is not None:
                return released
            raise
        return failed

    def _replace_missing_status_message(
        self,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork:
        _require_utc(at)
        _require_status_claim(work.publication, claim_owner)
        if work.publication.status_message_id is None:
            raise RepositoryConflict("only a known status message can be replaced")
        incarnation = work.publication.incarnation + 1
        replacement = replace(
            work.publication,
            state=StatusPublicationState.RETRYING,
            nonce=status_publication_nonce(
                work.publication.canonical_interaction_id,
                incarnation=incarnation,
            ),
            status_message_id=None,
            status_message_updated_at=None,
            history_checkpoint=None,
            history_reconciliation_required=False,
            updated_at=at,
            next_attempt_at=at,
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=0,
            incarnation=incarnation,
            error_code="status_message_missing",
        )
        request = replace(
            work.request,
            status_message_id=None,
            status_message_updated_at=None,
        )
        try:
            self._transact(
                (
                    self._put_status_publication_action(
                        previous=work.publication,
                        updated=replacement,
                        extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                        extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
                    ),
                    self._put_status_request_metadata_action(
                        previous=work.request,
                        updated=request,
                    ),
                ),
                token=_client_token(
                    f"{self._table_name}:status:missing:"
                    f"{work.publication.canonical_interaction_id}:{claim_owner}:{incarnation}"
                ),
            )
        except RepositoryConflict:
            latest = self._replace_latest_missing_status_message(
                stale_work=work,
                claim_owner=claim_owner,
                at=at,
            )
            if latest is not None:
                return latest
            raise
        return StatusPublicationWork(request=request, publication=replacement)

    def _replace_latest_missing_status_message(
        self,
        *,
        stale_work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None:
        latest = self._latest_owned_status_work(
            stale_work=stale_work,
            claim_owner=claim_owner,
            at=at,
        )
        if (
            latest is None
            or latest.publication.status_message_id != stale_work.publication.status_message_id
        ):
            return None
        publication = latest.publication
        incarnation = publication.incarnation + 1
        updated_at = max(at, publication.updated_at)
        replacement = replace(
            publication,
            state=StatusPublicationState.RETRYING,
            nonce=status_publication_nonce(
                publication.canonical_interaction_id,
                incarnation=incarnation,
            ),
            status_message_id=None,
            status_message_updated_at=None,
            history_checkpoint=None,
            history_reconciliation_required=False,
            updated_at=updated_at,
            next_attempt_at=updated_at,
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=0,
            incarnation=incarnation,
            error_code="status_message_missing",
        )
        request = replace(
            latest.request,
            status_message_id=None,
            status_message_updated_at=None,
        )
        self._transact(
            (
                self._put_status_publication_action(
                    previous=publication,
                    updated=replacement,
                    extra_condition="claim_owner=:owner AND claim_expiry > :claim_at",
                    extra_values={":owner": claim_owner, ":claim_at": _timestamp(at)},
                ),
                self._put_status_request_metadata_action(
                    previous=latest.request,
                    updated=request,
                ),
            ),
            token=_client_token(
                f"{self._table_name}:status:missing-latest:"
                f"{publication.canonical_interaction_id}:{claim_owner}:{incarnation}"
            ),
        )
        return StatusPublicationWork(request=request, publication=replacement)

    def _list_due_status_publications(
        self,
        at: datetime,
        limit: int,
    ) -> tuple[IngressStatusPublication, ...]:
        _require_utc(at)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("status publication query limit must be between 1 and 100")
        publications: list[IngressStatusPublication] = []
        exclusive_start_key: dict[str, AttributeValueTypeDef] | None = None
        while len(publications) < limit:
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "IndexName": "gsi1",
                "KeyConditionExpression": "gsi1pk=:due AND gsi1sk <= :at",
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":due": "INGRESS#STATUS_DUE",
                        ":at": f"{_timestamp(at)}#\uffff",
                    }
                ),
                "ScanIndexForward": True,
                "Limit": limit - len(publications),
            }
            if exclusive_start_key is not None:
                parameters["ExclusiveStartKey"] = exclusive_start_key
            response = self._client.query(**parameters)
            publications.extend(
                deserialize_ingress_status_publication(unmarshal_item(item))
                for item in response.get("Items", [])
            )
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break
        return tuple(publications[:limit])

    def _list_ready(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        ready: list[IngressRequest] = []
        for request in self._query_active():
            if not _is_ready(request, at):
                break
            ready.append(request)
        return tuple(ready)

    def _list_active_wake_candidates(self) -> tuple[IngressWakeCandidate, ...]:
        return tuple(IngressWakeCandidate.from_request(request) for request in self._query_active())

    def _claim(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest | None:
        _require_utc(at)
        if not claim_owner.strip():
            raise ValueError("claim owner must not be empty")
        if not _is_ready(request, at):
            return None
        claimed = replace(
            request,
            status=IngressStatus.CLAIMED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(seconds=INGRESS_CLAIM_SECONDS),
            delivery_attempt=request.delivery_attempt + 1,
            error_code=None,
            error_detail_code=None,
        )
        condition = self._request_condition(request)
        if request.processing_started_at is None:
            condition += " AND terminal_deadline_at > :at"
        values = self._expected_values(request)
        values[":at"] = _timestamp(at)
        if request.status is IngressStatus.RETRYING:
            condition += " AND (attribute_not_exists(next_attempt_at) OR next_attempt_at <= :at)"
        elif request.status is IngressStatus.CLAIMED:
            condition += " AND claim_expiry <= :at"
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=claimed,
                condition=condition,
                values=values,
                token_suffix=f"claim:{claim_owner}:{at}",
            )
        except RepositoryConflict:
            return None
        return claimed

    def _reschedule(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        next_attempt_at: datetime,
        error_code: str,
    ) -> IngressRequest:
        _require_utc(at)
        _require_utc(next_attempt_at)
        if next_attempt_at <= at:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.RETRYING
                and current.next_attempt_at == next_attempt_at
                and current.error_code == error_code
            ):
                return current
            raise ValueError("next ingress attempt must be in the future")
        if not error_code.strip():
            raise ValueError("reschedule error code must not be empty")
        current = self._load_current(request)
        if (
            current is not None
            and current.status is IngressStatus.RETRYING
            and current.next_attempt_at == next_attempt_at
            and current.error_code == error_code
        ):
            return current
        current = self._require_exact_claim(
            request=request,
            current=current,
            claim_owner=claim_owner,
            at=at,
        )
        updated = replace(
            current,
            status=IngressStatus.RETRYING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
        )
        condition, values, operation_condition, operation_values = self._claim_conditions(
            request=current,
            claim_owner=claim_owner,
            at=at,
        )
        try:
            self._replace_request_and_operation(
                previous=current,
                updated=updated,
                condition=condition,
                values=values,
                operation_condition=operation_condition,
                operation_values=operation_values,
                token_suffix=f"retry:{claim_owner}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.RETRYING
                and current.next_attempt_at == next_attempt_at
                and current.error_code == error_code
            ):
                return current
            raise
        return updated

    def _mark_accepted(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        debate_id: DebateId,
        attempt_id: AttemptId,
    ) -> IngressRequest:
        _require_utc(at)
        current = self._load_current(request)
        if (
            current is not None
            and current.status is IngressStatus.ACCEPTED
            and current.accepted_debate_id == debate_id
            and current.accepted_attempt_id == attempt_id
        ):
            return current
        current = self._require_exact_claim(
            request=request,
            current=current,
            claim_owner=claim_owner,
            at=at,
        )
        recovered = current.status_message_state in {
            StatusMessageState.STARTUP_TIMEOUT,
            StatusMessageState.RECOVERED,
        }
        updated = replace(
            current,
            status=IngressStatus.ACCEPTED,
            status_message_state=(
                StatusMessageState.RECOVERED if recovered else StatusMessageState.ACCEPTED
            ),
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=None,
            error_detail_code=None,
            accepted_debate_id=debate_id,
            accepted_attempt_id=attempt_id,
        )
        condition, values, operation_condition, operation_values = self._claim_conditions(
            request=current,
            claim_owner=claim_owner,
            at=at,
        )
        if current.processing_started_at is None:
            condition += " AND terminal_deadline_at > :at"
        try:
            status_actions = self._rearm_status_actions(
                request=updated,
                state=updated.status_message_state,
                at=at,
            )
            self._replace_request_and_operation(
                previous=current,
                updated=updated,
                condition=condition,
                values=values,
                operation_condition=operation_condition,
                operation_values=operation_values,
                extra_actions=(*self._leave_active_queue_actions(current, at), *status_actions),
                token_suffix=f"accepted:{debate_id}:{attempt_id}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.ACCEPTED
                and current.accepted_debate_id == debate_id
                and current.accepted_attempt_id == attempt_id
            ):
                return current
            raise
        return updated

    def _mark_startup_timeout(
        self,
        request: IngressRequest,
        at: datetime,
    ) -> IngressRequest:
        _require_utc(at)
        if request.status_message_state is StatusMessageState.STARTUP_TIMEOUT:
            current = self._status_work(request.interaction_id)
            if (
                current is None
                or current.publication.request_sort_key != ingress_request_sort_key(request)
                or current.request.status_message_state is not StatusMessageState.STARTUP_TIMEOUT
            ):
                raise RepositoryConflict("startup-timeout replay is stale")
            return current.request
        if not request.status.counts_toward_queue_limit:
            raise RepositoryConflict("startup timeout applies only to queued ingress")
        if request.processing_started_at is not None:
            raise RepositoryConflict("startup timeout cannot overtake started processing")
        if at < request.startup_deadline_at or at >= request.terminal_deadline_at:
            raise RepositoryConflict("startup timeout is outside its request deadline window")
        updated = replace(
            request,
            status_message_state=StatusMessageState.STARTUP_TIMEOUT,
            updated_at=at,
        )
        condition = self._request_condition(request)
        condition += " AND startup_deadline_at <= :at AND terminal_deadline_at > :at"
        values = self._expected_values(request)
        values[":at"] = _timestamp(at)
        try:
            status_actions = self._rearm_status_actions(
                request=updated,
                state=StatusMessageState.STARTUP_TIMEOUT,
                at=at,
            )
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=condition,
                values=values,
                extra_actions=status_actions,
                token_suffix=f"startup-timeout:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status_message_state is StatusMessageState.STARTUP_TIMEOUT
            ):
                return current
            raise
        return updated

    def _mark_terminal_deadline(
        self,
        request: IngressRequest,
        at: datetime,
        error_code: str,
    ) -> IngressRequest:
        _require_utc(at)
        if not error_code.strip():
            raise ValueError("terminal deadline failure requires an error code")
        if request.status.is_terminal:
            if request.status is IngressStatus.FAILED and request.error_code == error_code:
                return request
            raise RepositoryConflict("ingress request already reached another terminal state")
        if not request.status.counts_toward_queue_limit:
            raise RepositoryConflict("terminal deadline applies only to queued ingress")
        if request.processing_started_at is not None:
            raise RepositoryConflict("terminal deadline cannot overtake started processing")
        if at < request.terminal_deadline_at:
            raise RepositoryConflict("terminal deadline has not been reached")
        updated = replace(
            request,
            status=IngressStatus.FAILED,
            status_message_state=StatusMessageState.TERMINAL_FAILED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
            completed_at=at,
        )
        condition = self._request_condition(request)
        condition += (
            " AND terminal_deadline_at <= :at AND attribute_not_exists(processing_started_at)"
        )
        values = self._expected_values(request)
        values[":at"] = _timestamp(at)
        try:
            status_actions = self._rearm_status_actions(
                request=updated,
                state=StatusMessageState.TERMINAL_FAILED,
                at=at,
            )
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=condition,
                values=values,
                extra_actions=(*self._leave_active_queue_actions(request, at), *status_actions),
                token_suffix=f"terminal-deadline:{error_code}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is IngressStatus.FAILED
                and current.error_code == error_code
            ):
                return current
            if current is not None and current.processing_started_at is not None:
                raise RepositoryConflict(
                    "terminal deadline cannot overtake started processing"
                ) from None
            raise
        return updated

    def _mark_claim_terminal(
        self,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
        status: IngressStatus,
        error_code: str,
    ) -> IngressRequest:
        _require_utc(at)
        if status not in {IngressStatus.REJECTED, IngressStatus.FAILED}:
            raise ValueError("claimed terminal status must be rejected or failed")
        if not claim_owner.strip():
            raise ValueError("claim owner must not be empty")
        if not error_code.strip():
            raise ValueError("claimed terminal settlement requires an error code")
        current = self._load_current(request)
        if current is not None and current.status.is_terminal:
            if current.status is status and current.error_code == error_code:
                return current
            raise RepositoryConflict("ingress request already reached another terminal state")
        if current is None:
            raise RepositoryConflict("claimed ingress request no longer exists")
        _assert_exact_identity(request, current)
        if (
            current.status is not IngressStatus.CLAIMED
            or current.claim_owner != claim_owner
            or current.claim_expires_at is None
            or current.claim_expires_at <= at
            or current.delivery_attempt != request.delivery_attempt
            or current.claim_expires_at != request.claim_expires_at
        ):
            raise RepositoryConflict("only the exact live ingress claimant may settle")
        state = (
            StatusMessageState.REJECTED
            if status is IngressStatus.REJECTED
            else StatusMessageState.TERMINAL_FAILED
        )
        updated = replace(
            current,
            status=status,
            status_message_state=state,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
            completed_at=at,
        )
        request_values: DynamoItem = {
            ":claimed_status": IngressStatus.CLAIMED.value,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": current.schema_version,
            ":request_type": "ingress_request",
            ":interaction_id": current.interaction_id,
            ":operation_id": current.operation_id,
            ":interaction_kind": current.kind.value,
            ":created_at": _timestamp(current.created_at),
            ":owner": claim_owner,
            ":claim_expiry": _timestamp(current.claim_expires_at),
            ":at": _timestamp(at),
            ":delivery_attempt": current.delivery_attempt,
            ":message_state": current.status_message_state.value,
            ":status_channel": current.status_channel_id,
        }
        request_condition = (
            "#status=:claimed_status AND schema_version=:schema "
            "AND record_schema_version=:record_schema AND record_type=:request_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND interaction_kind=:interaction_kind AND created_at=:created_at "
            "AND claim_owner=:owner AND claim_expiry=:claim_expiry "
            "AND claim_expiry > :at AND delivery_attempt=:delivery_attempt "
            "AND status_message_state=:message_state "
            "AND status_channel_id=:status_channel"
        )
        if current.processing_started_at is None:
            request_condition += " AND attribute_not_exists(processing_started_at)"
        else:
            request_condition += " AND processing_started_at=:processing_started_at"
            request_values[":processing_started_at"] = _timestamp(current.processing_started_at)
        if current.status_message_id is None:
            request_condition += (
                " AND attribute_not_exists(status_message_id) "
                "AND attribute_not_exists(status_message_updated_at)"
            )
        else:
            if current.status_message_updated_at is None:
                raise RepositoryConflict("status message ID has no update timestamp")
            request_condition += (
                " AND status_message_id=:message_id AND status_message_updated_at=:message_updated"
            )
            request_values[":message_id"] = current.status_message_id
            request_values[":message_updated"] = _timestamp(current.status_message_updated_at)
        operation_values: DynamoItem = {
            ":claimed_status": IngressStatus.CLAIMED.value,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": current.schema_version,
            ":operation_type": "ingress_operation_result",
            ":interaction_id": current.interaction_id,
            ":operation_id": current.operation_id,
            ":request_sort_key": ingress_request_sort_key(current),
            ":created_at": _timestamp(current.created_at),
        }
        operation_condition = (
            "#status=:claimed_status AND schema_version=:schema "
            "AND record_schema_version=:record_schema AND record_type=:operation_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND request_sort_key=:request_sort_key AND created_at=:created_at"
        )
        try:
            status_actions = self._rearm_status_actions(request=updated, state=state, at=at)
            self._replace_request_and_operation(
                previous=current,
                updated=updated,
                condition=request_condition,
                values=request_values,
                operation_condition=operation_condition,
                operation_values=operation_values,
                extra_actions=(*self._leave_active_queue_actions(current, at), *status_actions),
                token_suffix=(
                    f"claim-terminal:{claim_owner}:{current.claim_expires_at}:"
                    f"{current.delivery_attempt}:{status.value}:{error_code}:{at}"
                ),
            )
        except RepositoryConflict:
            latest = self._load_current(request)
            if latest is not None and latest.status is status and latest.error_code == error_code:
                return latest
            raise
        return updated

    def _mark_terminal(
        self,
        request: IngressRequest,
        at: datetime,
        status: IngressStatus,
        error_code: str | None,
    ) -> IngressRequest:
        _require_utc(at)
        if status not in {
            IngressStatus.COMPLETED,
            IngressStatus.REJECTED,
            IngressStatus.FAILED,
        }:
            raise ValueError("terminal ingress status must be completed, rejected, or failed")
        if request.status.is_terminal:
            current = self._status_work(request.interaction_id)
            if (
                current is not None
                and current.publication.request_sort_key == ingress_request_sort_key(request)
                and current.request.status is status
                and current.request.error_code == error_code
            ):
                return current.request
            raise RepositoryConflict("ingress request already reached another terminal state")
        if status is IngressStatus.COMPLETED:
            if request.status is not IngressStatus.ACCEPTED:
                raise RepositoryConflict("only an accepted ingress request may complete")
            if error_code is not None:
                raise ValueError("completed ingress request cannot have an error code")
        elif error_code is None or not error_code.strip():
            raise ValueError("rejected and failed ingress requests require an error code")
        state_by_status = {
            IngressStatus.COMPLETED: StatusMessageState.COMPLETED,
            IngressStatus.REJECTED: StatusMessageState.REJECTED,
            IngressStatus.FAILED: StatusMessageState.TERMINAL_FAILED,
        }
        updated = replace(
            request,
            status=status,
            status_message_state=state_by_status[status],
            updated_at=at,
            next_attempt_at=None,
            claim_owner=None,
            claim_expires_at=None,
            error_code=error_code,
            completed_at=at,
        )
        queue_actions = (
            self._leave_active_queue_actions(request, at)
            if request.status.counts_toward_queue_limit
            else ()
        )
        try:
            status_actions = self._rearm_status_actions(
                request=updated,
                state=state_by_status[status],
                at=at,
            )
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=self._request_condition(request),
                values=self._expected_values(request),
                extra_actions=(*queue_actions, *status_actions),
                token_suffix=f"terminal:{status.value}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if (
                current is not None
                and current.status is status
                and current.error_code == error_code
            ):
                return current
            raise
        return updated

    def _request_status_publication(
        self,
        request: IngressRequest,
        state: StatusMessageState,
        at: datetime,
    ) -> IngressRequest:
        _require_utc(at)
        if request.status_message_state is state:
            current = self._status_work(request.interaction_id)
            if (
                current is None
                or current.publication.request_sort_key != ingress_request_sort_key(request)
                or current.request.status_message_state is not state
            ):
                raise RepositoryConflict("status publication replay is stale")
            return current.request
        updated = replace(
            request,
            status_message_state=state,
            updated_at=at,
        )
        try:
            self._replace_request_and_operation(
                previous=request,
                updated=updated,
                condition=self._request_condition(request),
                values=self._expected_values(request),
                extra_actions=self._rearm_status_actions(
                    request=updated,
                    state=state,
                    at=at,
                ),
                token_suffix=f"status-desired:{state.value}:{at}",
            )
        except RepositoryConflict:
            current = self._load_current(request)
            if current is not None and current.status_message_state is state:
                return current
            raise
        return updated

    def _list_startup_deadlines(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        return tuple(
            request
            for request in self._query_active()
            if request.startup_deadline_at <= at < request.terminal_deadline_at
            and request.processing_started_at is None
            and request.status_message_state
            not in {StatusMessageState.STARTUP_TIMEOUT, StatusMessageState.RECOVERED}
        )

    def _list_terminal_deadlines(self, at: datetime) -> tuple[IngressRequest, ...]:
        _require_utc(at)
        return tuple(
            request
            for request in self._query_active()
            if request.terminal_deadline_at <= at and request.processing_started_at is None
        )

    def _active_count(self) -> int:
        item = self._get_item(_counter_key())
        return _active_count_from_item(item)

    def _pending_status_count(self) -> int:
        item = self._get_item(_status_counter_key())
        if item is None:
            raise RepositoryConflict("status publication counter is missing")
        if _text(item, "record_type") != "ingress_status_pending_counter":
            raise RepositoryConflict("status publication counter record type is invalid")
        if _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION:
            raise RepositoryConflict("status publication counter shared schema is invalid")
        if _integer(item, "record_schema_version") != INGRESS_RECORD_SCHEMA_VERSION:
            raise RepositoryConflict("status publication counter record schema is invalid")
        count = _integer(item, "count")
        if count < 0:
            raise RepositoryConflict("status publication counter cannot be negative")
        return count

    def _query_active(self) -> tuple[IngressRequest, ...]:
        requests: list[IngressRequest] = []
        for pointer in self._query_active_pointers():
            item = self._get_item(_request_key(pointer.request_sort_key))
            if item is None:
                raise RepositoryConflict("ingress active pointer targets a missing request")
            request = deserialize_ingress_request(item)
            if (
                request.interaction_id != pointer.interaction_id
                or request.created_at != pointer.created_at
                or ingress_request_sort_key(request) != pointer.request_sort_key
            ):
                raise RepositoryConflict("ingress active pointer targets another request")
            if request.status.counts_toward_queue_limit:
                requests.append(request)
                continue

            # A terminal/accepted transaction may commit between the pointer Query and
            # this strongly consistent request Get. Only a verified pointer deletion
            # makes that race safe to skip; a residual pointer is durable corruption.
            current_pointer_item = self._get_item(_active_pointer_key(pointer.request_sort_key))
            if current_pointer_item is None:
                continue
            current_pointer = deserialize_ingress_active_pointer(current_pointer_item)
            if current_pointer != pointer:
                raise RepositoryConflict("ingress active pointer changed immutable identity")
            raise RepositoryConflict("inactive ingress request retains an active pointer")
        return tuple(requests)

    def _query_active_pointers(self) -> tuple[IngressActivePointer, ...]:
        pointers: list[IngressActivePointer] = []
        exclusive_start_key: dict[str, AttributeValueTypeDef] | None = None
        query_limit = INGRESS_QUEUE_LIMIT + 1
        while True:
            remaining = query_limit - len(pointers)
            if remaining <= 0:
                raise RepositoryConflict("ingress active pointer count exceeds twenty")
            parameters: QueryInputTypeDef = {
                "TableName": self._table_name,
                "KeyConditionExpression": "PK=:pk",
                "ExpressionAttributeValues": marshal_item(
                    {":pk": INGRESS_ACTIVE_POINTER_PARTITION}
                ),
                "ScanIndexForward": True,
                "ConsistentRead": True,
                "Limit": remaining,
            }
            if exclusive_start_key is not None:
                parameters["ExclusiveStartKey"] = exclusive_start_key
            response = self._client.query(**parameters)
            for item in response.get("Items", []):
                pointer = deserialize_ingress_active_pointer(unmarshal_item(item))
                if pointers and pointer.request_sort_key <= pointers[-1].request_sort_key:
                    raise RepositoryConflict("ingress active pointers are not strictly ordered")
                pointers.append(pointer)
                if len(pointers) > INGRESS_QUEUE_LIMIT:
                    raise RepositoryConflict("ingress active pointer count exceeds twenty")
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                return tuple(pointers)

    def _rearm_status_actions(
        self,
        *,
        request: IngressRequest,
        state: StatusMessageState,
        at: datetime,
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        publication = self._get_status_publication(request.interaction_id)
        if publication is None:
            raise RepositoryConflict("ingress request has no status publication")
        if (
            publication.request_sort_key != ingress_request_sort_key(request)
            or publication.status_channel_id != request.status_channel_id
        ):
            raise RepositoryConflict("ingress status publication identity is inconsistent")
        content = render_public_status(request, state)
        if (
            publication.desired_state is state
            and publication.content == content
            and publication.state.counts_as_pending
        ):
            return ()
        claim_is_active = publication.state is StatusPublicationState.CLAIMED
        history_required = publication.history_reconciliation_required
        updated = replace(
            publication,
            desired_state=state,
            state=(
                StatusPublicationState.CLAIMED
                if claim_is_active
                else StatusPublicationState.PREPARED
            ),
            content=content,
            content_hash=status_content_hash(content),
            updated_at=at,
            next_attempt_at=None if claim_is_active else at,
            claim_owner=publication.claim_owner if claim_is_active else None,
            claim_expires_at=publication.claim_expires_at if claim_is_active else None,
            delivery_attempt=(publication.delivery_attempt if claim_is_active else 0),
            history_reconciliation_required=(
                history_required if publication.status_message_id is None else False
            ),
            error_code=None,
        )
        actions = [
            self._put_status_publication_action(
                previous=publication,
                updated=updated,
            )
        ]
        if not publication.state.counts_as_pending:
            actions.append(self._increment_status_counter_action(at))
        return tuple(actions)

    def _put_status_publication_action(
        self,
        *,
        previous: IngressStatusPublication,
        updated: IngressStatusPublication,
        extra_condition: str | None = None,
        extra_values: Mapping[str, DynamoValue] | None = None,
    ) -> TransactWriteItemTypeDef:
        condition = (
            "publication_state=:publication_state AND desired_state=:desired_state "
            "AND updated_at=:publication_updated AND incarnation=:incarnation "
            "AND nonce=:nonce AND content_hash=:content_hash "
            "AND history_reconciliation_required=:history_required "
            "AND schema_version=:schema AND record_schema_version=:record_schema "
            "AND record_type=:publication_type "
            "AND canonical_interaction_id=:interaction_id "
            "AND request_sort_key=:request_sort_key"
        )
        checkpoint_values = _history_checkpoint_values(previous.history_checkpoint)
        for field, value in checkpoint_values.items():
            placeholder = f":expected_{field}"
            if value is None:
                condition += f" AND attribute_not_exists({field})"
            else:
                condition += f" AND {field}={placeholder}"
        if extra_condition is not None:
            condition += f" AND {extra_condition}"
        values: DynamoItem = {
            ":publication_state": previous.state.value,
            ":desired_state": previous.desired_state.value,
            ":publication_updated": _timestamp(previous.updated_at),
            ":incarnation": previous.incarnation,
            ":nonce": previous.nonce,
            ":content_hash": previous.content_hash,
            ":history_required": previous.history_reconciliation_required,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": INGRESS_STATUS_PUBLICATION_RECORD_SCHEMA_VERSION,
            ":publication_type": "ingress_status_publication",
            ":interaction_id": previous.canonical_interaction_id,
            ":request_sort_key": previous.request_sort_key,
        }
        for field, value in checkpoint_values.items():
            if value is not None:
                values[f":expected_{field}"] = value
        if extra_values is not None:
            values.update(extra_values)
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_status_publication(updated)),
                    "ConditionExpression": condition,
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )

    def _check_status_request_action(
        self,
        request: IngressRequest,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_request_key(ingress_request_sort_key(request))),
                    "ConditionExpression": self._status_request_condition(request),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        self._status_request_expected_values(request)
                    ),
                }
            },
        )

    def _put_status_request_metadata_action(
        self,
        *,
        previous: IngressRequest,
        updated: IngressRequest,
    ) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_request(updated)),
                    "ConditionExpression": self._status_request_condition(previous),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        self._status_request_expected_values(previous)
                    ),
                }
            },
        )

    def _status_request_condition(self, request: IngressRequest) -> str:
        return self._request_condition(request)

    def _status_request_expected_values(self, request: IngressRequest) -> DynamoItem:
        return self._expected_values(request)

    def _require_exact_claim(
        self,
        *,
        request: IngressRequest,
        current: IngressRequest | None,
        claim_owner: str,
        at: datetime,
    ) -> IngressRequest:
        if current is None:
            raise RepositoryConflict("claimed ingress request no longer exists")
        _assert_exact_identity(request, current)
        if (
            current.status is not IngressStatus.CLAIMED
            or current.claim_owner != claim_owner
            or current.claim_expires_at is None
            or current.claim_expires_at <= at
            or current.delivery_attempt != request.delivery_attempt
            or current.claim_expires_at != request.claim_expires_at
        ):
            raise RepositoryConflict("only the exact live ingress claimant may mutate")
        return current

    def _claim_conditions(
        self,
        *,
        request: IngressRequest,
        claim_owner: str,
        at: datetime,
    ) -> tuple[str, DynamoItem, str, DynamoItem]:
        if request.claim_expires_at is None:
            raise RepositoryConflict("claimed ingress request has no expiry")
        request_values: DynamoItem = {
            ":claimed_status": IngressStatus.CLAIMED.value,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": request.schema_version,
            ":request_type": "ingress_request",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":interaction_kind": request.kind.value,
            ":created_at": _timestamp(request.created_at),
            ":owner": claim_owner,
            ":claim_expiry": _timestamp(request.claim_expires_at),
            ":at": _timestamp(at),
            ":delivery_attempt": request.delivery_attempt,
            ":message_state": request.status_message_state.value,
            ":status_channel": request.status_channel_id,
        }
        request_condition = (
            "#status=:claimed_status AND schema_version=:schema "
            "AND record_schema_version=:record_schema AND record_type=:request_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND interaction_kind=:interaction_kind AND created_at=:created_at "
            "AND claim_owner=:owner AND claim_expiry=:claim_expiry "
            "AND claim_expiry > :at AND delivery_attempt=:delivery_attempt "
            "AND status_message_state=:message_state "
            "AND status_channel_id=:status_channel"
        )
        if request.processing_started_at is None:
            request_condition += " AND attribute_not_exists(processing_started_at)"
        else:
            request_condition += " AND processing_started_at=:processing_started_at"
            request_values[":processing_started_at"] = _timestamp(request.processing_started_at)
        if request.status_message_id is None:
            request_condition += (
                " AND attribute_not_exists(status_message_id) "
                "AND attribute_not_exists(status_message_updated_at)"
            )
        else:
            if request.status_message_updated_at is None:
                raise RepositoryConflict("status message ID has no update timestamp")
            request_condition += (
                " AND status_message_id=:message_id AND status_message_updated_at=:message_updated"
            )
            request_values[":message_id"] = request.status_message_id
            request_values[":message_updated"] = _timestamp(request.status_message_updated_at)
        operation_values: DynamoItem = {
            ":claimed_status": IngressStatus.CLAIMED.value,
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": request.schema_version,
            ":operation_type": "ingress_operation_result",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":request_sort_key": ingress_request_sort_key(request),
            ":created_at": _timestamp(request.created_at),
        }
        operation_condition = (
            "#status=:claimed_status AND schema_version=:schema "
            "AND record_schema_version=:record_schema AND record_type=:operation_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND request_sort_key=:request_sort_key AND created_at=:created_at"
        )
        return request_condition, request_values, operation_condition, operation_values

    def _replace_request_and_operation(
        self,
        *,
        previous: IngressRequest,
        updated: IngressRequest,
        condition: str,
        values: Mapping[str, DynamoValue],
        token_suffix: str,
        extra_actions: Iterable[TransactWriteItemTypeDef] = (),
        operation_condition: str | None = None,
        operation_values: Mapping[str, DynamoValue] | None = None,
    ) -> None:
        request_actions = self._replace_request_and_operation_actions(
            previous=previous,
            updated=updated,
            condition=condition,
            values=values,
            operation_condition=operation_condition,
            operation_values=operation_values,
        )
        token_source = f"{self._table_name}:{previous.operation_id}:{token_suffix}"
        self._transact(
            (*request_actions, *extra_actions),
            token=_client_token(token_source),
        )

    def _replace_request_and_operation_actions(
        self,
        *,
        previous: IngressRequest,
        updated: IngressRequest,
        condition: str,
        values: Mapping[str, DynamoValue],
        operation_condition: str | None = None,
        operation_values: Mapping[str, DynamoValue] | None = None,
    ) -> tuple[TransactWriteItemTypeDef, TransactWriteItemTypeDef]:
        operation = _operation_for_request(updated)
        request_action = cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_request(updated)),
                    "ConditionExpression": condition,
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(values),
                }
            },
        )
        operation_action = cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(serialize_ingress_operation_result(operation)),
                    "ConditionExpression": operation_condition
                    or (
                        "#status=:expected_status AND updated_at=:expected_updated "
                        "AND schema_version=:schema AND record_schema_version=:record_schema "
                        "AND record_type=:operation_type "
                        "AND interaction_id=:interaction_id AND operation_id=:operation_id "
                        "AND request_sort_key=:request_sort_key AND created_at=:created_at"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": marshal_item(
                        operation_values or self._operation_expected_values(previous)
                    ),
                }
            },
        )
        return request_action, operation_action

    def _request_condition(self, request: IngressRequest) -> str:
        condition = (
            "#status=:expected_status AND updated_at=:expected_updated "
            "AND schema_version=:schema AND record_schema_version=:record_schema "
            "AND record_type=:request_type "
            "AND interaction_id=:interaction_id AND operation_id=:operation_id "
            "AND created_at=:created_at AND startup_deadline_at=:startup_deadline "
            "AND terminal_deadline_at=:terminal_deadline "
            "AND status_message_state=:expected_message_state "
            "AND status_channel_id=:expected_status_channel"
        )
        if request.processing_started_at is None:
            condition += " AND attribute_not_exists(processing_started_at)"
        else:
            condition += " AND processing_started_at=:expected_processing_started"
        if request.status_message_id is None:
            condition += (
                " AND attribute_not_exists(status_message_id) "
                "AND attribute_not_exists(status_message_updated_at)"
            )
        else:
            condition += (
                " AND status_message_id=:expected_message_id "
                "AND status_message_updated_at=:expected_message_updated"
            )
        return condition

    def _expected_values(self, request: IngressRequest) -> DynamoItem:
        values: DynamoItem = {
            ":expected_status": request.status.value,
            ":expected_updated": _timestamp(request.updated_at),
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
            ":request_type": "ingress_request",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":created_at": _timestamp(request.created_at),
            ":startup_deadline": _timestamp(request.startup_deadline_at),
            ":terminal_deadline": _timestamp(request.terminal_deadline_at),
            ":expected_message_state": request.status_message_state.value,
            ":expected_status_channel": request.status_channel_id,
        }
        if request.status_message_id is not None:
            if request.status_message_updated_at is None:
                raise RepositoryConflict("status message ID has no update timestamp")
            values[":expected_message_id"] = request.status_message_id
            values[":expected_message_updated"] = _timestamp(request.status_message_updated_at)
        if request.processing_started_at is not None:
            values[":expected_processing_started"] = _timestamp(request.processing_started_at)
        return values

    def _operation_expected_values(self, request: IngressRequest) -> DynamoItem:
        return {
            ":expected_status": request.status.value,
            ":expected_updated": _timestamp(request.updated_at),
            ":schema": CURRENT_SCHEMA_VERSION,
            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
            ":operation_type": "ingress_operation_result",
            ":interaction_id": request.interaction_id,
            ":operation_id": request.operation_id,
            ":request_sort_key": ingress_request_sort_key(request),
            ":created_at": _timestamp(request.created_at),
        }

    def _increment_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_counter_key()),
                    "UpdateExpression": "SET #count=#count+:one, updated_at=:at",
                    "ConditionExpression": (
                        "#count >= :zero AND #count < :limit AND record_type=:type "
                        "AND schema_version=:schema "
                        "AND record_schema_version=:record_schema"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":limit": INGRESS_QUEUE_LIMIT,
                            ":type": "ingress_queue_counter",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _increment_status_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_status_counter_key()),
                    "UpdateExpression": "SET #count=#count+:one, updated_at=:at",
                    "ConditionExpression": (
                        "#count >= :zero AND record_type=:type "
                        "AND schema_version=:schema "
                        "AND record_schema_version=:record_schema"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":type": "ingress_status_pending_counter",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _decrement_status_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_status_counter_key()),
                    "UpdateExpression": "SET #count=#count-:one, updated_at=:at",
                    "ConditionExpression": (
                        "#count > :zero AND record_type=:type "
                        "AND schema_version=:schema "
                        "AND record_schema_version=:record_schema"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":type": "ingress_status_pending_counter",
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _leave_active_queue_actions(
        self,
        request: IngressRequest,
        at: datetime,
    ) -> tuple[TransactWriteItemTypeDef, TransactWriteItemTypeDef]:
        if not request.status.counts_toward_queue_limit:
            raise RepositoryConflict("only queued ingress may leave the active pointer set")
        return (
            self._decrement_counter_action(at),
            self._delete_active_pointer_action(request),
        )

    def _delete_active_pointer_action(
        self,
        request: IngressRequest,
    ) -> TransactWriteItemTypeDef:
        pointer = serialize_ingress_active_pointer(request)
        return cast(
            TransactWriteItemTypeDef,
            {
                "Delete": {
                    "TableName": self._table_name,
                    "Key": marshal_item(
                        {
                            "PK": INGRESS_ACTIVE_POINTER_PARTITION,
                            "SK": ingress_request_sort_key(request),
                        }
                    ),
                    "ConditionExpression": (
                        "record_type=:type AND schema_version=:schema "
                        "AND record_schema_version=:record_schema "
                        "AND interaction_id=:interaction_id "
                        "AND request_sort_key=:request_sort_key AND created_at=:created_at"
                    ),
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":type": pointer["record_type"],
                            ":schema": pointer["schema_version"],
                            ":record_schema": pointer["record_schema_version"],
                            ":interaction_id": pointer["interaction_id"],
                            ":request_sort_key": pointer["request_sort_key"],
                            ":created_at": pointer["created_at"],
                        }
                    ),
                }
            },
        )

    def _decrement_counter_action(self, at: datetime) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": marshal_item(_counter_key()),
                    "UpdateExpression": "SET #count=#count-:one, updated_at=:at",
                    "ConditionExpression": (
                        "#count > :zero AND #count <= :limit AND schema_version=:schema "
                        "AND record_schema_version=:record_schema AND record_type=:type"
                    ),
                    "ExpressionAttributeNames": {"#count": "count"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":zero": 0,
                            ":one": 1,
                            ":limit": INGRESS_QUEUE_LIMIT,
                            ":schema": CURRENT_SCHEMA_VERSION,
                            ":record_schema": INGRESS_RECORD_SCHEMA_VERSION,
                            ":type": "ingress_queue_counter",
                            ":at": _timestamp(at),
                        }
                    ),
                }
            },
        )

    def _put_new(self, item: DynamoItem) -> TransactWriteItemTypeDef:
        return cast(
            TransactWriteItemTypeDef,
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(item),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            },
        )

    def _transact(
        self,
        actions: Iterable[TransactWriteItemTypeDef],
        *,
        token: str,
        aggregate_write_floor: int | None = None,
    ) -> bool:
        action_list = list(actions)
        if not 1 <= len(action_list) <= 99:
            raise ValueError("DynamoDB transaction must contain between 1 and 100 actions")
        original_action_count = len(action_list)
        write_floor = (
            original_action_count if aggregate_write_floor is None else aggregate_write_floor
        )
        if not 1 <= write_floor <= original_action_count:
            raise ValueError("aggregate write floor must cover one to all transaction actions")
        action_list.insert(0, deployment_lock_open_check(table_name=self._table_name))
        try:
            response = self._client.transact_write_items(
                TransactItems=action_list,
                ClientRequestToken=token[:36],
                ReturnConsumedCapacity="TOTAL",
            )
        except self._client.exceptions.TransactionCanceledException as error:
            if is_condition_only_cancellation(error):
                raise RepositoryConflict("DynamoDB ingress transaction condition failed") from None
            raise RepositoryUnavailable from None
        except self._client.exceptions.IdempotentParameterMismatchException:
            raise RepositoryConflict("ingress transaction token input changed") from None
        capacities = response.get("ConsumedCapacity", [])
        write_units = sum(capacity.get("WriteCapacityUnits", 0) for capacity in capacities)
        read_units = sum(capacity.get("ReadCapacityUnits", 0) for capacity in capacities)
        if write_units > 0:
            return True
        if read_units > 0:
            return False
        # DynamoDB Local reports only aggregate units and currently omits the
        # immutable active-pointer write from enqueue's aggregate total. The
        # caller supplies that known local floor; an idempotent replay reports
        # only the cheaper reads described by the TransactWriteItems API.
        total_units = sum(capacity.get("CapacityUnits", 0) for capacity in capacities)
        return not capacities or total_units >= write_floor

    def _load_current(self, request: IngressRequest) -> IngressRequest | None:
        return self._load_request(ingress_request_sort_key(request))

    def _load_request(self, request_sort_key: str) -> IngressRequest | None:
        item = self._get_item(_request_key(request_sort_key))
        return None if item is None else deserialize_ingress_request(item)

    def _get_item(self, key: Mapping[str, DynamoValue]) -> DynamoItem | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(key),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        return None if raw is None else unmarshal_item(raw)

    def _transact_get_items(
        self,
        keys: tuple[DynamoItem, ...],
    ) -> tuple[DynamoItem | None, ...]:
        actions = [
            cast(
                TransactGetItemTypeDef,
                {
                    "Get": {
                        "TableName": self._table_name,
                        "Key": marshal_item(key),
                    }
                },
            )
            for key in keys
        ]
        response = self._client.transact_get_items(
            TransactItems=actions,
            ReturnConsumedCapacity="NONE",
        )
        raw_responses = response.get("Responses")
        if raw_responses is None or len(raw_responses) != len(keys):
            raise RepositoryConflict("DynamoDB replay bundle response is incomplete")
        items: list[DynamoItem | None] = []
        for raw_response in raw_responses:
            raw_item = raw_response.get("Item")
            items.append(None if raw_item is None else unmarshal_item(raw_item))
        return tuple(items)


def _operation_for_request(request: IngressRequest) -> IngressOperationResult:
    return IngressOperationResult(
        operation_id=request.operation_id,
        interaction_id=request.interaction_id,
        request_sort_key=ingress_request_sort_key(request),
        status=request.status,
        created_at=request.created_at,
        updated_at=request.updated_at,
        accepted_debate_id=request.accepted_debate_id,
        accepted_attempt_id=request.accepted_attempt_id,
        error_code=request.error_code,
    )


def _terminal_ingress_state(
    snapshot: DebateSnapshot,
) -> tuple[IngressStatus, StatusMessageState, str | None]:
    phase = snapshot.state.phase
    if phase is DebatePhase.COMPLETED:
        return IngressStatus.COMPLETED, StatusMessageState.COMPLETED, None
    if phase is DebatePhase.CANCELLED:
        return IngressStatus.COMPLETED, StatusMessageState.CANCELLED, None
    if phase is DebatePhase.FAILED:
        if snapshot.error_code is None:
            raise RepositoryConflict("failed debate origin has no error code")
        return IngressStatus.FAILED, StatusMessageState.TERMINAL_FAILED, snapshot.error_code
    raise RepositoryConflict("only a terminal debate may settle its origin ingress")


def _binding_for_request(request: IngressRequest) -> IngressSemanticOperationBinding:
    if request.kind is IngressKind.NEW_DEBATE:
        raise ValueError("new debate has no semantic component-operation binding")
    return IngressSemanticOperationBinding(
        operation_id=request.operation_id,
        canonical_interaction_id=request.interaction_id,
        request_sort_key=ingress_request_sort_key(request),
        created_at=request.created_at,
    )


def _validate_binding(
    binding: IngressSemanticOperationBinding,
    operation: IngressOperationResult,
) -> None:
    if operation.interaction_id != binding.canonical_interaction_id:
        raise RepositoryConflict("semantic operation binding points to another interaction")
    if operation.operation_id != binding.operation_id:
        raise RepositoryConflict("semantic operation binding points to another operation")
    if operation.request_sort_key != binding.request_sort_key:
        raise RepositoryConflict("semantic operation binding points to another request")
    if operation.created_at != binding.created_at:
        raise RepositoryConflict("semantic operation binding has another creation timestamp")


def _operation_key(interaction_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_OPERATION#{interaction_id}", "SK": "RESULT"}


def _semantic_binding_key(operation_id: str) -> DynamoItem:
    return {"PK": f"INGRESS_SEMANTIC_OPERATION#{operation_id}", "SK": "BINDING"}


def _status_publication_key(canonical_interaction_id: str) -> DynamoItem:
    return {
        "PK": f"INGRESS_OPERATION#{canonical_interaction_id}",
        "SK": "STATUS_PUBLICATION",
    }


def _history_checkpoint_values(
    checkpoint: StatusHistoryCheckpoint | None,
) -> dict[str, str | None]:
    return {
        "history_cursor_message_id": (
            checkpoint.history_cursor_message_id if checkpoint is not None else None
        ),
        "history_verified_head_message_id": (
            checkpoint.history_verified_head_message_id if checkpoint is not None else None
        ),
        "history_gap_cursor_message_id": (
            checkpoint.history_gap_cursor_message_id if checkpoint is not None else None
        ),
        "history_gap_upper_message_id": (
            checkpoint.history_gap_upper_message_id if checkpoint is not None else None
        ),
    }


def _history_checkpoint_token(checkpoint: StatusHistoryCheckpoint | None) -> str:
    values = _history_checkpoint_values(checkpoint)
    return ":".join(values[field] or "-" for field in sorted(values))


def _validate_history_checkpoint_progress(
    previous: StatusHistoryCheckpoint | None,
    updated: StatusHistoryCheckpoint,
) -> None:
    if previous is None:
        return
    previous_head = int(previous.history_verified_head_message_id)
    updated_head = int(updated.history_verified_head_message_id)
    if updated_head < previous_head:
        raise ValueError("status history verified head cannot move backwards")
    previous_cursor = previous.history_cursor_message_id
    updated_cursor = updated.history_cursor_message_id
    if previous_cursor is None and updated_cursor is not None:
        raise ValueError("completed status history baseline cannot be reopened")
    if (
        previous_cursor is not None
        and updated_cursor is not None
        and int(updated_cursor) > int(previous_cursor)
    ):
        raise ValueError("status history cursor cannot move towards newer messages")
    previous_gap_cursor = previous.history_gap_cursor_message_id
    previous_gap_upper = previous.history_gap_upper_message_id
    if previous_gap_cursor is None or previous_gap_upper is None:
        return
    if updated_head == previous_head:
        if (
            updated.history_gap_cursor_message_id is None
            or updated.history_gap_upper_message_id != previous_gap_upper
        ):
            raise ValueError("active status history gap must be resumed or completed")
        if int(updated.history_gap_cursor_message_id) > int(previous_gap_cursor):
            raise ValueError("status history gap cursor cannot move towards newer messages")
        return
    if updated_head < int(previous_gap_upper):
        raise ValueError("status history verified head cannot skip an active gap")


def _request_key(request_sort_key: str) -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": request_sort_key}


def _active_pointer_key(request_sort_key: str) -> DynamoItem:
    return {"PK": INGRESS_ACTIVE_POINTER_PARTITION, "SK": request_sort_key}


def _counter_key() -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": "COUNTER"}


def _status_counter_key() -> DynamoItem:
    return {"PK": "CONTROL#INGRESS", "SK": "STATUS_PENDING_COUNTER"}


def _active_count_from_item(item: DynamoItem | None) -> int:
    if item is None:
        raise RepositoryConflict("ingress counter is missing")
    if _text(item, "record_type") != "ingress_queue_counter":
        raise RepositoryConflict("ingress counter record type is invalid")
    if _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION:
        raise RepositoryConflict("ingress counter shared schema version is invalid")
    if _integer(item, "record_schema_version") != INGRESS_RECORD_SCHEMA_VERSION:
        raise RepositoryConflict("ingress counter record schema version is invalid")
    count = _integer(item, "count")
    if not 0 <= count <= INGRESS_QUEUE_LIMIT:
        raise RepositoryConflict("ingress counter is outside its valid range")
    return count


def _assert_exact_identity(incoming: IngressRequest, persisted: IngressRequest) -> None:
    if incoming.interaction_id != persisted.interaction_id:
        raise RepositoryIdentityConflict("ingress replay belongs to another interaction")
    _assert_common_identity(incoming, persisted, include_display_names=True)


def _assert_semantic_identity(incoming: IngressRequest, persisted: IngressRequest) -> None:
    if incoming.kind is IngressKind.NEW_DEBATE or persisted.kind is IngressKind.NEW_DEBATE:
        raise RepositoryIdentityConflict("semantic replay is only valid for component operations")
    _assert_common_identity(incoming, persisted, include_display_names=False)


def _assert_common_identity(
    incoming: IngressRequest,
    persisted: IngressRequest,
    *,
    include_display_names: bool,
) -> None:
    incoming_identity = (
        incoming.operation_id,
        incoming.kind,
        incoming.application_id,
        incoming.requester_id,
        incoming.requester_can_manage_messages,
        incoming.guild_id,
        incoming.channel_id,
        incoming.parent_channel_id,
        incoming.status_channel_id,
        incoming.command_name,
        incoming.custom_id,
        incoming.question,
        incoming.source_message_id,
        incoming.source_thread_id,
        incoming.target_debate_id,
        incoming.expected_attempt_id,
    )
    persisted_identity = (
        persisted.operation_id,
        persisted.kind,
        persisted.application_id,
        persisted.requester_id,
        persisted.requester_can_manage_messages,
        persisted.guild_id,
        persisted.channel_id,
        persisted.parent_channel_id,
        persisted.status_channel_id,
        persisted.command_name,
        persisted.custom_id,
        persisted.question,
        persisted.source_message_id,
        persisted.source_thread_id,
        persisted.target_debate_id,
        persisted.expected_attempt_id,
    )
    if incoming_identity != persisted_identity:
        raise RepositoryIdentityConflict("ingress replay immutable identity changed")
    if include_display_names and (
        incoming.requester_username != persisted.requester_username
        or incoming.requester_display_name != persisted.requester_display_name
    ):
        raise RepositoryIdentityConflict("ingress replay requester name snapshot changed")


def _is_ready(request: IngressRequest, at: datetime) -> bool:
    if at >= request.terminal_deadline_at and request.processing_started_at is None:
        return False
    if request.status is IngressStatus.PENDING:
        return True
    if request.status is IngressStatus.RETRYING:
        return request.next_attempt_at is None or request.next_attempt_at <= at
    return (
        request.status is IngressStatus.CLAIMED
        and request.claim_expires_at is not None
        and request.claim_expires_at <= at
    )


def _status_publication_is_due(
    publication: IngressStatusPublication,
    at: datetime,
) -> bool:
    if publication.state in {
        StatusPublicationState.PREPARED,
        StatusPublicationState.RETRYING,
    }:
        return publication.next_attempt_at is not None and publication.next_attempt_at <= at
    return (
        publication.state is StatusPublicationState.CLAIMED
        and publication.claim_expires_at is not None
        and publication.claim_expires_at <= at
    )


def _status_due_condition(publication: IngressStatusPublication) -> str:
    if publication.state in {
        StatusPublicationState.PREPARED,
        StatusPublicationState.RETRYING,
    }:
        return "next_attempt_at <= :claim_at"
    if publication.state is StatusPublicationState.CLAIMED:
        return "claim_expiry <= :claim_at"
    raise ValueError("only a due status publication can be claimed")


def _require_status_claim(
    publication: IngressStatusPublication,
    claim_owner: str,
) -> None:
    if not claim_owner.strip():
        raise ValueError("status claim owner must not be empty")
    if (
        publication.state is not StatusPublicationState.CLAIMED
        or publication.claim_owner != claim_owner
    ):
        raise RepositoryConflict("only the current status claimant may settle publication")


def _client_token(value: str) -> str:
    return f"tx-{hashlib.sha256(value.encode()).hexdigest()[:30]}"


def _timestamp(value: datetime) -> str:
    _require_utc(value)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")


def _text(item: Mapping[str, DynamoValue], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value


def _integer(item: Mapping[str, DynamoValue], field: str) -> int:
    value = item.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepositoryConflict(f"{field} is missing or invalid")
    return value
