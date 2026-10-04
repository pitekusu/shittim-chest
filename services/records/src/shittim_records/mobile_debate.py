"""Native debate acceptance through the existing durable Core ingress."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.application.ports import RepositoryIdentityConflict, RepositoryQueueFull
from shittim_chest.application.scale_to_zero import (
    EnqueuedIngress,
    IngressRequest,
    IngressStatus,
    StatusMessageState,
)
from shittim_chest.domain import DebatePhase

from shittim_records.archive import derive_requester_key
from shittim_records.auth import AuthFailure, _digest
from shittim_records.contracts import (
    DebateRequestResponse,
    DebateRequestsResponse,
    DebateRequestStatus,
    DebateStartRequest,
)
from shittim_records.mobile_auth import MobileSessionRecord
from shittim_records.mobile_session import MobileSessionStore, authenticate_mobile_session


class DebateRequestFailure(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DebateProgress:
    phase: DebatePhase | None = None
    updated_at: datetime | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class DebateRequestPage:
    requests: tuple[IngressRequest, ...]
    next_created_at: datetime | None = None
    next_request_id: str | None = None


class MobileDebateRepository(Protocol):
    async def get(self, *, owner_key: str, request_id: str) -> IngressRequest | None: ...

    async def enqueue(
        self,
        request: IngressRequest,
        *,
        session_hash: str,
        session: MobileSessionRecord,
        now: datetime,
    ) -> EnqueuedIngress: ...

    async def page(
        self,
        *,
        owner_key: str,
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> DebateRequestPage: ...

    def progress(self, request: IngressRequest) -> DebateProgress: ...


class DebatePermissionGuard(Protocol):
    async def check(self, *, requester_id: str) -> None: ...


class DebateRequestCursor:
    """Sign public continuation coordinates; never encode internal table keys.

    The existing Records CursorCodec carries GSI keys and lacks an owner binding;
    that storage-specific format cannot be exposed for a person's private requests.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("debate cursor key invalid")
        self._key = key

    def encode(
        self,
        *,
        owner: str,
        limit: int,
        at: datetime,
        request_id: str,
        now: datetime,
    ) -> str:
        payload = json.dumps(
            {
                "at": at.isoformat(),
                "id": request_id,
                "limit": limit,
                "expires": int(now.timestamp()) + 3600,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"{encoded}.{self._signature(owner, encoded)}"

    def decode(
        self,
        *,
        owner: str,
        limit: int,
        cursor: str,
        now: datetime,
    ) -> tuple[datetime, str]:
        try:
            if len(cursor) > 1024:
                raise ValueError
            encoded, signature = cursor.split(".")
            if not hmac.compare_digest(signature, self._signature(owner, encoded)):
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"at", "id", "limit", "expires"}
                or type(payload["expires"]) is not int
                or payload["expires"] <= int(now.timestamp())
                or type(payload["limit"]) is not int
                or payload["limit"] != limit
            ):
                raise ValueError
            # Reuse the request contract for the UUID boundary.
            DebateStartRequest(request_id=payload["id"], question="cursor")
            at = datetime.fromisoformat(payload["at"])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError
            return at.astimezone(UTC), payload["id"]
        except ValueError, TypeError, KeyError:
            raise DebateRequestFailure("CURSOR_INVALID", 400) from None

    def _signature(self, owner: str, encoded: str) -> str:
        return hmac.new(
            self._key,
            f"mobile-debate:cursor:{owner}:{encoded}".encode(),
            hashlib.sha256,
        ).hexdigest()


