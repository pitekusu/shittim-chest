"""C10 absolute expiry, single-token revocation and private response boundaries."""

from datetime import UTC, datetime

import pytest
from tests.test_auth import FakeAvatars
from tests.test_mobile_exchange import KEY, NOW, REQUEST, ExchangeStore

from shittim_records.auth import AuthFailure, _digest, session_hash
from shittim_records.mobile_auth import MobileSessionRecord
from shittim_records.mobile_exchange import MobileExchangeService
from shittim_records.mobile_session import MobileSessionService


class SessionStore(ExchangeStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []
        self.deleted: list[str] = []
        self.fail_delete = False

    def get_session(self, *, session_hash: str) -> MobileSessionRecord | None:
        self.reads.append(session_hash)
        return self.sessions.get(session_hash)

    def delete_session(self, *, session_hash: str) -> None:
        if self.fail_delete:
            raise AuthFailure("mobile_session_unavailable")
        self.deleted.append(session_hash)
        self.sessions.pop(session_hash, None)


@pytest.fixture
def mobile():
    store = SessionStore()
    avatars = FakeAvatars()
    exchange = MobileExchangeService(
        store=store,
        avatars=avatars,
        session_key=KEY,
        admin_requester_key="r" * 43,
        clock=lambda: NOW,
    ).exchange(REQUEST)
    service = MobileSessionService(
        store=store,
        avatars=avatars,
        session_key=KEY,
        admin_requester_key="r" * 43,
        clock=lambda: NOW,
    )
    return service, store, avatars, exchange


def test_c09_token_returns_only_public_session_fields_without_extending_expiry(mobile):
    service, store, avatars, exchange = mobile
    token = exchange.access_token
    before = dict(store.sessions)
    urls = iter(("https://media.example/first", "https://media.example/second"))
    avatars.requester_avatar_url = lambda **kwargs: next(urls)
    first = service.session(raw_token=token)
    service._admin_requester_key = "another-admin"
    second = service.session(raw_token=token)
    assert first.is_admin and not second.is_admin  # Derived each time, never stored.
    assert first.user.avatar.url != second.user.avatar.url
    assert first.user.display_name == exchange.user.display_name
    assert first.expires_at == second.expires_at == exchange.expires_at
    assert set(first.model_dump(by_alias=True)) == {"schemaVersion", "user", "isAdmin", "expiresAt"}
    assert store.sessions == before and not store.deleted
    serialized = first.model_dump_json() + repr(first)
    for private in (token, "r" * 43, REQUEST.code, REQUEST.code_verifier):
        assert private not in serialized
    assert "Test" not in repr(first)
    assert store.reads == [_digest(KEY, "mobile-session", token)] * 2
    assert session_hash(KEY, token) not in store.reads


@pytest.mark.parametrize(
    "offset,valid", [(-1, False), (0, True), (7775999, True), (7776000, False)]
)
def test_90_day_deadline_is_absolute_even_if_expired_row_remains(mobile, offset, valid):
    service, store, _avatars, exchange = mobile
    record = next(iter(store.sessions.values()))
    service._clock = lambda: datetime.fromtimestamp(record.created_at + offset, UTC)
    assert (service.authenticate(raw_token=exchange.access_token) == record) is valid
    if valid:
        assert service.session(raw_token=exchange.access_token).expires_at == exchange.expires_at
    else:
        for operation in (service.session, service.logout):
            with pytest.raises(AuthFailure, match=r"^session_required$"):
                operation(raw_token=exchange.access_token)
    assert len(store.sessions) == 1 and not store.deleted


@pytest.mark.parametrize("token", [None, "", "t" * 42, "t" * 44, "t" * 42 + "\n", "あ" * 43])
def test_malformed_tokens_never_reach_storage(mobile, token):
    service, store, _avatars, _exchange = mobile
    assert service.authenticate(raw_token=token) is None
    with pytest.raises(AuthFailure, match=r"^session_required$"):
        service.logout(raw_token=token)
    assert not store.reads and not store.deleted


def test_logout_revokes_only_the_presented_token_and_unknown_or_reused_tokens_fail(mobile):
    service, store, _avatars, exchange = mobile
    record = next(iter(store.sessions.values()))
    other_token = "o" * 43
    other_hash = _digest(KEY, "mobile-session", other_token)
    store.sessions[other_hash] = record  # Same requester, another device.
    with pytest.raises(AuthFailure, match=r"^session_required$"):
        service.logout(raw_token="u" * 43)
    assert not store.deleted
    service.logout(raw_token=exchange.access_token)
    assert store.deleted == [_digest(KEY, "mobile-session", exchange.access_token)]
    assert service.authenticate(raw_token=other_token) == record
    assert service.authenticate(raw_token=exchange.access_token) is None
    for operation in (service.session, service.logout):
        with pytest.raises(AuthFailure, match=r"^session_required$"):
            operation(raw_token=exchange.access_token)
    assert store.sessions == {other_hash: record}


def test_avatar_placeholder_and_expiry_during_response_preparation(mobile):
    service, store, _avatars, exchange = mobile
    digest, record = next(iter(store.sessions.items()))
    store.sessions[digest] = record.model_copy(update={"avatar_asset_key": None})
    assert service.session(raw_token=exchange.access_token).user.avatar.kind == "placeholder"
    service._clock = iter((NOW, exchange.expires_at)).__next__
    with pytest.raises(AuthFailure, match=r"^session_required$"):
        service.session(raw_token=exchange.access_token)
    assert not store.deleted


def test_storage_failures_propagate_and_never_report_success(mobile, monkeypatch):
    service, store, _avatars, exchange = mobile
    store.fail_delete = True
    with pytest.raises(AuthFailure, match=r"^mobile_session_unavailable$"):
        service.logout(raw_token=exchange.access_token)
    assert len(store.sessions) == 1 and not store.deleted

    def corrupt(**kwargs):
        raise AuthFailure("mobile_session_record_invalid")

    monkeypatch.setattr(store, "get_session", corrupt)
    with pytest.raises(AuthFailure, match=r"^mobile_session_record_invalid$"):
        service.authenticate(raw_token=exchange.access_token)
