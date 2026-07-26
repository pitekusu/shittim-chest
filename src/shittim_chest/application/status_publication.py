"""Durable public-status rendering and delivery orchestration."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum, unique
from typing import Protocol

from shittim_chest.application.ports import Clock, StatusPublicationRepository
from shittim_chest.application.scale_to_zero import (
    IngressKind,
    IngressRequest,
    IngressStatusPublication,
    StatusHistoryCheckpoint,
    StatusMessageState,
    StatusPublicationState,
    StatusPublicationWork,
)

DISCORD_MESSAGE_LIMIT = 2_000
STATUS_QUESTION_DISPLAY_LIMIT = 700
STATUS_REQUESTER_DISPLAY_LIMIT = 80
STATUS_RETRY_BASE_SECONDS = 5.0
STATUS_RETRY_MAX_SECONDS = 300.0
STATUS_MAX_DELIVERY_ATTEMPTS = 8
STATUS_MAX_CONVERGENCE_PASSES = 2

_MARKDOWN_CHARACTERS = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~=])")
_SNOWFLAKE = re.compile(r"[0-9]{1,20}\Z")


@unique
class StatusDeliveryErrorCode(StrEnum):
    """Content-free classifications returned by the Discord REST boundary."""

    MESSAGE_MISSING = "status_message_missing"
    MESSAGE_AMBIGUOUS = "status_message_ambiguous"
    RATE_LIMITED = "status_rate_limited"
    UNAVAILABLE = "status_unavailable"
    REJECTED = "status_delivery_rejected"
    CONFLICT = "status_delivery_conflict"


class StatusDeliveryError(RuntimeError):
    """Base error whose representation never contains provider or user content."""

    __slots__ = ("code", "retry_after_seconds", "retryable")

    def __init__(
        self,
        code: StatusDeliveryErrorCode,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class StatusMessageMissing(StatusDeliveryError):
    """A previously persisted Discord message was confirmed deleted."""

    def __init__(self) -> None:
        super().__init__(StatusDeliveryErrorCode.MESSAGE_MISSING, retryable=True)


class StatusWriteAmbiguous(StatusDeliveryError):
    """A Discord write may have succeeded even though no response can be trusted."""

    def __init__(
        self,
        code: StatusDeliveryErrorCode = StatusDeliveryErrorCode.UNAVAILABLE,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            code,
            retryable=True,
            retry_after_seconds=retry_after_seconds,
        )


class StatusHistoryProgress(StatusDeliveryError):
    """Retryable history scan with one complete durable Discord checkpoint."""

    __slots__ = ("checkpoint",)

    def __init__(
        self,
        code: StatusDeliveryErrorCode,
        *,
        checkpoint: StatusHistoryCheckpoint,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            code,
            retryable=True,
            retry_after_seconds=retry_after_seconds,
        )
        self.checkpoint = checkpoint


@dataclass(frozen=True, slots=True, repr=False)
class DiscordStatusMessage:
    """Minimal Discord message response required for idempotent delivery."""

    message_id: str
    channel_id: str
    author_id: str
    content: str
    nonce: str | None

    def __post_init__(self) -> None:
        for label, value in (
            ("message ID", self.message_id),
            ("channel ID", self.channel_id),
            ("author ID", self.author_id),
        ):
            _require_discord_snowflake(value, label=label)
        if len(self.content) > DISCORD_MESSAGE_LIMIT:
            raise ValueError("Discord status content exceeds the message limit")


class DiscordStatusGateway(Protocol):
    """Structural base for type-checking one moderator-only REST adapter."""

    async def current_bot_user_id(self) -> str: ...

    async def fetch_message(self, *, channel_id: str, message_id: str) -> DiscordStatusMessage: ...

    async def find_by_nonce(
        self,
        *,
        channel_id: str,
        author_id: str,
        nonce: str,
        operation_marker: str,
        after_message_id: str,
        checkpoint: StatusHistoryCheckpoint | None,
    ) -> DiscordStatusMessage | None: ...

    async def create_message(
        self,
        *,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> DiscordStatusMessage: ...

    async def edit_message(
        self,
        *,
        channel_id: str,
        message_id: str,
        content: str,
    ) -> DiscordStatusMessage: ...


StatusGatewayFactory = Callable[[IngressRequest], Awaitable[DiscordStatusGateway]]


@unique
class StatusPublicationOutcome(StrEnum):
    """Low-cardinality result safe for Lambda telemetry."""

    NO_WORK = "no_work"
    DELIVERED = "delivered"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


def render_public_status(
    request: IngressRequest,
    state: StatusMessageState,
) -> str:
    """Render deterministic public content without Discord or AWS dependencies."""

    state_text = _state_text(state)
    question = _question_text(request)
    requester = sanitize_status_text(
        request.requester_display_name,
        limit=STATUS_REQUESTER_DISPLAY_LIMIT,
    )
    accepted_at = int(request.created_at.timestamp())
    content = (
        f"状態: {state.name} ({state_text})\n"
        f"議題: {question}\n"
        f"依頼者: {requester}\n"
        f"受付時刻: <t:{accepted_at}:F>\n"
        f"識別子: {status_publication_marker(request.interaction_id)}"
    )
    if len(content) > DISCORD_MESSAGE_LIMIT:
        raise ValueError("rendered public status exceeds Discord's message limit")
    return content


def sanitize_status_text(value: str, *, limit: int) -> str:
    """Flatten unsafe user text, neutralize mentions, and escape Markdown."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2:
        raise ValueError("status text limit must be an integer of at least two")
    normalized = unicodedata.normalize("NFKC", value)
    safe_characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            safe_characters.append(" ")
        else:
            safe_characters.append(character)
    flattened = " ".join("".join(safe_characters).split())
    flattened = flattened.replace("@", "\uff20").replace("<", "\uff1c").replace(">", "\uff1e")
    escaped = _MARKDOWN_CHARACTERS.sub(r"\\\1", flattened)
    if not escaped:
        escaped = "(表示なし)"
    if len(escaped) <= limit:
        return escaped
    return f"{escaped[: limit - 1].rstrip()}…"


