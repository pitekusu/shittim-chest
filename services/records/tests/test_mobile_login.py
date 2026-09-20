"""C07 browser binding; no Discord calls, tokens, public HTTP routes or AWS writes."""

from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.test_auth import configuration

from shittim_records.auth import OAUTH_COOKIE_NAME, AuthFailure, _digest
from shittim_records.mobile_auth import (
    MobileAuthorizedTransaction,
    MobileAuthorizeRequest,
    MobileAuthorizingTransaction,
    MobileConsumedTransaction,
    MobileStartedTransaction,
    MobileStartRequest,
    MobileTransaction,
    MobileTransactionState,
)
from shittim_records.mobile_login import MOBILE_OAUTH_COOKIE_NAME, MobileLoginService

NOW = datetime(2026, 9, 20, tzinfo=UTC)
KEY = b"test-only-hmac-key" * 2
REQUEST = MobileStartRequest.model_validate(
    {"codeChallenge": "c" * 43, "codeChallengeMethod": "S256", "state": "s" * 43}
)


class MemoryStore:
    def __init__(self) -> None:
        self.states: dict[str, MobileTransactionState] = {}
        self.fail_advance = False

    def create(self, state: MobileStartedTransaction, *, now_epoch: int) -> None:
        assert state.transaction_hash not in self.states
        self.states[state.transaction_hash] = state

    def get(self, transaction_hash: str, *, now_epoch: int) -> MobileTransactionState:
        if transaction_hash not in self.states:
            raise AuthFailure("mobile_grant_invalid")
        return self.states[transaction_hash]

    def advance(
        self,
        expected: MobileStartedTransaction | MobileAuthorizingTransaction,
        replacement: MobileAuthorizingTransaction | MobileAuthorizedTransaction,
        *,
        now_epoch: int,
    ) -> None:
        if self.fail_advance or self.states.get(expected.transaction_hash) != expected:
            raise AuthFailure("mobile_grant_invalid")
        self.states[expected.transaction_hash] = replacement


@pytest.fixture
def login() -> tuple[MobileLoginService, MemoryStore]:
    store = MemoryStore()
    return MobileLoginService(store=store, oauth=configuration().oauth, hmac_key=KEY), store


def test_begin_preserves_device_binding_and_only_stores_domain_separated_hash(login) -> None:
    service, store = login
    first = service.begin(REQUEST, now=NOW)
    second = service.begin(REQUEST, now=NOW)
    assert first.transaction_id != second.transaction_id
    assert first.expires_at == NOW + timedelta(minutes=10)
    assert (
        first.authorize_path == "/api/v1/auth/mobile/authorize?transaction=" + first.transaction_id
    )
    state = store.states[_digest(KEY, "mobile-transaction", first.transaction_id)]
    assert state.code_challenge == REQUEST.code_challenge
    assert state.client_state == REQUEST.state
    assert state.return_to == "/"
    assert state.transaction_hash != _digest(KEY, "oauth-state", first.transaction_id)
    assert first.transaction_id not in state.model_dump_json()
    assert first.transaction_id not in repr(first)


def test_authorize_claims_once_and_sets_a_separate_bounded_cookie(login) -> None:
    service, store = login
    started = service.begin(REQUEST, now=NOW)
    now = NOW + timedelta(seconds=599)
    request = MobileAuthorizeRequest(transaction=started.transaction_id)
    result = service.authorize(request, now=now)
    url = urlsplit(result.location)
    query = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path) == ("https", "discord.com", "/oauth2/authorize")
    assert query == {
        "client_id": [configuration().oauth.client_id],
        "redirect_uri": [configuration().oauth.oauth_callback_url],
        "response_type": ["code"],
        "scope": ["identify guilds.members.read"],
        "state": query["state"],
    }
    cookie = SimpleCookie(result.oauth_cookie)[MOBILE_OAUTH_COOKIE_NAME]
    assert cookie["secure"] and cookie["httponly"]
    assert cookie["samesite"] == "Lax" and cookie["path"] == "/"
    assert cookie["domain"] == "" and cookie["max-age"] == "1"
    assert MOBILE_OAUTH_COOKIE_NAME != OAUTH_COOKIE_NAME
    oauth_state = query["state"][0]
    state = store.states[_digest(KEY, "mobile-transaction", started.transaction_id)]
    assert isinstance(state, MobileAuthorizingTransaction)
    assert state.expires_at == int(started.expires_at.timestamp())
    assert state.browser_nonce_hash == _digest(KEY, "mobile-browser-nonce", cookie.value)
    assert state.oauth_state_hash == _digest(KEY, "mobile-oauth-state", oauth_state)
    assert REQUEST.state not in result.location and REQUEST.code_challenge not in result.location
    for private in (cookie.value, oauth_state, started.transaction_id):
        assert private not in repr(result)
        assert private not in state.model_dump_json()
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.authorize(request, now=now)