class MobileDebateService:
    def __init__(
        self,
        *,
        sessions: MobileSessionStore,
        repository: MobileDebateRepository,
        permissions: DebatePermissionGuard,
        runtime: DiscordRuntimeConfig,
        channel_id: str,
        enabled: bool,
        session_key: bytes,
        identity_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._repository = repository
        self._permissions = permissions
        self._runtime = runtime
        self._channel_id = channel_id
        self._enabled = enabled
        self._session_key = session_key
        self._identity_key = identity_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cursors = DebateRequestCursor(session_key)

    def _authenticate(self, token: str | None) -> MobileSessionRecord:
        session = authenticate_mobile_session(
            store=self._sessions,
            session_key=self._session_key,
            raw_token=token,
            clock=self._clock,
        )
        if session is None:
            raise AuthFailure("session_required")
        return session

    def _still_authorized(self, token: str | None, expected: MobileSessionRecord) -> None:
        if self._authenticate(token) != expected:
            raise AuthFailure("session_required")

    async def start(
        self,
        *,
        token: str | None,
        payload: DebateStartRequest,
    ) -> tuple[DebateRequestResponse, bool]:
        session = self._authenticate(token)
        # Replay frozen identity/content first: no reauthorization or changed profile may
        # manufacture a second operation after an ambiguous successful submission.
        existing = await self._repository.get(
            owner_key=session.requester_key,
            request_id=payload.request_id,
        )
        if existing is not None:
            if existing.question != payload.question:
                raise DebateRequestFailure("REQUEST_ID_CONFLICT", 409)
            response = self._view(existing)
            self._still_authorized(token, session)
            return response, False
        if not self._enabled:
            raise DebateRequestFailure("DEBATE_START_DISABLED", 503)
        if session.discord_user_id is None or session.discord_username is None:
            raise DebateRequestFailure("DEBATE_START_REAUTH_REQUIRED", 403)
        if not hmac.compare_digest(
            derive_requester_key(self._identity_key, session.discord_user_id),
            session.requester_key,
        ):
            raise AuthFailure("session_required")
        if self._channel_id not in self._runtime.allowed_channel_ids:
            raise DebateRequestFailure("DEBATE_START_DISABLED", 503)
        try:
            await self._permissions.check(requester_id=session.discord_user_id)
        except DebateRequestFailure as error:
            if error.code == "GUILD_MEMBERSHIP_REQUIRED":
                # A known departure must not immediately regain cached read
                # authorization through /mobile/session with this same token.
                # Channel-only permission loss or Discord outages do not revoke.
                self._sessions.delete_session(
                    session_hash=_digest(self._session_key, "mobile-session", token or "")
                )
            raise
        self._still_authorized(token, session)
        now = self._clock()
        request = IngressRequest.mobile_debate(
            request_id=payload.request_id,
            owner_key=session.requester_key,
            application_id=self._runtime.application_id_for(DiscordBotSlot.MODERATOR),
            question=payload.question,
            requester_id=session.discord_user_id,
            requester_username=session.discord_username,
            requester_display_name=session.display_name,
            guild_id=self._runtime.guild_id,
            channel_id=self._channel_id,
            created_at=now,
        )
        try:
            result = await self._repository.enqueue(
                request,
                session_hash=_digest(self._session_key, "mobile-session", token or ""),
                session=session,
                now=now,
            )
        except RepositoryQueueFull:
            raise DebateRequestFailure("DEBATE_QUEUE_FULL", 429) from None
        except RepositoryIdentityConflict:
            # Another same-ID POST can win between lookup and enqueue. Resolve by
            # the public owner/content binding, not today's mutable profile/config.
            existing = await self._repository.get(
                owner_key=session.requester_key, request_id=payload.request_id
            )
            if existing is None or existing.question != payload.question:
                raise DebateRequestFailure("REQUEST_ID_CONFLICT", 409) from None
            response = self._view(existing)
            self._still_authorized(token, session)
            return response, False
        response = self._view(result.request)
        self._still_authorized(token, session)
        return response, result.created

    async def get(self, *, token: str | None, request_id: str) -> DebateRequestResponse:
        session = self._authenticate(token)
        request = await self._repository.get(owner_key=session.requester_key, request_id=request_id)
        if request is None:
            raise DebateRequestFailure("DEBATE_REQUEST_NOT_FOUND", 404)
        response = self._view(request)
        self._still_authorized(token, session)
        return response

    async def list(
        self,
        *,
        token: str | None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> DebateRequestsResponse:
        session = self._authenticate(token)
        if not 1 <= limit <= 50:
            raise DebateRequestFailure("REQUEST_INVALID", 400)
        after = (
            None
            if cursor is None
            else self._cursors.decode(
                owner=session.requester_key,
                limit=limit,
                cursor=cursor,
                now=self._clock(),
            )
        )
        page = await self._repository.page(
            owner_key=session.requester_key, limit=limit, after=after
        )
        items = [self._view(request) for request in page.requests]
        next_cursor = None
        if page.next_created_at is not None and page.next_request_id is not None:
            next_cursor = self._cursors.encode(
                owner=session.requester_key,
                limit=limit,
                at=page.next_created_at,
                request_id=page.next_request_id,
                now=self._clock(),
            )
        self._still_authorized(token, session)
        return DebateRequestsResponse(items=items, next_cursor=next_cursor)

    def _view(self, request: IngressRequest) -> DebateRequestResponse:
        if request.mobile_request_id is None or request.question is None:
            raise DebateRequestFailure("DEBATE_REQUESTS_UNAVAILABLE", 503)
        progress = self._repository.progress(request)
        status: DebateRequestStatus
        error_code = None
        if progress.record_id is not None:
            status = "published"
        elif progress.phase is DebatePhase.CANCELLED:
            status, error_code = "cancelled", "DEBATE_CANCELLED"
        elif request.status in {IngressStatus.REJECTED, IngressStatus.FAILED}:
            status = "failed"
            error_code = (
                "DEBATE_REJECTED" if request.status is IngressStatus.REJECTED else "DEBATE_FAILED"
            )
        elif progress.phase is DebatePhase.FAILED:
            status, error_code = "failed", "DEBATE_FAILED"
        elif progress.phase is DebatePhase.COMPLETED or request.status is IngressStatus.COMPLETED:
            status = "publishing"
        elif progress.phase is not None and progress.phase is not DebatePhase.ACCEPTED:
            status = "running"
        elif progress.phase is DebatePhase.ACCEPTED or request.status is IngressStatus.ACCEPTED:
            status = "queued"
        elif (
            request.status_message_state is StatusMessageState.STARTING
            and request.status_message_id is not None
        ):
            # The factory's STARTING value is an unposted intent. Only a confirmed
            # saved Discord status message establishes that user-facing state.
            status = "starting"
        elif request.status is IngressStatus.PENDING and request.status_message_id is None:
            status = "accepted"
        else:
            status = "queued"
        return DebateRequestResponse(
            request_id=request.mobile_request_id,
            question=request.question,
            status=status,
            phase=None if progress.phase is None else progress.phase.value,
            created_at=request.created_at,
            updated_at=max(request.updated_at, progress.updated_at or request.updated_at),
            record_id=progress.record_id,
            error_code=error_code,
        )
