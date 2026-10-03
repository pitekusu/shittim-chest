"""Minimal Android publication notifications, bound to a live mobile session."""

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from shittim_records.auth import AuthFailure, _digest
from shittim_records.mobile_auth import (
    Digest,
    EpochSeconds,
    MobileNotificationDeviceRequest,
    MobileNotificationDeviceResponse,
    MobileSessionRecord,
    OpaqueValue,
)
from shittim_records.mobile_session import MobileSessionReader, authenticate_mobile_session

OUTBOX_PK = "MOBILE_PUSH_OUTBOX"
DEVICE_PK = "MOBILE_PUSH_DEVICE"
DELIVERY_PREFIX = "MOBILE_PUSH_DELIVERY#"
EVENT_MAX_AGE_SECONDS = 24 * 60 * 60
MAX_DELIVERY_ATTEMPTS = 3
MAX_EVENT_RUNS = 60


def timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("mobile_push_time_invalid")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def pending_mobile_notification(*, record_id: str, created_at: datetime) -> dict[str, str | int]:
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", record_id) is None:
        raise ValueError("mobile_push_record_invalid")
    return {
        "PK": OUTBOX_PK,
        "SK": record_id,
        "schema_version": 1,
        "record_id": record_id,
        "published_at": timestamp(created_at),
        "state": "pending",
        "runs": 0,
        "expiresAt": int(created_at.timestamp()) + EVENT_MAX_AGE_SECONDS,
    }


class NotificationDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    token: str = Field(repr=False, min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9:_\-.]+$")
    token_hash: Digest
    binding_id: OpaqueValue
    session_hash: Digest
    session_created_at: EpochSeconds
    registered_at: str
    expires_at: EpochSeconds


class NotificationStore(Protocol):
    def register(self, device: NotificationDevice, *, now_epoch: int) -> None: ...

    def unregister(self, *, token_hash: str, binding_id: str, session_hash: str) -> None: ...

    def get_event(self, record_id: str) -> Mapping[str, object] | None: ...

    def claim_event(self, record_id: str, *, now_epoch: int) -> bool: ...

    def finish_event(self, record_id: str, *, state: str) -> None: ...

    def devices(self, *, after: str | None = None) -> Iterable[NotificationDevice]: ...

    def checkpoint(self, record_id: str, *, cursor: str | None, retry_needed: bool) -> None: ...

    def current_device(self, token_hash: str) -> NotificationDevice | None: ...

    def claim_delivery(self, record_id: str, device: NotificationDevice, *, now_epoch: int) -> str:
        """Return send, wait, or done; a prior ambiguous send must not be repeated."""
        ...

    def finish_delivery(
        self,
        record_id: str,
        device: NotificationDevice,
        *,
        state: str,
        now_epoch: int,
        retry_after: int = 60,
    ) -> None: ...

    def record_requester_name(self, record_id: str) -> str | None: ...


