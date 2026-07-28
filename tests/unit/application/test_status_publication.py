"""Pure status rendering and publisher orchestration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from shittim_chest.application.ports import Clock, StatusPublicationRepository
from shittim_chest.application.scale_to_zero import (
    STATUS_PUBLICATION_CLAIM_SECONDS,
    IngressRequest,
    IngressStatusPublication,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationState,
    StatusPublicationWork,
    status_publication_nonce,
)
from shittim_chest.application.status_publication import (
    STATUS_MAX_DELIVERY_ATTEMPTS,
    DiscordStatusGateway,
    DiscordStatusMessage,
    PublicStatusPublisher,
    StatusDeliveryError,
    StatusDeliveryErrorCode,
    StatusHistoryProgress,
    StatusMessageMissing,
    StatusPublicationOutcome,
    StatusWriteAmbiguous,
    has_exact_status_publication_marker,
    render_public_status,
    sanitize_status_text,
    status_content_hash,
    status_publication_marker,
)

NOW = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)


class FixedClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        value = self.current
        self.current += timedelta(microseconds=1)
        return value


class FakeStatusRepository:
    def __init__(self, work: StatusPublicationWork) -> None:
        self.work = work
        self.delivered: list[str] = []
        self.rescheduled: list[datetime] = []
        self.failed: list[str] = []
        self.claims = 0
        self.rearm_state_on_delivery: StatusMessageState | None = None

    async def claim_status_publication(
        self,
        *,
        interaction_id: str,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork | None:
        publication = self.work.publication
        due = (
            publication.state in {StatusPublicationState.PREPARED, StatusPublicationState.RETRYING}
            and publication.next_attempt_at is not None
            and publication.next_attempt_at <= at
        )
        if interaction_id != publication.canonical_interaction_id or not due:
            return None
        self.claims += 1
        claimed = replace(
            publication,
            state=StatusPublicationState.CLAIMED,
            updated_at=at,
            next_attempt_at=None,
            claim_owner=claim_owner,
            claim_expires_at=at + timedelta(seconds=STATUS_PUBLICATION_CLAIM_SECONDS),
            delivery_attempt=publication.delivery_attempt + 1,
            history_reconciliation_required=publication.history_reconciliation_required,
        )
        self.work = StatusPublicationWork(request=self.work.request, publication=claimed)
        return self.work

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
        assert work.publication.claim_owner == claim_owner
        self.rescheduled.append(next_attempt_at)
        unbound_message_may_exist = message_may_exist and work.publication.status_message_id is None
        publication = replace(
            work.publication,
            state=StatusPublicationState.RETRYING,
            updated_at=at,
            next_attempt_at=next_attempt_at,
            claim_owner=None,
            claim_expires_at=None,
            delivery_attempt=(
                0
                if (
                    history_checkpoint is not None
                    and history_checkpoint != work.publication.history_checkpoint
                )
                else work.publication.delivery_attempt
            ),
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
        self.work = StatusPublicationWork(request=work.request, publication=publication)
        return publication

    async def mark_status_delivered(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        message_id: str,
        at: datetime,
    ) -> IngressStatusPublication:
        assert work.publication.claim_owner == claim_owner
        self.delivered.append(message_id)
        if self.rearm_state_on_delivery is not None:
            desired_state = self.rearm_state_on_delivery
            self.rearm_state_on_delivery = None
            content = render_public_status(work.request, desired_state)
            request = replace(
                work.request,
                status_message_state=desired_state,
                status_message_id=message_id,
                status_message_updated_at=at,
                updated_at=at,
            )
            publication = replace(
                work.publication,
                desired_state=desired_state,
                delivered_state=work.publication.desired_state,
                state=StatusPublicationState.PREPARED,
                content=content,
                content_hash=status_content_hash(content),
                status_message_id=message_id,
                status_message_updated_at=at,
                updated_at=at,
                next_attempt_at=at,
                claim_owner=None,
                claim_expires_at=None,
                delivery_attempt=0,
                history_checkpoint=None,
                history_reconciliation_required=False,
            )
            self.work = StatusPublicationWork(request=request, publication=publication)
            return publication
        publication = replace(
            work.publication,
            state=StatusPublicationState.DELIVERED,
            delivered_state=work.publication.desired_state,
            status_message_id=message_id,
            status_message_updated_at=at,
            updated_at=at,
            claim_owner=None,
            claim_expires_at=None,
            history_checkpoint=None,
            history_reconciliation_required=False,
        )
        request = replace(
            work.request,
            status_message_id=message_id,
            status_message_updated_at=at,
        )
        self.work = StatusPublicationWork(request=request, publication=publication)
        return publication

    async def mark_status_failed(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
        error_code: str,
        message_may_exist: bool = False,
    ) -> IngressStatusPublication:
        assert work.publication.claim_owner == claim_owner
        self.failed.append(error_code)
        unbound_message_may_exist = message_may_exist and work.publication.status_message_id is None
        publication = replace(
            work.publication,
            state=StatusPublicationState.FAILED,
            updated_at=at,
            claim_owner=None,
            claim_expires_at=None,
            history_reconciliation_required=(
                work.publication.history_reconciliation_required or unbound_message_may_exist
            ),
            error_code=error_code,
        )
        self.work = StatusPublicationWork(request=work.request, publication=publication)
        return publication

    async def replace_missing_status_message(
        self,
        *,
        work: StatusPublicationWork,
        claim_owner: str,
        at: datetime,
    ) -> StatusPublicationWork:
        assert work.publication.claim_owner == claim_owner
        incarnation = work.publication.incarnation + 1
        publication = replace(
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
        )
        request = replace(work.request, status_message_id=None, status_message_updated_at=None)
        self.work = StatusPublicationWork(request=request, publication=publication)
        return self.work

    async def list_due_status_publications(
        self,
        *,
        at: datetime,
        limit: int,
    ) -> tuple[IngressStatusPublication, ...]:
        del at, limit
        return (self.work.publication,)


class FakeGateway:
    def __init__(self, publication: IngressStatusPublication, application_id: str) -> None:
        self.publication = publication
        self.application_id = application_id
        self.created = 0
        self.edited = 0
        self.fetched = 0
        self.searched = 0
        self.fetch_result: DiscordStatusMessage | Exception | None = None
        self.search_result: DiscordStatusMessage | None = None
        self.search_error: StatusDeliveryError | None = None
        self.create_error: StatusDeliveryError | None = None
        self.create_result: DiscordStatusMessage | None = None
        self.create_content: str | None = None
        self.edit_error: StatusDeliveryError | None = None

    async def current_bot_user_id(self) -> str:
        return self.application_id

    def message(
        self, *, content: str | None = None, nonce: str | None = None
    ) -> DiscordStatusMessage:
        return DiscordStatusMessage(
            message_id="500",
            channel_id=self.publication.status_channel_id,
            author_id=self.application_id,
            content=self.publication.content if content is None else content,
            nonce=self.publication.nonce if nonce is None else nonce,
        )

    async def fetch_message(self, *, channel_id: str, message_id: str) -> DiscordStatusMessage:
        del channel_id, message_id
        self.fetched += 1
        if isinstance(self.fetch_result, Exception):
            raise self.fetch_result
        if self.fetch_result is None:
            return self.message()
        return self.fetch_result

    async def find_by_nonce(
        self,
        *,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        after_message_id: str,
        checkpoint: StatusHistoryCheckpoint | None,
    ) -> DiscordStatusMessage | None:
        del (
            channel_id,
            author_id,
            nonce,
            operation_marker,
            after_message_id,
            checkpoint,
        )
        self.searched += 1
        if self.search_error is not None:
            raise self.search_error
        return self.search_result

    async def create_message(
        self,
        *,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> DiscordStatusMessage:
        self.created += 1
        if self.create_error is not None:
            raise self.create_error
        if self.create_result is not None:
            return self.create_result
        return DiscordStatusMessage(
            message_id="500",
            channel_id=channel_id,
            author_id=self.application_id,
            content=content if self.create_content is None else self.create_content,
            nonce=nonce,
        )

    async def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> DiscordStatusMessage:
        del channel_id, message_id
        self.edited += 1
        if self.edit_error is not None:
            raise self.edit_error
        return self.message(content=content)


def request() -> IngressRequest:
    return IngressRequest.new_debate(
        interaction_id="300",
        operation_id="300",
        application_id="200",
        question="甘い朝食は **何** がいい? @everyone\r\n\u202eabc",
        requester_id="400",
        requester_username="requester",
        requester_display_name="_<@123>_\nRequester",
        guild_id="100",
        channel_id="101",
        command_name="shittim",
        created_at=NOW,
    )


def work() -> StatusPublicationWork:
    source = request()
    publication = IngressStatusPublication.prepared(
        source,
        content=render_public_status(source, StatusMessageState.STARTING),
    )
    return StatusPublicationWork(request=source, publication=publication)


@pytest.mark.parametrize("state", tuple(StatusMessageState))
def test_renderer_is_bounded_and_explicit_for_every_state(state: StatusMessageState) -> None:
    content = render_public_status(request(), state)

    assert content.startswith(f"状態: {state.name} (")
    assert len(content) <= 2_000
    assert "@everyone" not in content
    assert "<@123>" not in content
    assert "\u202e" not in content
    assert "\r" not in content
    assert "\nRequester" not in content
    assert "受付時刻: <t:" in content
    assert status_publication_marker(request().interaction_id) in content


def test_timeout_and_recovery_guidance_matches_the_canonical_user_actions() -> None:
    startup_timeout = render_public_status(request(), StatusMessageState.STARTUP_TIMEOUT)
    recovered = render_public_status(request(), StatusMessageState.RECOVERED)
    terminal = render_public_status(request(), StatusMessageState.TERMINAL_FAILED)

    assert "依頼は保存済み" in startup_timeout
    assert "自動復旧を継続中" in startup_timeout
    assert "再実行は不要" in startup_timeout
    assert "復旧しました" in recovered
    assert "議論を開始します" in recovered
    assert "依頼を再実行してください" in terminal


def test_sanitizer_truncates_after_neutralizing_controls_and_markdown() -> None:
    result = sanitize_status_text("**@all**\n\u2066abc", limit=12)

    assert len(result) <= 12
    assert "@all" not in result
    assert "\u2066" not in result
    assert result.endswith("…")


def test_operation_marker_matches_only_the_dedicated_final_line() -> None:
    marker = status_publication_marker(request().interaction_id)

    assert has_exact_status_publication_marker(f"状態本文\n識別子: {marker}", marker)
    assert not has_exact_status_publication_marker(
        f"議題へ埋め込んだ {marker}\n識別子: sc-ffffffffffffffffffff",
        marker,
    )


@pytest.mark.asyncio
async def test_fresh_publication_creates_once_and_persists_message_id() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.DELIVERED
    assert gateway.created == 1
    assert repository.delivered == ["500"]


@pytest.mark.asyncio
async def test_created_message_with_unexpected_content_is_bound_by_history_before_edit() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    gateway.create_content = "provider returned different content"
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert gateway.created == 1
    assert gateway.edited == 0
    assert repository.work.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_state_change_during_delivery_converges_without_duplicate_message() -> None:
    repository = FakeStatusRepository(work())
    repository.rearm_state_on_delivery = StatusMessageState.STARTUP_TIMEOUT
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.DELIVERED
    assert gateway.created == 1
    assert gateway.edited == 1
    assert repository.work.publication.state is StatusPublicationState.DELIVERED
    assert repository.work.publication.delivered_state is StatusMessageState.STARTUP_TIMEOUT


@pytest.mark.asyncio
async def test_delivered_duplicate_does_not_read_token_or_touch_discord() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.DELIVERED,
        delivered_state=StatusMessageState.STARTING,
        status_message_id="500",
        status_message_updated_at=NOW,
        next_attempt_at=None,
    )
    request_with_message = replace(
        source.request,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )
    factory_calls = 0

    async def unexpected_factory(request: IngressRequest) -> DiscordStatusGateway:
        del request
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("settled publication must not read the Bot token")

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=unexpected_factory,
    )

    assert outcome is StatusPublicationOutcome.NO_WORK
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_retry_reconciles_existing_nonce_without_duplicate_create() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=1,
        history_reconciliation_required=True,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.search_result = gateway.message()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.DELIVERED
    assert gateway.searched == 1
    assert gateway.created == 0


@pytest.mark.asyncio
async def test_exhausted_ambiguous_history_never_authorizes_another_create() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=1,
        history_reconciliation_required=True,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert repository.claims == 1
    assert gateway.searched == 1
    assert gateway.created == 0
    assert repository.work.publication.history_checkpoint is None
    assert repository.work.publication.history_reconciliation_required
    assert repository.work.publication.error_code == (
        StatusDeliveryErrorCode.MESSAGE_AMBIGUOUS.value
    )


@pytest.mark.asyncio
async def test_exhausted_ambiguous_history_fails_closed_at_the_attempt_limit() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=STATUS_MAX_DELIVERY_ATTEMPTS - 1,
        history_reconciliation_required=True,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert gateway.searched == 1
    assert gateway.created == 0
    assert repository.failed == [StatusDeliveryErrorCode.MESSAGE_AMBIGUOUS.value]


@pytest.mark.asyncio
async def test_new_ambiguous_create_on_final_attempt_is_persisted_before_failure() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=STATUS_MAX_DELIVERY_ATTEMPTS - 1,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.create_error = StatusWriteAmbiguous()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert gateway.created == 1
    assert repository.work.publication.delivery_attempt == STATUS_MAX_DELIVERY_ATTEMPTS
    assert repository.work.publication.history_reconciliation_required
    assert repository.failed == [StatusDeliveryErrorCode.UNAVAILABLE.value]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel_id", "102"),
        ("author_id", "201"),
        ("nonce", "different-nonce"),
    ],
)
async def test_untrusted_create_response_identity_is_ambiguous_on_final_attempt(
    field: str,
    value: str,
) -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=STATUS_MAX_DELIVERY_ATTEMPTS - 1,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.create_result = replace(gateway.message(), **{field: value})
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert gateway.created == 1
    assert repository.work.publication.history_reconciliation_required
    assert repository.failed == [StatusDeliveryErrorCode.CONFLICT.value]


@pytest.mark.asyncio
async def test_history_progress_persists_cursor_and_exact_retry_after() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=STATUS_MAX_DELIVERY_ATTEMPTS - 1,
        history_reconciliation_required=True,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.search_error = StatusHistoryProgress(
        StatusDeliveryErrorCode.RATE_LIMITED,
        checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="500",
            history_verified_head_message_id="600",
        ),
        retry_after_seconds=900,
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert repository.work.publication.history_checkpoint == StatusHistoryCheckpoint(
        history_cursor_message_id="500",
        history_verified_head_message_id="600",
    )
    assert repository.work.publication.history_reconciliation_required
    assert repository.work.publication.delivery_attempt == 0
    assert repository.rescheduled == [NOW + timedelta(seconds=900, microseconds=1)]


@pytest.mark.asyncio
async def test_known_current_message_suppresses_duplicate_edit() -> None:
    source = work()
    publication = replace(
        source.publication,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    request_with_message = replace(
        source.request,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    assert (
        await publisher.publish(
            interaction_id="300",
            claim_owner="worker",
            gateway_factory=lambda _: _gateway(gateway),
        )
        is StatusPublicationOutcome.DELIVERED
    )
    assert gateway.fetched == 1
    assert gateway.edited == 0


@pytest.mark.asyncio
async def test_ambiguous_edit_of_a_known_message_never_rearms_history_or_create() -> None:
    source = work()
    publication = replace(
        source.publication,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    request_with_message = replace(
        source.request,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.fetch_result = gateway.message(content="old state")
    gateway.edit_error = StatusWriteAmbiguous()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert gateway.fetched == 1
    assert gateway.edited == 1
    assert gateway.created == 0
    assert not repository.work.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_known_stale_message_is_edited_once() -> None:
    source = work()
    publication = replace(
        source.publication,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    request_with_message = replace(
        source.request,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.fetch_result = gateway.message(content="old status")
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.DELIVERED
    assert gateway.fetched == 1
    assert gateway.edited == 1
    assert gateway.created == 0


@pytest.mark.asyncio
async def test_unowned_fetched_message_is_rejected_before_edit() -> None:
    source = work()
    publication = replace(
        source.publication,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    request_with_message = replace(
        source.request,
        status_message_id="500",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.fetch_result = replace(gateway.message(content="unrelated"), author_id="201")
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert gateway.edited == 0


@pytest.mark.asyncio
async def test_transient_error_is_durably_rescheduled_without_sleep() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    gateway.create_error = StatusDeliveryError(
        StatusDeliveryErrorCode.RATE_LIMITED,
        retryable=True,
        retry_after_seconds=42.0,
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert repository.rescheduled
    assert repository.work.publication.delivery_attempt == 1
    assert not repository.work.publication.history_reconciliation_required
    assert repository.rescheduled[0] >= NOW + timedelta(seconds=42)


@pytest.mark.asyncio
async def test_ambiguous_first_create_enters_one_way_history_reconciliation() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    gateway.create_error = StatusWriteAmbiguous()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert gateway.created == 1
    assert repository.work.publication.history_reconciliation_required
    assert repository.work.publication.delivery_attempt == 1


@pytest.mark.asyncio
async def test_provider_retry_after_is_never_shortened() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    gateway.create_error = StatusDeliveryError(
        StatusDeliveryErrorCode.RATE_LIMITED,
        retryable=True,
        retry_after_seconds=900.0,
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert repository.rescheduled[0] >= NOW + timedelta(seconds=900)


@pytest.mark.asyncio
async def test_retryable_gateway_factory_failure_is_durably_rescheduled() -> None:
    repository = FakeStatusRepository(work())
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    async def fail_factory(request: IngressRequest) -> DiscordStatusGateway:
        del request
        raise StatusDeliveryError(StatusDeliveryErrorCode.UNAVAILABLE, retryable=True)

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=fail_factory,
    )

    assert outcome is StatusPublicationOutcome.RETRY_SCHEDULED
    assert repository.rescheduled
    assert repository.work.publication.delivery_attempt == 1
    assert not repository.work.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_last_transient_attempt_settles_failed_instead_of_retrying_forever() -> None:
    source = work()
    publication = replace(
        source.publication,
        state=StatusPublicationState.RETRYING,
        delivery_attempt=STATUS_MAX_DELIVERY_ATTEMPTS - 1,
        next_attempt_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=source.request, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.create_error = StatusDeliveryError(
        StatusDeliveryErrorCode.UNAVAILABLE,
        retryable=True,
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert repository.rescheduled == []
    assert gateway.created == 1
    assert repository.failed == [StatusDeliveryErrorCode.UNAVAILABLE.value]


@pytest.mark.asyncio
async def test_safe_prewrite_failures_stop_after_the_bounded_attempt_limit() -> None:
    repository = FakeStatusRepository(work())
    clock = FixedClock()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, clock),
    )

    async def fail_factory(request: IngressRequest) -> DiscordStatusGateway:
        del request
        raise StatusDeliveryError(StatusDeliveryErrorCode.UNAVAILABLE, retryable=True)

    outcomes: list[StatusPublicationOutcome] = []
    for _ in range(STATUS_MAX_DELIVERY_ATTEMPTS):
        outcomes.append(
            await publisher.publish(
                interaction_id="300",
                claim_owner="worker",
                gateway_factory=fail_factory,
            )
        )
        next_attempt_at = repository.work.publication.next_attempt_at
        if next_attempt_at is not None:
            clock.current = next_attempt_at

    assert outcomes == [
        *(
            StatusPublicationOutcome.RETRY_SCHEDULED
            for _ in range(STATUS_MAX_DELIVERY_ATTEMPTS - 1)
        ),
        StatusPublicationOutcome.FAILED,
    ]
    assert repository.claims == STATUS_MAX_DELIVERY_ATTEMPTS
    assert repository.work.publication.delivery_attempt == STATUS_MAX_DELIVERY_ATTEMPTS
    assert not repository.work.publication.history_reconciliation_required


@pytest.mark.asyncio
async def test_permanent_error_settles_failed() -> None:
    repository = FakeStatusRepository(work())
    gateway = FakeGateway(repository.work.publication, repository.work.request.application_id)
    gateway.create_error = StatusDeliveryError(
        StatusDeliveryErrorCode.REJECTED,
        retryable=False,
    )
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.FAILED
    assert repository.failed == [StatusDeliveryErrorCode.REJECTED.value]


@pytest.mark.asyncio
async def test_missing_message_rotates_nonce_and_recreates_in_same_invocation() -> None:
    source = work()
    publication = replace(
        source.publication,
        status_message_id="499",
        status_message_updated_at=NOW,
    )
    request_with_message = replace(
        source.request,
        status_message_id="499",
        status_message_updated_at=NOW,
    )
    repository = FakeStatusRepository(
        StatusPublicationWork(request=request_with_message, publication=publication)
    )
    gateway = FakeGateway(publication, source.request.application_id)
    gateway.fetch_result = StatusMessageMissing()
    publisher = PublicStatusPublisher(
        repository=cast(StatusPublicationRepository, repository),
        clock=cast(Clock, FixedClock()),
    )

    outcome = await publisher.publish(
        interaction_id="300",
        claim_owner="worker",
        gateway_factory=lambda _: _gateway(gateway),
    )

    assert outcome is StatusPublicationOutcome.DELIVERED
    assert repository.claims == 2
    assert gateway.created == 1
    assert repository.work.publication.incarnation == 1
    assert repository.work.publication.nonce != publication.nonce


async def _gateway(gateway: FakeGateway) -> DiscordStatusGateway:
    return cast(DiscordStatusGateway, gateway)
