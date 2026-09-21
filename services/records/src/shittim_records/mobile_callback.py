"""Internal Discord-to-app handoff. No public routing or session issuance until C09-C12."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlencode

from shittim_records.auth import (
    AuthConfiguration,
    AuthFailure,
    AvatarStore,
    DiscordOAuth,
    _clear_cookie,
    _digest,
    _utc,
    authenticate_discord_requester,
)
from shittim_records.mobile_auth import (
    MOBILE_CALLBACK_PATH,
    MOBILE_CODE_TTL_SECONDS,
    MobileAuthorizedTransaction,
    MobileCallbackParameters,
    MobileTransaction,
)
from shittim_records.mobile_login import (
    MOBILE_OAUTH_COOKIE_NAME,
    MobileLoginService,
    MobileLoginStore,
)


@dataclass(frozen=True, slots=True)
class MobileOAuthCompletion:
    location: str = field(repr=False)
    clear_oauth_cookie: str


class MobileCallbackService:
    def __init__(
        self,
        *,
        store: MobileLoginStore,
        discord: DiscordOAuth,
        avatars: AvatarStore,
        configuration: AuthConfiguration,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._discord = discord
        self._avatars = avatars
        self._configuration = configuration
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._login = MobileLoginService(
            store=store, oauth=configuration.oauth, hmac_key=configuration.session_hmac_key
        )

    def complete(
        self, *, code: str, state: str, browser_nonce: str | None
    ) -> MobileOAuthCompletion:
        if not code:
            raise AuthFailure("mobile_grant_invalid")
        transaction_id, binding = self._login.validate_callback(
            oauth_state=state, browser_nonce=browser_nonce, now=self._clock()
        )
        requester = authenticate_discord_requester(
            code=code,
            discord=self._discord,
            avatars=self._avatars,
            configuration=self._configuration,
        )
        # Network/avatar I/O can outlive the transaction. Never issue a code from stale time.
        verified_at = _utc(self._clock())
        now_epoch = int(verified_at.timestamp())
        if not binding.created_at <= now_epoch < binding.expires_at:
            raise AuthFailure("mobile_grant_invalid")
        raw_code = secrets.token_urlsafe(32)
        authorized = MobileAuthorizedTransaction(
            **binding.model_dump(include=set(MobileTransaction.model_fields)),
            status="authorized",
            code_hash=_digest(self._configuration.session_hmac_key, "mobile-code", raw_code),
            code_issued_at=now_epoch,
            code_expires_at=min(now_epoch + MOBILE_CODE_TTL_SECONDS, binding.expires_at),
            requester_key=requester.requester_key,
            display_name=requester.display_name,
            avatar_asset_key=requester.avatar_asset_key,
            guild_verified_at=verified_at,
        )
        # Only the CAS winner returns a grant. Storage failure must never produce a redirect.
        self._store.advance(binding, authorized, now_epoch=now_epoch)
        parameters = MobileCallbackParameters(
            transaction=transaction_id, code=raw_code, state=binding.client_state
        )
        query = urlencode(parameters.model_dump())
        return MobileOAuthCompletion(
            location=f"{self._configuration.oauth.allowed_origin}{MOBILE_CALLBACK_PATH}?{query}",
            clear_oauth_cookie=_clear_cookie(MOBILE_OAUTH_COOKIE_NAME),
        )
