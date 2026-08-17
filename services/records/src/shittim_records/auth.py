"""Pure OAuth, session, and authorization services for Records."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shittim_records.archive import derive_requester_key

OAUTH_COOKIE_NAME = "__Host-shittim-records-oauth"
SESSION_COOKIE_NAME = "__Host-shittim-records-session"
CSRF_COOKIE_NAME = "__Host-shittim-records-csrf"
OAUTH_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(hours=12)
PROFILE_TTL = timedelta(days=30)
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


class AuthFailure(RuntimeError):
    """Stable authentication failure that contains no private value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RecordsOAuthConfig(BaseModel):
    """Exact private OAuth configuration loaded from SSM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1, le=1)
    client_id: str = Field(pattern=r"^[0-9]{17,20}$")
    guild_id: str = Field(pattern=r"^[0-9]{17,20}$")
    allowed_origin: str = Field(pattern=r"^https://[a-z0-9.-]+$")
    oauth_callback_url: str

    @model_validator(mode="after")
    def require_callback_on_allowed_origin(self) -> RecordsOAuthConfig:
        try:
            parsed = urlsplit(self.allowed_origin)
            port = parsed.port
        except ValueError as error:
            raise ValueError("Records origin is invalid") from error
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or hostname is None
            or HOSTNAME_PATTERN.fullmatch(hostname.lower()) is None
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.path
            or parsed.query
            or parsed.fragment
            or self.allowed_origin != f"https://{hostname.lower()}"
        ):
            raise ValueError("Records origin is invalid")
        expected = f"{self.allowed_origin}/api/v1/auth/discord/callback"
        if self.oauth_callback_url != expected:
            raise ValueError("OAuth callback must use the configured Records origin")
        return self


@dataclass(frozen=True, slots=True)
class AuthConfiguration:
    identity_hmac_key: bytes = field(repr=False)
    session_hmac_key: bytes = field(repr=False)
    oauth: RecordsOAuthConfig
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthState:
    nonce_hash: str
    return_to: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    requester_key: str
    display_name: str
    avatar_asset_key: str | None
    csrf_hash: str
    guild_verified_at: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class DiscordTokens:
    access_token: str = field(repr=False)
    token_type: str


@dataclass(frozen=True, slots=True)
class DiscordIdentity:
    user_id: str
    username: str
    global_name: str | None
    user_avatar_hash: str | None
    guild_nickname: str | None
    guild_avatar_hash: str | None


@dataclass(frozen=True, slots=True)
class OAuthStart:
    location: str
    oauth_cookie: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthCompletion:
    location: str
    session_cookie: str = field(repr=False)
    csrf_cookie: str = field(repr=False)
    clear_oauth_cookie: str


class AuthStore(Protocol):
    def create_oauth_state(self, *, state_hash: str, state: OAuthState) -> None: ...

    def claim_oauth_state(
        self,
        *,
        state_hash: str,
        nonce_hash: str,
        now_epoch: int,
        claimed_at: str,
    ) -> OAuthState: ...

    def create_session(
        self,
        *,
        session_hash: str,
        session: SessionRecord,
        profile_expires_at: int,
    ) -> None: ...

    def get_session(self, *, session_hash: str) -> SessionRecord | None: ...

    def delete_session(self, *, session_hash: str) -> None: ...


class DiscordOAuth(Protocol):
    def exchange_code(self, *, code: str, configuration: AuthConfiguration) -> DiscordTokens: ...

    def get_identity(
        self,
        *,
        tokens: DiscordTokens,
        guild_id: str,
    ) -> DiscordIdentity: ...

    def fetch_avatar(self, *, identity: DiscordIdentity, guild_id: str) -> bytes | None: ...


class AvatarStore(Protocol):
    def put_requester_avatar(self, *, object_key: str, body: bytes) -> None: ...

    def requester_avatar_url(self, *, object_key: str) -> str: ...


class AuthService:
    """Execute OAuth and session transitions without leaking browser secrets."""

    def __init__(
        self,
        *,
        store: AuthStore,
        discord: DiscordOAuth,
        avatars: AvatarStore,
        configuration: AuthConfiguration,
    ) -> None:
        self._store = store
        self._discord = discord
        self._avatars = avatars
        self._configuration = configuration

    @property
    def allowed_origin(self) -> str:
        return self._configuration.oauth.allowed_origin

    @property
    def session_hmac_key(self) -> bytes:
        return self._configuration.session_hmac_key

    def begin(self, *, return_to: str | None, now: datetime) -> OAuthStart:
        now = _utc(now)
        safe_return_to = validate_return_to(return_to)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        state_hash = _digest(self._configuration.session_hmac_key, "oauth-state", state)
        nonce_hash = _digest(self._configuration.session_hmac_key, "oauth-nonce", nonce)
        self._store.create_oauth_state(
            state_hash=state_hash,
            state=OAuthState(
                nonce_hash=nonce_hash,
                return_to=safe_return_to,
                expires_at=int((now + OAUTH_TTL).timestamp()),
            ),
        )
        query = urlencode(
            {
                "client_id": self._configuration.oauth.client_id,
                "redirect_uri": self._configuration.oauth.oauth_callback_url,
                "response_type": "code",
                "scope": "identify guilds.members.read",
                "state": state,
            }
        )
        return OAuthStart(
            location=f"{DISCORD_AUTHORIZE_URL}?{query}",
            oauth_cookie=_cookie(OAUTH_COOKIE_NAME, nonce, max_age=int(OAUTH_TTL.total_seconds())),
        )

    def complete(
        self,
        *,
        code: str,
        state: str,
        browser_nonce: str,
        now: datetime,
    ) -> OAuthCompletion:
        now = _utc(now)
        if not code or not state or not browser_nonce:
            raise AuthFailure("oauth_request_invalid")
        session_key = self._configuration.session_hmac_key
        claimed = self._store.claim_oauth_state(
            state_hash=_digest(session_key, "oauth-state", state),
            nonce_hash=_digest(session_key, "oauth-nonce", browser_nonce),
            now_epoch=int(now.timestamp()),
            claimed_at=now.isoformat(),
        )
        tokens = self._discord.exchange_code(code=code, configuration=self._configuration)
        identity = self._discord.get_identity(
            tokens=tokens,
            guild_id=self._configuration.oauth.guild_id,
        )
        display_name = (
            identity.guild_nickname or identity.global_name or identity.username
        ).strip()
        if not display_name:
            raise AuthFailure("discord_identity_invalid")
        requester_key = derive_requester_key(
            self._configuration.identity_hmac_key,
            identity.user_id,
        )
        avatar_asset_key = self._cache_avatar(identity=identity, requester_key=requester_key)
        raw_session = secrets.token_urlsafe(32)
        raw_csrf = secrets.token_urlsafe(32)
        expires_at = int((now + SESSION_TTL).timestamp())
        self._store.create_session(
            session_hash=_digest(session_key, "session", raw_session),
            session=SessionRecord(
                requester_key=requester_key,
                display_name=display_name,
                avatar_asset_key=avatar_asset_key,
                csrf_hash=_digest(session_key, "csrf", raw_csrf),
                guild_verified_at=now.isoformat(),
                expires_at=expires_at,
            ),
            profile_expires_at=int((now + PROFILE_TTL).timestamp()),
        )
        return OAuthCompletion(
            location=f"{self._configuration.oauth.allowed_origin}{claimed.return_to}",
            session_cookie=_cookie(
                SESSION_COOKIE_NAME,
                raw_session,
                max_age=int(SESSION_TTL.total_seconds()),
            ),
            csrf_cookie=_cookie(
                CSRF_COOKIE_NAME,
                raw_csrf,
                max_age=int(SESSION_TTL.total_seconds()),
                http_only=False,
            ),
            clear_oauth_cookie=_clear_cookie(OAUTH_COOKIE_NAME),
        )

    def authenticate(self, *, raw_session: str | None, now: datetime) -> SessionRecord | None:
        if not raw_session:
            return None
        session = self._store.get_session(
            session_hash=_digest(self._configuration.session_hmac_key, "session", raw_session)
        )
        if session is None or session.expires_at <= int(_utc(now).timestamp()):
            return None
        return session

    def avatar_url(self, *, asset_key: str) -> str:
        """Return a short-lived URL only for a validated requester asset key."""

        if not _valid_requester_asset_key(asset_key):
            raise AuthFailure("session_record_invalid")
        return self._avatars.requester_avatar_url(object_key=asset_key)

    def logout(
        self,
        *,
        raw_session: str | None,
        raw_csrf: str | None,
        csrf_header: str | None,
        origin: str | None,
        now: datetime,
    ) -> tuple[str, str]:
        if origin != self._configuration.oauth.allowed_origin:
            raise AuthFailure("origin_invalid")
        session = self.authenticate(raw_session=raw_session, now=now)
        if session is None or not raw_csrf or not csrf_header:
            raise AuthFailure("session_required")
        if not hmac.compare_digest(raw_csrf, csrf_header):
            raise AuthFailure("csrf_invalid")
        expected = _digest(self._configuration.session_hmac_key, "csrf", raw_csrf)
        if not hmac.compare_digest(expected, session.csrf_hash):
            raise AuthFailure("csrf_invalid")
        if raw_session is None:
            raise AuthFailure("session_required")
        self._store.delete_session(
            session_hash=_digest(self._configuration.session_hmac_key, "session", raw_session)
        )
        return _clear_cookie(SESSION_COOKIE_NAME), _clear_cookie(
            CSRF_COOKIE_NAME,
            http_only=False,
        )

    def _cache_avatar(
        self,
        *,
        identity: DiscordIdentity,
        requester_key: str,
    ) -> str | None:
        try:
            body = self._discord.fetch_avatar(
                identity=identity,
                guild_id=self._configuration.oauth.guild_id,
            )
            if body is None:
                return None
            object_key = f"requesters/{requester_key}/avatar.webp"
            self._avatars.put_requester_avatar(object_key=object_key, body=body)
            return object_key
        except Exception:
            return None


def validate_return_to(value: str | None) -> str:
    """Allow only the three SPA route shapes owned by Records."""

    if value in (None, "", "/", "/insights"):
        return value if value else "/"
    prefix = "/records/"
    if value.startswith(prefix) and _is_record_id(value.removeprefix(prefix)):
        return value
    raise AuthFailure("return_to_invalid")


def session_hash(key: bytes, raw_session: str) -> str:
    """Return the stable session lookup key for an opaque browser token."""

    return _digest(key, "session", raw_session)


def csrf_hash(key: bytes, raw_csrf: str) -> str:
    """Return the stable CSRF verifier for an opaque browser token."""

    return _digest(key, "csrf", raw_csrf)


def _is_record_id(value: str) -> bool:
    return len(value) == 43 and all(character.isalnum() or character in "_-" for character in value)


def _valid_requester_asset_key(value: str) -> bool:
    return (
        value.startswith("requesters/")
        and len(value) <= 256
        and ".." not in value
        and all(character.isalnum() or character in "/._-" for character in value)
    )


def _digest(key: bytes, domain: str, value: str) -> str:
    if len(key) < 32:
        raise AuthFailure("configuration_invalid")
    return hmac.new(key, f"records:{domain}:{value}".encode(), hashlib.sha256).hexdigest()


def _cookie(name: str, value: str, *, max_age: int, http_only: bool = True) -> str:
    attributes = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "Secure", "SameSite=Lax"]
    if http_only:
        attributes.append("HttpOnly")
    return "; ".join(attributes)


def _clear_cookie(name: str, *, http_only: bool = True) -> str:
    return _cookie(name, "", max_age=0, http_only=http_only)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
