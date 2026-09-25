"""Mobile session checks and single-device logout; HTTP routing remains in C12."""

import hmac
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from shittim_records.auth import AuthFailure, AvatarStore, _digest, _utc
from shittim_records.contracts import ImageAvatarRef, PlaceholderAvatarRef, SessionUser
from shittim_records.mobile_auth import MobileSessionRecord, MobileSessionResponse, cache_account_id


class MobileSessionReader(Protocol):
    def get_session(self, *, session_hash: str) -> MobileSessionRecord | None: ...


class MobileSessionStore(MobileSessionReader, Protocol):
    def delete_session(self, *, session_hash: str) -> None: ...


def authenticate_mobile_session(
    *,
    store: MobileSessionReader,
    session_key: bytes,
    raw_token: str | None,
    clock: Callable[[], datetime],
) -> MobileSessionRecord | None:
    """Shared read-only authentication, without avatar signing or administrator configuration."""

    if raw_token is None or re.fullmatch(r"[A-Za-z0-9_-]{43}", raw_token) is None:
        return None
    session = store.get_session(session_hash=_digest(session_key, "mobile-session", raw_token))
    # Read the clock after storage I/O so a delayed read cannot extend authorization.
    now_epoch = int(_utc(clock()).timestamp())
    if session is None or not session.created_at <= now_epoch < session.expires_at:
        return None
    return session


class MobileSessionService:
    def __init__(
        self,
        *,
        store: MobileSessionStore,
        avatars: AvatarStore,
        session_key: bytes,
        admin_requester_key: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(session_key) < 32:
            raise AuthFailure("configuration_invalid")
        self._store = store
        self._avatars = avatars
        self._session_key = session_key
        self._admin_requester_key = admin_requester_key
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def authenticate(self, *, raw_token: str | None) -> MobileSessionRecord | None:
        """Check every request without extending expiry or relying on TTL cleanup."""

        return authenticate_mobile_session(
            store=self._store,
            session_key=self._session_key,
            raw_token=raw_token,
            clock=self._clock,
        )

    def session(self, *, raw_token: str | None) -> MobileSessionResponse:
        session = self.authenticate(raw_token=raw_token)
        if session is None:
            raise AuthFailure("session_required")
        avatar = (
            PlaceholderAvatarRef(
                kind="placeholder", alt=f"{session.display_name}のアバター", fallback_variant="cyan"
            )
            if session.avatar_asset_key is None
            else ImageAvatarRef(
                kind="image",
                url=self._avatars.requester_avatar_url(object_key=session.avatar_asset_key),
                alt=f"{session.display_name}のアバター",
                fallback_variant="cyan",
            )
        )
        response = MobileSessionResponse.model_validate(
            {
                "schemaVersion": 1,
                "cacheAccountId": cache_account_id(self._session_key, session.requester_key),
                "user": SessionUser(display_name=session.display_name, avatar=avatar),
                "isAdmin": hmac.compare_digest(self._admin_requester_key, session.requester_key),
                "expiresAt": datetime.fromtimestamp(session.expires_at, UTC),
            }
        )
        if not session.created_at <= int(_utc(self._clock()).timestamp()) < session.expires_at:
            raise AuthFailure("session_required")
        return response

    def logout(self, *, raw_token: str | None) -> None:
        """Require a live token and wait for its deletion; never issue replacement tokens."""

        if raw_token is None or self.authenticate(raw_token=raw_token) is None:
            raise AuthFailure("session_required")
        self._store.delete_session(
            session_hash=_digest(self._session_key, "mobile-session", raw_token)
        )
