"""Mobile login initiation and browser binding, without public routes or token exchange."""

import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlencode

from shittim_records.auth import (
    DISCORD_AUTHORIZE_URL,
    AuthFailure,
    RecordsOAuthConfig,
    _cookie,
    _digest,
    _utc,
)
from shittim_records.mobile_auth import (
    MOBILE_TRANSACTION_TTL_SECONDS,
    MobileAuthorizedTransaction,
    MobileAuthorizeRequest,
    MobileAuthorizingTransaction,
    MobileStartedTransaction,
    MobileStartRequest,
    MobileStartResponse,
    MobileTransactionState,
)

MOBILE_OAUTH_COOKIE_NAME = "__Host-shittim-records-mobile-oauth"
_CALLBACK_STATE = re.compile(r"m\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})")


class MobileLoginStore(Protocol):
    """C06 storage boundary; no SDK or session-writing authority in this service."""

    def create(self, state: MobileStartedTransaction, *, now_epoch: int) -> None: ...

    def get(self, transaction_hash: str, *, now_epoch: int) -> MobileTransactionState: ...

    def advance(
        self,
        expected: MobileStartedTransaction | MobileAuthorizingTransaction,
        replacement: MobileAuthorizingTransaction | MobileAuthorizedTransaction,
        *,
        now_epoch: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class MobileBrowserAuthorization:
    # Both the redirect's state and the Set-Cookie value are private, including in repr.
    location: str = field(repr=False)
    oauth_cookie: str = field(repr=False)


class MobileLoginService:
    def __init__(
        self, *, store: MobileLoginStore, oauth: RecordsOAuthConfig, hmac_key: bytes
    ) -> None:
        if len(hmac_key) < 32:
            raise AuthFailure("configuration_invalid")
        self._store = store
        self._oauth = oauth
        self._hmac_key = hmac_key

    def begin(self, request: MobileStartRequest, *, now: datetime) -> MobileStartResponse:
        now_epoch = int(_utc(now).timestamp())
        transaction_id = secrets.token_urlsafe(32)
        state = MobileStartedTransaction(
            status="started",
            transaction_hash=_digest(self._hmac_key, "mobile-transaction", transaction_id),
            code_challenge=request.code_challenge,
            client_state=request.state,
            return_to=request.return_to,
            created_at=now_epoch,
            expires_at=now_epoch + MOBILE_TRANSACTION_TTL_SECONDS,
        )
        self._store.create(state, now_epoch=now_epoch)
        return MobileStartResponse.model_validate(
            {
                "schemaVersion": 1,
                "transactionId": transaction_id,
                "authorizePath": f"/api/v1/auth/mobile/authorize?transaction={transaction_id}",
                "expiresAt": datetime.fromtimestamp(state.expires_at, UTC),
            }
        )

    def authorize(
        self, request: MobileAuthorizeRequest, *, now: datetime
    ) -> MobileBrowserAuthorization:
        now_epoch = int(_utc(now).timestamp())
        state = self._load(request.transaction, now_epoch)
        if not isinstance(state, MobileStartedTransaction):
            raise AuthFailure("mobile_grant_invalid")
        browser_nonce = secrets.token_urlsafe(32)
        # C08 can route the shared Discord callback and locate the transaction
        # without a new index. The prefix/transaction alone never prove identity.
        oauth_state = f"m.{request.transaction}.{secrets.token_urlsafe(32)}"
        authorizing = MobileAuthorizingTransaction(
            **state.model_dump(exclude={"status"}),
            status="authorizing",
            browser_nonce_hash=_digest(self._hmac_key, "mobile-browser-nonce", browser_nonce),
            oauth_state_hash=_digest(self._hmac_key, "mobile-oauth-state", oauth_state),
        )
        self._store.advance(state, authorizing, now_epoch=now_epoch)
        query = urlencode(
            {
                "client_id": self._oauth.client_id,
                "redirect_uri": self._oauth.oauth_callback_url,
                "response_type": "code",
                "scope": "identify guilds.members.read",
                "state": oauth_state,
            }
        )
        return MobileBrowserAuthorization(
            location=f"{DISCORD_AUTHORIZE_URL}?{query}",
            oauth_cookie=_cookie(
                MOBILE_OAUTH_COOKIE_NAME, browser_nonce, max_age=state.expires_at - now_epoch
            ),
        )

    def validate_callback(
        self, *, oauth_state: str, browser_nonce: str | None, now: datetime
    ) -> tuple[str, MobileAuthorizingTransaction]:
        """Verify browser binding before C08 calls Discord; this does not consume it.

        C08 must still advance the returned snapshot with C06 CAS after identity
        verification. Merely validating a callback is not one-time code issuance.
        """

        match = _CALLBACK_STATE.fullmatch(oauth_state)
        if (
            match is None
            or browser_nonce is None
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", browser_nonce) is None
        ):
            raise AuthFailure("mobile_grant_invalid")
        transaction_id = match[1]
        state = self._load(transaction_id, int(_utc(now).timestamp()))
        if not isinstance(state, MobileAuthorizingTransaction):
            raise AuthFailure("mobile_grant_invalid")
        valid_state = hmac.compare_digest(
            state.oauth_state_hash, _digest(self._hmac_key, "mobile-oauth-state", oauth_state)
        )
        valid_browser = hmac.compare_digest(
            state.browser_nonce_hash,
            _digest(self._hmac_key, "mobile-browser-nonce", browser_nonce),
        )
        if not valid_state or not valid_browser:
            raise AuthFailure("mobile_grant_invalid")
        return transaction_id, state

    def _load(self, transaction_id: str, now_epoch: int) -> MobileTransactionState:
        transaction_hash = _digest(self._hmac_key, "mobile-transaction", transaction_id)
        state = self._store.get(transaction_hash, now_epoch=now_epoch)
        if (
            state.transaction_hash != transaction_hash
            or not state.created_at <= now_epoch < state.expires_at
        ):
            raise AuthFailure("mobile_grant_invalid")
        return state
