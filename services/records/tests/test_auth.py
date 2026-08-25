"""Pure OAuth and session transition tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest

from shittim_records.archive import derive_requester_key
from shittim_records.auth import (
    AuthConfiguration,
    AuthFailure,
    AuthService,
    DiscordIdentity,
    DiscordTokens,
    OAuthState,
    RecordsOAuthConfig,
    SessionRecord,
    csrf_hash,
    session_hash,
    validate_return_to,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
IDENTITY_KEY = b"i" * 32
SESSION_KEY = b"s" * 32
CLIENT_ID = "1" * 18
GUILD_ID = "2" * 18
USER_ID = "3" * 18


class FakeStore:
    def __init__(self) -> None:
        self.oauth: dict[str, OAuthState] = {}
        self.claimed: set[str] = set()
        self.sessions: dict[str, SessionRecord] = {}

    def create_oauth_state(self, *, state_hash: str, state: OAuthState) -> None:
        assert state_hash not in self.oauth
        self.oauth[state_hash] = state

    def claim_oauth_state(
        self,
        *,
        state_hash: str,
        nonce_hash: str,
        now_epoch: int,
        claimed_at: str,
    ) -> OAuthState:
        del claimed_at
        state = self.oauth.get(state_hash)
        if (
            state is None
            or state_hash in self.claimed
            or state.nonce_hash != nonce_hash
            or state.expires_at <= now_epoch
        ):
            raise AuthFailure("oauth_state_invalid")
        self.claimed.add(state_hash)
        return state

    def create_session(
        self,
        *,
        session_hash: str,
        session: SessionRecord,
    ) -> None:
        self.sessions[session_hash] = session

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        return self.sessions.get(session_hash)

    def delete_session(self, *, session_hash: str) -> None:
        self.sessions.pop(session_hash, None)


class FakeDiscord:
    def __init__(self, *, avatar: bytes | None = b"webp") -> None:
        self.avatar = avatar
        self.codes: list[str] = []

    def exchange_code(self, *, code: str, configuration: AuthConfiguration) -> DiscordTokens:
        assert configuration.client_secret == "private-client-secret"  # noqa: S105
        self.codes.append(code)
        return DiscordTokens(
            access_token="private-access-token",  # noqa: S106 - inert test credential.
            token_type="Bearer",  # noqa: S106 - protocol value, not a credential.
        )

    def get_identity(self, *, tokens: DiscordTokens, guild_id: str) -> DiscordIdentity:
        assert tokens.access_token == "private-access-token"  # noqa: S105
        assert guild_id == GUILD_ID
        return DiscordIdentity(
            user_id=USER_ID,
            username="username",
            global_name="Global Name",
            user_avatar_hash="a" * 32,
            guild_nickname="Guild Nickname",
            guild_avatar_hash="b" * 32,
        )

    def fetch_avatar(self, *, identity: DiscordIdentity, guild_id: str) -> bytes | None:
        assert identity.guild_avatar_hash == "b" * 32
        assert guild_id == GUILD_ID
        return self.avatar


class FakeAvatars:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.objects: dict[str, bytes] = {}

    def put_requester_avatar(self, *, object_key: str, body: bytes) -> None:
        if self.fail:
            raise OSError("private storage unavailable")
        self.objects[object_key] = body

    def requester_avatar_url(self, *, object_key: str) -> str:
        return f"https://media.example.invalid/{object_key}"


def configuration() -> AuthConfiguration:
    return AuthConfiguration(
        identity_hmac_key=IDENTITY_KEY,
        session_hmac_key=SESSION_KEY,
        admin_requester_key=derive_requester_key(IDENTITY_KEY, USER_ID),
        oauth=RecordsOAuthConfig(
            schema_version=1,
            client_id=CLIENT_ID,
            guild_id=GUILD_ID,
            allowed_origin="https://records.example.invalid",
            oauth_callback_url=("https://records.example.invalid/api/v1/auth/discord/callback"),
        ),
        client_secret="private-client-secret",  # noqa: S106 - inert test credential.
    )


def service(
    *,
    store: FakeStore | None = None,
    discord: FakeDiscord | None = None,
    avatars: FakeAvatars | None = None,
) -> tuple[AuthService, FakeStore, FakeAvatars]:
    actual_store = store or FakeStore()
    actual_avatars = avatars or FakeAvatars()
    return (
        AuthService(
            store=actual_store,
            discord=discord or FakeDiscord(),
            avatars=actual_avatars,
            configuration=configuration(),
        ),
        actual_store,
        actual_avatars,
    )


def _cookie_value(cookie: str) -> str:
    return cookie.partition("=")[2].partition(";")[0]


def test_oauth_start_persists_only_hmacs_and_uses_exact_scope() -> None:
    auth, store, _avatars = service()

    result = auth.begin(return_to=f"/records/{'r' * 43}", now=NOW)

    query = parse_qs(urlsplit(result.location).query)
    raw_state = query["state"][0]
    raw_nonce = _cookie_value(result.oauth_cookie)
    assert query["scope"] == ["identify guilds.members.read"]
    assert query["redirect_uri"] == [configuration().oauth.oauth_callback_url]
    assert "Secure" in result.oauth_cookie
    assert "HttpOnly" in result.oauth_cookie
    assert "SameSite=Lax" in result.oauth_cookie
    assert raw_state not in repr(store.oauth)
    assert raw_nonce not in repr(store.oauth)
    assert raw_nonce not in repr(result)
    assert "private-client-secret" not in repr(configuration())
    assert len(store.oauth) == 1


@pytest.mark.parametrize("value", ("/records/short", "//evil.invalid", "/unknown"))
def test_return_to_rejects_paths_outside_the_spa_allowlist(value: str) -> None:
    with pytest.raises(AuthFailure) as caught:
        validate_return_to(value)

    assert caught.value.code == "return_to_invalid"


def test_admin_route_is_a_valid_oauth_return_path() -> None:
    assert validate_return_to("/admin") == "/admin"


def test_callback_claims_once_and_stores_only_hashed_session_values() -> None:
    auth, store, avatars = service()
    started = auth.begin(return_to="/insights", now=NOW)
    query = parse_qs(urlsplit(started.location).query)
    raw_state = query["state"][0]
    raw_nonce = _cookie_value(started.oauth_cookie)

    completed = auth.complete(
        code="one-time-code",
        state=raw_state,
        browser_nonce=raw_nonce,
        now=NOW + timedelta(seconds=1),
    )

    raw_session = _cookie_value(completed.session_cookie)
    raw_csrf = _cookie_value(completed.csrf_cookie)
    expected_requester = derive_requester_key(IDENTITY_KEY, USER_ID)
    stored = store.sessions[session_hash(SESSION_KEY, raw_session)]
    assert stored.requester_key == expected_requester
    assert stored.display_name == "Guild Nickname"
    assert stored.csrf_hash == csrf_hash(SESSION_KEY, raw_csrf)
    assert stored.avatar_asset_key == f"requesters/{expected_requester}/avatar.webp"
    assert avatars.objects[stored.avatar_asset_key] == b"webp"
    assert raw_session not in repr(store.sessions)
    assert raw_csrf not in repr(store.sessions)
    assert raw_session not in repr(completed)
    assert raw_csrf not in repr(completed)
    assert completed.location == "https://records.example.invalid/insights"
    assert "HttpOnly" in completed.session_cookie
    assert "HttpOnly" not in completed.csrf_cookie
    with pytest.raises(AuthFailure) as caught:
        auth.complete(
            code="another-code",
            state=raw_state,
            browser_nonce=raw_nonce,
            now=NOW + timedelta(seconds=2),
        )
    assert caught.value.code == "oauth_state_invalid"


def test_avatar_failure_falls_back_without_failing_login() -> None:
    auth, store, _avatars = service(avatars=FakeAvatars(fail=True))
    started = auth.begin(return_to="/", now=NOW)
    query = parse_qs(urlsplit(started.location).query)

    completed = auth.complete(
        code="code",
        state=query["state"][0],
        browser_nonce=_cookie_value(started.oauth_cookie),
        now=NOW,
    )

    stored = store.sessions[session_hash(SESSION_KEY, _cookie_value(completed.session_cookie))]
    assert stored.avatar_asset_key is None


def test_expired_session_is_rejected_before_ttl_deletion_and_logout_checks_csrf() -> None:
    auth, store, _avatars = service()
    raw_session = "session-token"
    raw_csrf = "csrf-token"
    store.sessions[session_hash(SESSION_KEY, raw_session)] = SessionRecord(
        requester_key="requester-key",
        display_name="Requester",
        avatar_asset_key=None,
        csrf_hash=csrf_hash(SESSION_KEY, raw_csrf),
        guild_verified_at=NOW.isoformat(),
        expires_at=int((NOW + timedelta(minutes=1)).timestamp()),
    )

    assert auth.authenticate(raw_session=raw_session, now=NOW) is not None
    assert auth.authenticate(raw_session=raw_session, now=NOW + timedelta(minutes=2)) is None
    with pytest.raises(AuthFailure) as caught:
        auth.logout(
            raw_session=raw_session,
            raw_csrf=raw_csrf,
            csrf_header="wrong",
            origin="https://records.example.invalid",
            now=NOW,
        )
    assert caught.value.code == "csrf_invalid"

    cleared = auth.logout(
        raw_session=raw_session,
        raw_csrf=raw_csrf,
        csrf_header=raw_csrf,
        origin="https://records.example.invalid",
        now=NOW,
    )
    assert all("Max-Age=0" in item for item in cleared)
    assert store.sessions == {}


def test_avatar_url_rejects_non_requester_keys() -> None:
    auth, _store, _avatars = service()
    assert auth.avatar_url(asset_key="requesters/opaque/avatar.webp").startswith("https://")
    with pytest.raises(AuthFailure):
        auth.avatar_url(asset_key=cast(str, "participants/a.webp"))
