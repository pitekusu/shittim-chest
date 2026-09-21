"""One device login through the real services, with isolated Discord and storage fakes."""

import json
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from tests.test_auth import FakeAvatars, FakeDiscord, FakeStore, configuration
from tests.test_http_api import FakeRecords, event
from tests.test_mobile_http import PREFIX
from tests.test_mobile_login import NOW, MemoryStore

from shittim_records.auth import OAUTH_COOKIE_NAME, AuthService
from shittim_records.http_api import AuthHttpController, ReadHttpController
from shittim_records.mobile_auth import MobileConsumedTransaction, MobileTransaction
from shittim_records.mobile_callback import MobileCallbackService
from shittim_records.mobile_exchange import MobileExchangeService
from shittim_records.mobile_http import MobileAuthHttpController
from shittim_records.mobile_login import MOBILE_OAUTH_COOKIE_NAME, MobileLoginService
from shittim_records.mobile_session import MobileSessionService


class MobileStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.sessions = {}

    def issue_session(self, expected, *, session_hash, session, now_epoch):
        assert self.states[expected.transaction_hash] == expected
        self.states[expected.transaction_hash] = MobileConsumedTransaction(
            **expected.model_dump(include=set(MobileTransaction.model_fields)),
            status="consumed",
            consumed_at=now_epoch,
        )
        self.sessions[session_hash] = session

    def get_session(self, *, session_hash):
        return self.sessions.get(session_hash)

    def delete_session(self, *, session_hash):
        del self.sessions[session_hash]


@pytest.fixture
def flow():
    config = configuration()
    web_store, store, discord, avatars = FakeStore(), MobileStore(), FakeDiscord(), FakeAvatars()
    mobile = MobileAuthHttpController(
        login=MobileLoginService(store=store, oauth=config.oauth, hmac_key=config.session_hmac_key),
        callback=MobileCallbackService(
            store=store, discord=discord, avatars=avatars, configuration=config, clock=lambda: NOW
        ),
        exchange=MobileExchangeService(
            store=store,
            avatars=avatars,
            session_key=config.session_hmac_key,
            admin_requester_key=config.admin_requester_key,
            clock=lambda: NOW,
        ),
        sessions=MobileSessionService(
            store=store,
            avatars=avatars,
            session_key=config.session_hmac_key,
            admin_requester_key=config.admin_requester_key,
            clock=lambda: NOW,
        ),
        allowed_origin=config.oauth.allowed_origin,
    )
    auth = AuthHttpController(
        AuthService(store=web_store, discord=discord, avatars=avatars, configuration=config), mobile
    )
    return auth, store, web_store, discord


def post(auth, path, payload):
    return auth.handle(
        {
            **event(f"POST {PREFIX}{path}", headers={"content-type": "application/json"}),
            "body": json.dumps(payload),
        },
        now=NOW,
    )


def authorize(auth):
    # RFC 7636 Appendix B: a public vector, not a real application credential.
    started = post(
        auth,
        "start",
        {
            "codeChallenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "codeChallengeMethod": "S256",
            "state": "s" * 43,
            "returnTo": "/",
        },
    )
    assert started["statusCode"] == 200
    path = json.loads(started["body"])["authorizePath"]
    browser = auth.handle(event(f"GET {PREFIX}authorize", query=urlsplit(path).query), now=NOW)
    assert browser["statusCode"] == 302
    state = parse_qs(urlsplit(browser["headers"]["Location"]).query)["state"][0]
    cookie = SimpleCookie(browser["cookies"][0])[MOBILE_OAUTH_COOKIE_NAME].value
    return event(
        "GET /api/v1/auth/discord/callback",
        query=urlencode(
            {
                "state": state,
                "code": "discord-one-time-code",
            }
        ),
        cookies=[f"{MOBILE_OAUTH_COOKIE_NAME}={cookie}"],
    )