def device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class MobileNotificationRegistrationService:
    def __init__(
        self,
        *,
        sessions: MobileSessionReader,
        store: NotificationStore,
        session_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._key = session_key
        self._clock = clock or (lambda: datetime.now(UTC))

    def update(
        self, *, raw_token: str | None, request: MobileNotificationDeviceRequest, delete: bool
    ) -> MobileNotificationDeviceResponse | None:
        session = authenticate_mobile_session(
            store=self._sessions, session_key=self._key, raw_token=raw_token, clock=self._clock
        )
        if session is None or raw_token is None:
            raise AuthFailure("session_required")
        session_hash = _digest(self._key, "mobile-session", raw_token)
        token_hash = device_token_hash(request.token)
        if delete:
            self._store.unregister(
                token_hash=token_hash, binding_id=request.binding_id, session_hash=session_hash
            )
            return None
        now = self._clock()
        self._store.register(
            NotificationDevice(
                token=request.token,
                token_hash=token_hash,
                binding_id=request.binding_id,
                session_hash=session_hash,
                session_created_at=session.created_at,
                registered_at=timestamp(now),
                expires_at=session.expires_at,
            ),
            now_epoch=int(now.timestamp()),
        )
        return MobileNotificationDeviceResponse.model_validate(
            {"schemaVersion": 1, "expiresAt": datetime.fromtimestamp(session.expires_at, UTC)}
        )


class PushDeliveryFailed(Exception):
    def __init__(self, code: str, *, retry_after: int = 60) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = max(60, min(86400, retry_after))


class NotificationSender(Protocol):
    def send(self, *, token: str, data: dict[str, str]) -> None: ...


@dataclass(frozen=True)
class NotificationSummary:
    sent: int = 0
    failed: int = 0
    pending: bool = False


class MobileNotificationDispatchService:
    def __init__(
        self,
        *,
        store: NotificationStore,
        sessions: MobileSessionReader,
        sender: NotificationSender | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch(
        self, record_id: str, *, remaining_seconds: Callable[[], float] = lambda: 120
    ) -> NotificationSummary:
        event = self._store.get_event(record_id)
        now_epoch = int(self._clock().timestamp())
        if event is None or event.get("state") not in {"pending", "processing"}:
            return NotificationSummary()
        if not self._store.claim_event(record_id, now_epoch=now_epoch):
            return NotificationSummary()
        expires_at = event.get("expiresAt")
        runs = event.get("runs")
        if not isinstance(expires_at, int) or now_epoch >= expires_at:
            self._store.finish_event(record_id, state="expired")
            return NotificationSummary()
        if not isinstance(runs, int) or runs >= MAX_EVENT_RUNS:
            self._store.finish_event(record_id, state="failed")
            return NotificationSummary(failed=1)
        if self._sender is None:
            self._store.finish_event(record_id, state="disabled")
            return NotificationSummary()
        requester_name = self._store.record_requester_name(record_id)
        if requester_name is None:
            self._store.finish_event(record_id, state="cancelled")
            return NotificationSummary()
        published_at = event.get("published_at")
        if not isinstance(published_at, str):
            self._store.finish_event(record_id, state="failed")
            return NotificationSummary(failed=1)
        sent = failed = 0
        pending = event.get("retry_needed") is True
        interrupted = False
        cursor = event.get("cursor")
        for device in self._store.devices(after=cursor if isinstance(cursor, str) else None):
            if remaining_seconds() < 35:
                pending = True
                interrupted = True
                break
            now_epoch = int(self._clock().timestamp())
            if now_epoch >= expires_at:
                self._store.finish_event(record_id, state="expired")
                return NotificationSummary(sent=sent, failed=failed)
            if device.expires_at <= now_epoch:
                self._store.unregister(
                    token_hash=device.token_hash,
                    binding_id=device.binding_id,
                    session_hash=device.session_hash,
                )
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            if device.registered_at > published_at:
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            session = self._sessions.get_session(session_hash=device.session_hash)
            if not _live_session(session, device, now_epoch):
                self._store.unregister(
                    token_hash=device.token_hash,
                    binding_id=device.binding_id,
                    session_hash=device.session_hash,
                )
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            # Re-check both registration and authorization immediately before acquiring
            # its receipt; old jobs cannot target a token rebound to another login.
            if self._store.current_device(device.token_hash) != device:
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            decision = self._store.claim_delivery(record_id, device, now_epoch=now_epoch)
            if decision == "wait":
                pending = True
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            if decision != "send":
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            session = self._sessions.get_session(session_hash=device.session_hash)
            if (
                not _live_session(session, device, int(self._clock().timestamp()))
                or self._store.current_device(device.token_hash) != device
            ):
                self._store.finish_delivery(
                    record_id, device, state="cancelled", now_epoch=now_epoch
                )
                self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
                continue
            data = {
                "type": "record_published",
                "schemaVersion": "1",
                "recordId": record_id,
                "bindingId": device.binding_id,
                "publishedAt": published_at,
                "requesterName": safe_requester_name(requester_name),
            }
            try:
                self._sender.send(token=device.token, data=data)
            except PushDeliveryFailed as error:
                state = "retry" if error.code == "retryable" else error.code
                self._store.finish_delivery(
                    record_id,
                    device,
                    state=state,
                    now_epoch=int(self._clock().timestamp()),
                    retry_after=error.retry_after,
                )
                if state == "invalid_token":
                    self._store.unregister(
                        token_hash=device.token_hash,
                        binding_id=device.binding_id,
                        session_hash=device.session_hash,
                    )
                failed += 1
                pending |= state == "retry"
            except Exception:
                # The provider may have accepted a request whose response was lost.
                # Retrying this delivery blindly would duplicate a visible notification.
                self._store.finish_delivery(record_id, device, state="unknown", now_epoch=now_epoch)
                failed += 1
            else:
                self._store.finish_delivery(record_id, device, state="sent", now_epoch=now_epoch)
                sent += 1
            self._store.checkpoint(record_id, cursor=device.token_hash, retry_needed=pending)
        if not interrupted:
            self._store.checkpoint(record_id, cursor=None, retry_needed=False)
        self._store.finish_event(record_id, state="pending" if pending else "complete")
        return NotificationSummary(sent=sent, failed=failed, pending=pending)


def _live_session(
    session: MobileSessionRecord | None, device: NotificationDevice, now_epoch: int
) -> bool:
    return (
        session is not None
        and session.created_at == device.session_created_at
        and session.created_at <= now_epoch < session.expires_at == device.expires_at
    )


def safe_requester_name(value: str) -> str:
    """Only a bounded display name goes to FCM; no multiline notification injection."""
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cs"} else character
        for character in normalized
        if unicodedata.category(character) != "Cf" or character in {"\u200c", "\u200d"}
    )
    single_line = " ".join(cleaned.split()).strip()
    return unicodedata.normalize("NFC", single_line)[:100] or "依頼者"
