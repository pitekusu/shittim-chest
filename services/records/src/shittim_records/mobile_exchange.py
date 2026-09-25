"""Exchange a device-bound code for a mobile session; public routing remains in C12."""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from shittim_records.auth import AuthFailure, AvatarStore, _digest, _utc
from shittim_records.contracts import ImageAvatarRef, PlaceholderAvatarRef, SessionUser
from shittim_records.mobile_auth import (
    MOBILE_SESSION_TTL_SECONDS,
    MobileAuthorizedTransaction,
    MobileExchangeRequest,
    MobileExchangeResponse,
    MobileSessionRecord,
    MobileTransactionState,
    cache_account_id,
)


class MobileExchangeStore(Protocol):
    def get(self, transaction_hash: str, *, now_epoch: int) -> MobileTransactionState: ...

    def issue_session(
        self,
        expected: MobileAuthorizedTransaction,
        *,
        session_hash: str,
        session: MobileSessionRecord,
        now_epoch: int,
    ) -> None: ...


class MobileExchangeService:
    def __init__(
        self,
        *,
        store: MobileExchangeStore,
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

    def exchange(self, request: MobileExchangeRequest) -> MobileExchangeResponse:
        now_epoch = int(_utc(self._clock()).timestamp())
        transaction_hash = _digest(self._session_key, "mobile-transaction", request.transaction_id)
        grant = self._store.get(transaction_hash, now_epoch=now_epoch)
        if not isinstance(grant, MobileAuthorizedTransaction) or (
            grant.transaction_hash != transaction_hash
        ):
            raise AuthFailure("mobile_grant_invalid")
        _require_live_code(grant, now_epoch)
        code_matches = hmac.compare_digest(
            grant.code_hash, _digest(self._session_key, "mobile-code", request.code)
        )
        # RFC 7636 S256: hash the ASCII verifier, then base64url without padding.
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        verifier_matches = hmac.compare_digest(grant.code_challenge, challenge)
        if not code_matches or not verifier_matches:
            raise AuthFailure("mobile_grant_invalid")

        # Build the response before consuming the code: avatar/validation failure is retryable.
        avatar = (
            PlaceholderAvatarRef(
                kind="placeholder", alt=f"{grant.display_name}のアバター", fallback_variant="cyan"
            )
            if grant.avatar_asset_key is None
            else ImageAvatarRef(
                kind="image",
                url=self._avatars.requester_avatar_url(object_key=grant.avatar_asset_key),
                alt=f"{grant.display_name}のアバター",
                fallback_variant="cyan",
            )
        )
        now_epoch = int(_utc(self._clock()).timestamp())
        _require_live_code(grant, now_epoch)
        raw_token = secrets.token_urlsafe(32)
        session = MobileSessionRecord(
            requester_key=grant.requester_key,
            display_name=grant.display_name,
            avatar_asset_key=grant.avatar_asset_key,
            guild_verified_at=grant.guild_verified_at,
            created_at=now_epoch,
            expires_at=now_epoch + MOBILE_SESSION_TTL_SECONDS,
        )
        response = MobileExchangeResponse.model_validate(
            {
                "schemaVersion": 1,
                "cacheAccountId": cache_account_id(self._session_key, session.requester_key),
                "accessToken": raw_token,
                "tokenType": "Bearer",
                "expiresAt": datetime.fromtimestamp(session.expires_at, UTC),
                "user": SessionUser(display_name=session.display_name, avatar=avatar),
                "isAdmin": hmac.compare_digest(self._admin_requester_key, session.requester_key),
                "returnTo": grant.return_to,
            }
        )
        self._store.issue_session(
            grant,
            session_hash=_digest(self._session_key, "mobile-session", raw_token),
            session=session,
            now_epoch=now_epoch,
        )
        return response


def _require_live_code(grant: MobileAuthorizedTransaction, now_epoch: int) -> None:
    if not (
        grant.created_at <= now_epoch < grant.expires_at
        and grant.code_issued_at <= now_epoch < grant.code_expires_at
    ):
        raise AuthFailure("mobile_grant_invalid")