def test_device_login_read_logout_and_replay_do_not_create_web_sessions(flow):
    auth, store, web_store, discord = flow
    callback = authorize(auth)
    completed = auth.handle(callback, now=NOW)
    assert completed["statusCode"] == 302
    assert len(completed["cookies"]) == 1 and "Max-Age=0" in completed["cookies"][0]
    query = parse_qs(urlsplit(completed["headers"]["Location"]).query)
    request = {
        "transactionId": query["transaction"][0],
        "code": query["code"][0],
        "codeVerifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    }
    wrong = post(auth, "exchange", {**request, "codeVerifier": "x" * 43})
    assert wrong["statusCode"] == 400 and not store.sessions
    assert json.loads(wrong["body"])["error"]["code"] == "MOBILE_GRANT_INVALID"
    exchanged = post(auth, "exchange", request)
    assert exchanged["statusCode"] == 200 and "cookies" not in exchanged
    payload = json.loads(exchanged["body"])
    assert payload["isAdmin"] is True and payload["returnTo"] == "/"
    assert payload["expiresAt"] == (NOW + timedelta(days=90)).isoformat().replace("+00:00", "Z")
    bearer = {"authorization": "Bearer " + payload["accessToken"]}
    session = auth.handle(event(f"GET {PREFIX}session", headers=bearer), now=NOW)
    assert session["statusCode"] == 200 and "accessToken" not in session["body"]
    read = ReadHttpController(
        store=web_store,
        mobile_store=store,
        session_key=configuration().session_hmac_key,
        records=cast(Any, FakeRecords()),
        clock=lambda: NOW,
    )
    assert read.handle(event("GET /api/v1/records", headers=bearer), now=NOW)["statusCode"] == 200
    assert post(auth, "exchange", request)["statusCode"] == 400
    assert auth.handle(callback, now=NOW)["statusCode"] == 400
    assert discord.codes == ["discord-one-time-code"]
    logged_out = auth.handle(event(f"POST {PREFIX}logout", headers=bearer), now=NOW)
    assert logged_out["statusCode"] == 204 and logged_out["body"] == ""
    assert not store.sessions and not web_store.sessions and not web_store.oauth
    assert read.handle(event("GET /api/v1/records", headers=bearer), now=NOW)["statusCode"] == 401
    for response in (completed, wrong, exchanged, session, logged_out):
        assert response["headers"]["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize(
    "failure", ["missing_cookie", "wrong_cookie", "duplicate_state", "cancel", "extra"]
)
def test_failed_mobile_callback_never_falls_back_to_web_or_discloses_input(flow, failure):
    auth, store, web_store, discord = flow
    callback = authorize(auth)
    if failure == "missing_cookie":
        callback["cookies"] = []
    elif failure == "wrong_cookie":
        callback["cookies"] = [f"{MOBILE_OAUTH_COOKIE_NAME}={'x' * 43}"]
    else:
        callback["rawQueryString"] += {
            "duplicate_state": "&state=web-state",
            "cancel": "&error=access_denied&error_description=private-provider-detail",
            "extra": "&redirect_uri=https://other.invalid",
        }[failure]
    response = auth.handle(callback, now=NOW)
    assert response["statusCode"] == 400
    assert "アプリに戻り" in response["body"] and "Location" not in response["headers"]
    assert "private-provider-detail" not in response["body"]
    assert "default-src 'none'" in response["headers"]["Content-Security-Policy"]
    assert list(SimpleCookie(response["cookies"][0])) == [MOBILE_OAUTH_COOKIE_NAME]
    assert not discord.codes and not store.sessions and not web_store.claimed


def test_web_callback_still_uses_web_state_and_cookies_when_mobile_is_connected(flow):
    auth, store, web_store, _discord = flow
    started = auth.handle(event("GET /api/v1/auth/discord/start"), now=NOW)
    query = parse_qs(urlsplit(started["headers"]["Location"]).query)
    nonce = SimpleCookie(started["cookies"][0])[OAUTH_COOKIE_NAME].value
    response = auth.handle(
        event(
            "GET /api/v1/auth/discord/callback",
            query=urlencode(
                {
                    "code": "web-code",
                    "state": query["state"][0],
                }
            ),
            cookies=[f"{OAUTH_COOKIE_NAME}={nonce}"],
        ),
        now=NOW,
    )
    assert response["statusCode"] == 302 and len(response["cookies"]) == 3
    assert len(web_store.sessions) == 1 and not store.states and not store.sessions
