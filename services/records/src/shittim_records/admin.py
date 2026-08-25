"""Pure authorization services for the Records ADMIN boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from shittim_records.archive import derive_requester_key
from shittim_records.auth import SessionRecord, csrf_hash, session_hash

IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")


class AdminFailure(RuntimeError):
    """Stable content-free failure raised at the ADMIN boundary."""

    def __init__(self, code: str, status: int) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AdminSecurityConfiguration:
    identity_hmac_key: bytes = field(repr=False)
    session_hmac_key: bytes = field(repr=False)
    admin_discord_user_id: str = field(repr=False)
    allowed_origin: str


class AdminSessionStore(Protocol):
    def get_session(self, *, session_hash: str) -> SessionRecord | None: ...


class AdminAuthorizer:
    """Authenticate a Session then derive the exact administrator key for every request."""

    def __init__(
        self,
        *,
        store: AdminSessionStore,
        configuration: AdminSecurityConfiguration,
    ) -> None:
        self._store = store
        self._configuration = configuration

    @property
    def allowed_origin(self) -> str:
        return self._configuration.allowed_origin

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord:
        if not raw_session:
            raise AdminFailure("AUTHENTICATION_REQUIRED", 401)
        stored = self._store.get_session(
            session_hash=session_hash(self._configuration.session_hmac_key, raw_session)
        )
        if stored is None or stored.expires_at <= int(_utc(now).timestamp()):
            raise AdminFailure("AUTHENTICATION_REQUIRED", 401)
        expected = derive_requester_key(
            self._configuration.identity_hmac_key,
            self._configuration.admin_discord_user_id,
        )
        if not hmac.compare_digest(expected, stored.requester_key):
            raise AdminFailure("ADMIN_ACCESS_DENIED", 403)
        return stored

    def authorize_write(
        self,
        *,
        session: SessionRecord,
        raw_csrf: str | None,
        csrf_header: str | None,
        origin: str | None,
        idempotency_key: str | None,
    ) -> str:
        if origin != self._configuration.allowed_origin:
            raise AdminFailure("ORIGIN_INVALID", 403)
        if not raw_csrf or not csrf_header or not hmac.compare_digest(raw_csrf, csrf_header):
            raise AdminFailure("CSRF_INVALID", 403)
        expected_csrf = csrf_hash(self._configuration.session_hmac_key, raw_csrf)
        if not hmac.compare_digest(expected_csrf, session.csrf_hash):
            raise AdminFailure("CSRF_INVALID", 403)
        if idempotency_key is None or IDEMPOTENCY_PATTERN.fullmatch(idempotency_key) is None:
            raise AdminFailure("IDEMPOTENCY_KEY_INVALID", 400)
        return hashlib.sha256(idempotency_key.encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
