"""ADMIN authorization service tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shittim_records.admin import (
    AdminAuthorizer,
    AdminFailure,
    AdminSecurityConfiguration,
)
from shittim_records.archive import derive_requester_key
from shittim_records.auth import SessionRecord, csrf_hash, session_hash

NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
ADMIN_ID = "123456789" + "012345678"
IDENTITY_KEY = b"i" * 32
SESSION_KEY = b"s" * 32


class SessionStore:
    def __init__(self, record: SessionRecord | None) -> None:
        self.record = record
        self.received_hash: str | None = None

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        self.received_hash = session_hash
        return self.record


def session(requester_key: str) -> SessionRecord:
    return SessionRecord(
        requester_key=requester_key,
        display_name="private-name",
        avatar_asset_key=None,
        csrf_hash=csrf_hash(SESSION_KEY, "csrf-token"),
        guild_verified_at=NOW.isoformat(),
        expires_at=int((NOW + timedelta(hours=1)).timestamp()),
    )


def configuration() -> AdminSecurityConfiguration:
    return AdminSecurityConfiguration(
        identity_hmac_key=IDENTITY_KEY,
        session_hmac_key=SESSION_KEY,
        admin_discord_user_id=ADMIN_ID,
        allowed_origin="https://records.example.invalid",
    )


def test_authorizer_accepts_only_exact_hmac_derived_discord_id() -> None:
    expected = derive_requester_key(IDENTITY_KEY, ADMIN_ID)
    store = SessionStore(session(expected))
    authorizer = AdminAuthorizer(store=store, configuration=configuration())

    authenticated = authorizer.authenticate(raw_session="session-token", now=NOW)

    assert authenticated.requester_key == expected
    assert store.received_hash == session_hash(SESSION_KEY, "session-token")
    assert ADMIN_ID not in repr(authorizer)

    denied = AdminAuthorizer(
        store=SessionStore(session(derive_requester_key(IDENTITY_KEY, "999999999" * 2))),
        configuration=configuration(),
    )
    with pytest.raises(AdminFailure) as caught:
        denied.authenticate(raw_session="session-token", now=NOW)
    assert caught.value.code == "ADMIN_ACCESS_DENIED"
    assert caught.value.status == 403


@pytest.mark.parametrize(
    ("origin", "csrf_header", "idempotency_key", "expected_code"),
    (
        ("https://other.example.invalid", "csrf-token", "idempotency-key-1", "ORIGIN_INVALID"),
        (
            "https://records.example.invalid",
            "wrong-csrf",
            "idempotency-key-1",
            "CSRF_INVALID",
        ),
        (
            "https://records.example.invalid",
            "csrf-token",
            "short",
            "IDEMPOTENCY_KEY_INVALID",
        ),
    ),
)
def test_admin_writes_require_exact_origin_csrf_and_idempotency(
    origin: str,
    csrf_header: str,
    idempotency_key: str,
    expected_code: str,
) -> None:
    authorizer = AdminAuthorizer(store=SessionStore(None), configuration=configuration())

    with pytest.raises(AdminFailure) as caught:
        authorizer.authorize_write(
            session=session(derive_requester_key(IDENTITY_KEY, ADMIN_ID)),
            raw_csrf="csrf-token",
            csrf_header=csrf_header,
            origin=origin,
            idempotency_key=idempotency_key,
        )

    assert caught.value.code == expected_code