def test_callback_requires_matching_transaction_state_and_browser_without_consuming(login) -> None:
    service, store = login
    first = service.begin(REQUEST, now=NOW)
    second = service.begin(REQUEST, now=NOW)
    result = service.authorize(MobileAuthorizeRequest(transaction=first.transaction_id), now=NOW)
    second_result = service.authorize(
        MobileAuthorizeRequest(transaction=second.transaction_id), now=NOW
    )
    state = parse_qs(urlsplit(result.location).query)["state"][0]
    nonce = SimpleCookie(result.oauth_cookie)[MOBILE_OAUTH_COOKIE_NAME].value
    other_nonce = SimpleCookie(second_result.oauth_cookie)[MOBILE_OAUTH_COOKIE_NAME].value
    snapshots = dict(store.states)
    transaction_id, binding = service.validate_callback(
        oauth_state=state, browser_nonce=nonce, now=NOW
    )
    assert transaction_id == first.transaction_id
    assert binding.status == "authorizing"
    assert store.states == snapshots  # C08 will CAS after Discord identity verification.
    for bad_state, bad_cookie in (
        (state, None),
        (state, ""),
        (state, other_nonce),
        (state, "\r\n" + nonce),
        (REQUEST.state, nonce),
        ("m." + first.transaction_id + "." + "x" * 43, nonce),
        (state.replace(first.transaction_id, second.transaction_id), nonce),
        (state + "\n", nonce),
        (state + ".extra", nonce),
    ):
        with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
            service.validate_callback(oauth_state=bad_state, browser_nonce=bad_cookie, now=NOW)
    with pytest.raises(AuthFailure):
        service.validate_callback(oauth_state=state, browser_nonce=nonce, now=first.expires_at)
    assert store.states == snapshots

    store.states[binding.transaction_hash] = MobileConsumedTransaction(
        **binding.model_dump(include=set(MobileTransaction.model_fields)),
        status="consumed",
        consumed_at=int(NOW.timestamp()),
    )
    with pytest.raises(AuthFailure):
        service.validate_callback(oauth_state=state, browser_nonce=nonce, now=NOW)


def test_expiry_wrong_phase_and_storage_conflict_do_not_return_a_redirect(login) -> None:
    service, store = login
    started = service.begin(REQUEST, now=NOW)
    request = MobileAuthorizeRequest(transaction=started.transaction_id)
    for now in (NOW - timedelta(seconds=1), started.expires_at):
        with pytest.raises(AuthFailure):
            service.authorize(request, now=now)
    with pytest.raises(AuthFailure):
        service.validate_callback(
            oauth_state=f"m.{started.transaction_id}.{'x' * 43}", browser_nonce="b" * 43, now=NOW
        )
    store.fail_advance = True
    with pytest.raises(AuthFailure):
        service.authorize(request, now=NOW)
    assert all(state.status == "started" for state in store.states.values())
    with pytest.raises(AuthFailure):
        service.authorize(MobileAuthorizeRequest(transaction="z" * 43), now=NOW)


def test_weak_configuration_and_naive_time_fail_before_storage(login) -> None:
    service, store = login
    with pytest.raises(AuthFailure, match=r"^configuration_invalid$"):
        MobileLoginService(store=store, oauth=configuration().oauth, hmac_key=b"short")
    with pytest.raises(ValueError, match="timezone-aware"):
        service.begin(REQUEST, now=NOW.replace(tzinfo=None))
    assert not store.states