def status_content_hash(content: str) -> str:
    """Hash the exact UTF-8 body persisted before any Discord request."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def status_publication_marker(canonical_interaction_id: str) -> str:
    """Return a stable public-safe marker when Discord omits a message nonce."""

    if not canonical_interaction_id.strip():
        raise ValueError("canonical interaction ID must not be empty")
    digest = hashlib.sha256(f"status-marker:{canonical_interaction_id}".encode()).hexdigest()
    return f"sc-{digest[:20]}"


def has_exact_status_publication_marker(content: str, operation_marker: str) -> bool:
    """Match only the dedicated final marker line, never user-controlled prose."""

    if not operation_marker or "\n" in operation_marker or "\r" in operation_marker:
        return False
    return content.rsplit("\n", maxsplit=1)[-1] == f"識別子: {operation_marker}"


class PublicStatusPublisher:
    """Claim and converge one durable desired state through the moderator Bot."""

    __slots__ = ("_clock", "_repository")

    def __init__(
        self,
        *,
        repository: StatusPublicationRepository,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def publish(
        self,
        *,
        interaction_id: str,
        claim_owner: str,
        gateway_factory: StatusGatewayFactory,
    ) -> StatusPublicationOutcome:
        """Publish at most one current state, durably scheduling any retry."""

        claimed = await self._repository.claim_status_publication(
            interaction_id=interaction_id,
            claim_owner=claim_owner,
            at=self._clock.now(),
        )
        if claimed is None:
            return StatusPublicationOutcome.NO_WORK
        try:
            gateway = await gateway_factory(claimed.request)
        except StatusDeliveryError as error:
            return await self._settle_error(claimed, claim_owner, error)
        for convergence_pass in range(STATUS_MAX_CONVERGENCE_PASSES):
            try:
                message = await self._deliver(claimed.publication, gateway)
            except StatusMessageMissing:
                replacement = await self._repository.replace_missing_status_message(
                    work=claimed,
                    claim_owner=claim_owner,
                    at=self._clock.now(),
                )
                reclaimed = await self._repository.claim_status_publication(
                    interaction_id=replacement.publication.canonical_interaction_id,
                    claim_owner=claim_owner,
                    at=self._clock.now(),
                )
                if reclaimed is None:
                    return StatusPublicationOutcome.RETRY_SCHEDULED
                try:
                    message = await self._deliver(
                        reclaimed.publication,
                        gateway,
                    )
                except StatusDeliveryError as error:
                    return await self._settle_error(reclaimed, claim_owner, error)
                if message is None:
                    return await self._settle_error(
                        reclaimed,
                        claim_owner,
                        StatusDeliveryError(
                            StatusDeliveryErrorCode.MESSAGE_AMBIGUOUS,
                            retryable=True,
                        ),
                    )
                claimed = reclaimed
            except StatusDeliveryError as error:
                return await self._settle_error(claimed, claim_owner, error)

            if message is None:
                return await self._settle_error(
                    claimed,
                    claim_owner,
                    StatusDeliveryError(
                        StatusDeliveryErrorCode.MESSAGE_AMBIGUOUS,
                        retryable=True,
                    ),
                )

            settled = await self._repository.mark_status_delivered(
                work=claimed,
                claim_owner=claim_owner,
                message_id=message.message_id,
                at=self._clock.now(),
            )
            if settled.state is StatusPublicationState.DELIVERED:
                return StatusPublicationOutcome.DELIVERED
            if convergence_pass + 1 >= STATUS_MAX_CONVERGENCE_PASSES:
                return StatusPublicationOutcome.RETRY_SCHEDULED
            claimed = await self._repository.claim_status_publication(
                interaction_id=interaction_id,
                claim_owner=claim_owner,
                at=self._clock.now(),
            )
            if claimed is None:
                return StatusPublicationOutcome.RETRY_SCHEDULED
        return StatusPublicationOutcome.RETRY_SCHEDULED

    async def _deliver(
        self,
        publication: IngressStatusPublication,
        gateway: DiscordStatusGateway,
    ) -> DiscordStatusMessage | None:
        expected_author = await gateway.current_bot_user_id()
        message: DiscordStatusMessage | None = None
        created_now = False
        if publication.status_message_id is not None:
            message = await gateway.fetch_message(
                channel_id=publication.status_channel_id,
                message_id=publication.status_message_id,
            )
            self._validate_owned_message(
                message,
                publication,
                expected_author=expected_author,
                expected_message_id=publication.status_message_id,
            )
        elif publication.history_reconciliation_required:
            message = await gateway.find_by_nonce(
                channel_id=publication.status_channel_id,
                author_id=expected_author,
                nonce=publication.nonce,
                operation_marker=status_publication_marker(publication.canonical_interaction_id),
                after_message_id=publication.canonical_interaction_id,
                checkpoint=publication.history_checkpoint,
            )
            if message is None:
                return None
            self._validate_owned_message(
                message,
                publication,
                expected_author=expected_author,
            )

        if message is None:
            message = await gateway.create_message(
                channel_id=publication.status_channel_id,
                content=publication.content,
                nonce=publication.nonce,
            )
            try:
                self._validate_owned_message(
                    message,
                    publication,
                    expected_author=expected_author,
                )
            except StatusDeliveryError:
                raise StatusWriteAmbiguous(StatusDeliveryErrorCode.CONFLICT) from None
            created_now = True
        if message.content != publication.content:
            if created_now:
                raise StatusWriteAmbiguous(StatusDeliveryErrorCode.CONFLICT)
            expected_message_id = message.message_id
            message = await gateway.edit_message(
                channel_id=publication.status_channel_id,
                message_id=expected_message_id,
                content=publication.content,
            )
            self._validate_owned_message(
                message,
                publication,
                expected_author=expected_author,
                expected_message_id=expected_message_id,
            )
        self._validate_delivery(message, publication)
        return message

    async def _settle_error(
        self,
        work: StatusPublicationWork,
        claim_owner: str,
        error: StatusDeliveryError,
    ) -> StatusPublicationOutcome:
        history_checkpoint = error.checkpoint if isinstance(error, StatusHistoryProgress) else None
        history_progressed = (
            history_checkpoint is not None
            and history_checkpoint != work.publication.history_checkpoint
        )
        if error.retryable and (
            history_progressed or work.publication.delivery_attempt < STATUS_MAX_DELIVERY_ATTEMPTS
        ):
            at = self._clock.now()
            await self._repository.reschedule_status_publication(
                work=work,
                claim_owner=claim_owner,
                at=at,
                next_attempt_at=at + self._retry_delay(work.publication, error),
                error_code=error.code.value,
                history_checkpoint=history_checkpoint,
                message_may_exist=isinstance(error, StatusWriteAmbiguous),
            )
            return StatusPublicationOutcome.RETRY_SCHEDULED
        settled = await self._repository.mark_status_failed(
            work=work,
            claim_owner=claim_owner,
            at=self._clock.now(),
            error_code=error.code.value,
            message_may_exist=isinstance(error, StatusWriteAmbiguous),
        )
        if settled.state is StatusPublicationState.FAILED:
            return StatusPublicationOutcome.FAILED
        return StatusPublicationOutcome.RETRY_SCHEDULED

    @staticmethod
    def _validate_delivery(
        message: DiscordStatusMessage,
        publication: IngressStatusPublication,
    ) -> None:
        if (
            message.content != publication.content
            or status_content_hash(message.content) != publication.content_hash
        ):
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.CONFLICT,
                retryable=False,
            )

    @staticmethod
    def _validate_owned_message(
        message: DiscordStatusMessage,
        publication: IngressStatusPublication,
        *,
        expected_author: str,
        expected_message_id: str | None = None,
    ) -> None:
        marker = status_publication_marker(publication.canonical_interaction_id)
        if (
            message.channel_id != publication.status_channel_id
            or message.author_id != expected_author
            or (expected_message_id is not None and message.message_id != expected_message_id)
            or (message.nonce is not None and message.nonce != publication.nonce)
            or (
                message.nonce is None
                and not has_exact_status_publication_marker(message.content, marker)
            )
        ):
            raise StatusDeliveryError(
                StatusDeliveryErrorCode.CONFLICT,
                retryable=False,
            )

    @staticmethod
    def _retry_delay(
        publication: IngressStatusPublication,
        error: StatusDeliveryError,
    ) -> timedelta:
        delay = min(
            STATUS_RETRY_MAX_SECONDS,
            STATUS_RETRY_BASE_SECONDS * (2 ** max(0, publication.delivery_attempt - 1)),
        )
        if error.retry_after_seconds is not None and math.isfinite(error.retry_after_seconds):
            delay = max(delay, error.retry_after_seconds)
        return timedelta(seconds=delay)


def _question_text(request: IngressRequest) -> str:
    if request.question is not None:
        return sanitize_status_text(request.question, limit=STATUS_QUESTION_DISPLAY_LIMIT)
    operation = {
        IngressKind.RETRY: "既存の議論の再試行",
        IngressKind.CANCEL: "既存の議論の中止",
    }.get(request.kind)
    if operation is None:
        raise ValueError("status request has no displayable topic")
    return operation


def _state_text(state: StatusMessageState) -> str:
    labels = {
        StatusMessageState.PENDING: "待機中",
        StatusMessageState.STARTING: "実行環境を起動中",
        StatusMessageState.READY: "処理準備完了",
        StatusMessageState.STARTUP_TIMEOUT: (
            "3分以内に起動できませんでした。依頼は保存済みで自動復旧を継続中です。再実行は不要です"
        ),
        StatusMessageState.RECOVERED: "シッテムの箱が復旧しました。議論を開始します",
        StatusMessageState.ACCEPTED: "処理を開始",
        StatusMessageState.COMPLETED: "完了",
        StatusMessageState.CANCELLED: "中止",
        StatusMessageState.REJECTED: "受付不可",
        StatusMessageState.TERMINAL_FAILED: (
            "シッテムの箱を起動できませんでした。依頼を再実行してください"
        ),
    }
    return labels[state]


def _require_discord_snowflake(value: str, *, label: str) -> None:
    if (
        _SNOWFLAKE.fullmatch(value) is None
        or not 0 < int(value) < 2**64
        or str(int(value)) != value
    ):
        raise ValueError(f"Discord status {label} must be a canonical snowflake")


__all__ = (
    "DISCORD_MESSAGE_LIMIT",
    "STATUS_MAX_CONVERGENCE_PASSES",
    "STATUS_MAX_DELIVERY_ATTEMPTS",
    "DiscordStatusGateway",
    "DiscordStatusMessage",
    "PublicStatusPublisher",
    "StatusDeliveryError",
    "StatusDeliveryErrorCode",
    "StatusGatewayFactory",
    "StatusMessageMissing",
    "StatusPublicationOutcome",
    "StatusWriteAmbiguous",
    "render_public_status",
    "sanitize_status_text",
    "status_content_hash",
    "status_publication_marker",
)
