"""C08 handoff boundaries using fake Discord/storage, plus existing Web regressions."""

from collections.abc import Callable
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.test_auth import USER_ID, FakeAvatars, FakeDiscord, configuration
from tests.test_mobile_login import NOW, REQUEST, MemoryStore

from shittim_records.auth import AuthFailure, _digest
from shittim_records.mobile_auth import (
    MobileAuthorizedTransaction,
    MobileAuthorizeRequest,
)
from shittim_records.mobile_callback import MobileCallbackService
from shittim_records.mobile_login import MOBILE_OAUTH_COOKIE_NAME, MobileLoginService


def prepare_callback(
    *, clock: Callable[[], datetime] = lambda: NOW, avatars: FakeAvatars | None = None
) -> tuple[MobileCallbackService, MemoryStore, FakeDiscord, dict[str, str]]:
    config = configuration()
    store = MemoryStore()
    discord = FakeDiscord()
    login = MobileLoginService(
        store=store, oauth=config.oauth, hmac_key=config.session_hmac_key, discord=discord
    )
    started = login.begin(REQUEST, now=NOW)
    browser = login.authorize(MobileAuthorizeRequest(transaction=started.transaction_id), now=NOW)
    callback = MobileCallbackService(
        store=store,
        discord=discord,
        avatars=avatars or FakeAvatars(),
        configuration=config,
        clock=clock,
    )
    return (
        callback,
        store,
        discord,
        {
            "code": "discord-one-time-code",
            "state": parse_qs(urlsplit(browser.location).query)["state"][0],
            "browser_nonce": SimpleCookie(browser.oauth_cookie)[MOBILE_OAUTH_COOKIE_NAME].value,
        },
    )


def test_handoff_issues_a_bounded_private_grant_once_and_never_a_session() -> None:
    times = iter(NOW + timedelta(seconds=seconds) for seconds in (1, 5, 6))
    avatars = FakeAvatars()
    service, store, discord, arguments = prepare_callback(clock=times.__next__, avatars=avatars)
    binding = next(iter(store.states.values()))

    result = service.complete(**arguments)

    url = urlsplit(result.location)
    query = parse_qs(url.query)
    assert (url.scheme, url.netloc, url.path, url.fragment) == (
        "https",
        "records.example.invalid",
        "/auth/mobile/callback",
        "",
    )
    assert set(query) == {"transaction", "code", "state"}
    assert query["transaction"] == [arguments["state"].split(".")[1]]
    assert query["state"] == [REQUEST.state]
    raw_code = query["code"][0]
    assert len(raw_code) == 43
    assert raw_code not in (query["transaction"][0], REQUEST.state, arguments["code"])
    stored = next(iter(store.states.values()))
    assert isinstance(stored, MobileAuthorizedTransaction)
    assert stored.code_hash == _digest(configuration().session_hmac_key, "mobile-code", raw_code)
    assert stored.code_issued_at == int((NOW + timedelta(seconds=5)).timestamp())
    assert stored.code_expires_at == stored.code_issued_at + 60
    assert stored.expires_at == binding.expires_at
    assert stored.code_challenge == binding.code_challenge
    assert stored.return_to == binding.return_to
    assert stored.guild_verified_at == NOW + timedelta(seconds=5)
    assert stored.display_name == "Guild Nickname"
    assert stored.requester_key == configuration().admin_requester_key
    assert stored.avatar_asset_key == f"requesters/{stored.requester_key}/avatar.webp"
    assert avatars.objects == {stored.avatar_asset_key: b"webp"}
    cookie = SimpleCookie(result.clear_oauth_cookie)[MOBILE_OAUTH_COOKIE_NAME]
    assert cookie.value == "" and cookie["max-age"] == "0"
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Lax"
    for private in (USER_ID, arguments["code"], "private-access-token", "private-client-secret"):
        assert private not in result.location + stored.model_dump_json() + repr(result)
    assert raw_code not in stored.model_dump_json() + repr(result)
    assert stored.display_name not in repr(stored)
    assert stored.requester_key not in repr(stored)
    # A repeated callback is rejected before another Discord exchange or code issuance.
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.complete(**arguments)
    assert discord.codes == [arguments["code"]]
    assert next(iter(store.states.values())) == stored


@pytest.mark.parametrize("field,value", [("code", ""), ("state", "invalid"), ("browser_nonce", "")])
def test_invalid_browser_handoff_never_calls_discord(field: str, value: str) -> None:
    service, store, discord, arguments = prepare_callback()
    before = dict(store.states)
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.complete(**{**arguments, field: value})
    assert not discord.codes
    assert store.states == before


@pytest.mark.parametrize(
    "method,category",
    [("exchange_code", "discord_token_invalid"), ("get_identity", "guild_membership_required")],
)
def test_discord_failure_does_not_authorize_or_return_a_code(monkeypatch, method, category) -> None:
    service, store, discord, arguments = prepare_callback()
    before = dict(store.states)

    def deny(**kwargs):
        raise AuthFailure(category)

    monkeypatch.setattr(discord, method, deny)
    with pytest.raises(AuthFailure, match=f"^{category}$"):
        service.complete(**arguments)
    assert store.states == before


def test_storage_conflict_never_returns_a_grant() -> None:
    service, store, _discord, arguments = prepare_callback()
    before = dict(store.states)
    store.fail_advance = True
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.complete(**arguments)
    assert store.states == before


@pytest.mark.parametrize("completion_second", [590, 600])
def test_network_delay_cannot_extend_or_outlive_transaction(completion_second: int) -> None:
    times = iter((NOW, NOW + timedelta(seconds=completion_second)))
    service, store, _discord, arguments = prepare_callback(clock=times.__next__)
    if completion_second == 600:
        before = dict(store.states)
        with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
            service.complete(**arguments)
        assert store.states == before
    else:
        service.complete(**arguments)
        stored = next(iter(store.states.values()))
        assert isinstance(stored, MobileAuthorizedTransaction)
        assert stored.code_expires_at == stored.expires_at
        assert stored.code_expires_at - stored.code_issued_at == 10


def test_avatar_failure_keeps_verified_login_without_storing_an_expiring_url() -> None:
    service, store, _discord, arguments = prepare_callback(avatars=FakeAvatars(fail=True))
    service.complete(**arguments)
    stored = next(iter(store.states.values()))
    assert isinstance(stored, MobileAuthorizedTransaction)
    assert stored.display_name == "Guild Nickname"
    assert stored.avatar_asset_key is None
