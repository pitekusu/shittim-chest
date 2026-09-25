"""C09 PKCE, issuance and privacy boundaries, without Discord or AWS calls."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.test_auth import FakeAvatars
from tests.test_mobile_auth_adapters import mobile_states

from shittim_records.auth import AuthFailure, _digest, session_hash
from shittim_records.mobile_auth import (
    MOBILE_SESSION_TTL_SECONDS,
    MobileAuthorizedTransaction,
    MobileConsumedTransaction,
    MobileExchangeRequest,
    MobileSessionRecord,
    MobileTransaction,
    MobileTransactionState,
)
from shittim_records.mobile_exchange import MobileExchangeService

KEY = b"test-only-mobile-session-key" * 2
NOW = datetime.fromtimestamp(1050, UTC)
# Public test vector from RFC 7636 Appendix B; not an application credential.
REQUEST = MobileExchangeRequest.model_validate(
    {
        "transactionId": "t" * 43,
        "code": "c" * 43,
        "codeVerifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    }
)


class ExchangeStore:
    def __init__(self) -> None:
        self.grant: MobileTransactionState = mobile_states()[2].model_copy(
            update={
                "transaction_hash": _digest(KEY, "mobile-transaction", REQUEST.transaction_id),
                "code_hash": _digest(KEY, "mobile-code", REQUEST.code),
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "avatar_asset_key": f"requesters/{'r' * 43}/avatar.webp",
                "return_to": "/records/" + "d" * 43,
            }
        )
        self.sessions: dict[str, MobileSessionRecord] = {}
        self.fail_issue = False

    def get(self, transaction_hash: str, *, now_epoch: int) -> MobileTransactionState:
        if transaction_hash != self.grant.transaction_hash:
            raise AuthFailure("mobile_grant_invalid")
        return self.grant

    def issue_session(
        self,
        expected: MobileAuthorizedTransaction,
        *,
        session_hash: str,
        session: MobileSessionRecord,
        now_epoch: int,
    ) -> None:
        if self.fail_issue:
            raise AuthFailure("mobile_session_unavailable")
        assert self.grant == expected
        self.sessions[session_hash] = session
        self.grant = MobileConsumedTransaction(
            **expected.model_dump(include=set(MobileTransaction.model_fields)),
            status="consumed",
            consumed_at=now_epoch,
        )


@pytest.fixture
def exchange():
    store = ExchangeStore()
    avatars = FakeAvatars()
    service = MobileExchangeService(
        store=store,
        avatars=avatars,
        session_key=KEY,
        admin_requester_key="r" * 43,
        clock=lambda: NOW,
    )
    return service, store, avatars


def test_rfc7636_exchange_returns_one_90_day_bearer_without_cookie_or_private_keys(exchange):
    service, store, _avatars = exchange
    result = service.exchange(REQUEST)
    token = result.access_token
    hashed = _digest(KEY, "mobile-session", token)
    saved = store.sessions[hashed]
    assert hashed != session_hash(KEY, token)  # Cannot be used as a Web cookie.
    assert saved.created_at == int(NOW.timestamp())
    assert saved.expires_at - saved.created_at == MOBILE_SESSION_TTL_SECONDS == 90 * 24 * 60 * 60
    assert result.expires_at == NOW + timedelta(days=90)
    assert result.token_type == "Bearer" and result.is_admin  # noqa: S105 - protocol value.
    assert result.return_to == "/records/" + "d" * 43
    assert result.user.display_name == "Test" and result.user.avatar.kind == "image"
    assert set(result.model_dump(by_alias=True)) == {
        "schemaVersion",
        "cacheAccountId",
        "accessToken",
        "tokenType",
        "expiresAt",
        "user",
        "isAdmin",
        "returnTo",
    }
    assert token not in repr(result) + saved.model_dump_json()
    assert "Test" not in repr(result) + repr(saved)
    for secret in (REQUEST.code, REQUEST.code_verifier, REQUEST.transaction_id):
        assert secret not in result.model_dump_json() + saved.model_dump_json()
    assert "https://" not in saved.model_dump_json()
    assert isinstance(store.grant, MobileConsumedTransaction)
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.exchange(REQUEST)
    assert list(store.sessions) == [hashed]


@pytest.mark.parametrize("field", ["transaction_id", "code", "code_verifier"])
def test_wrong_binding_is_rejected_without_consuming_grant(exchange, field):
    service, store, _avatars = exchange
    before = store.grant
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.exchange(REQUEST.model_copy(update={field: "x" * 43}))
    assert store.grant == before and not store.sessions


@pytest.mark.parametrize("seconds", [1039, 1100, 1600])
def test_code_deadlines_reject_even_when_storage_returns_a_stale_row(exchange, seconds):
    service, store, _avatars = exchange
    service._clock = lambda: datetime.fromtimestamp(seconds, UTC)
    with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
        service.exchange(REQUEST)
    assert not store.sessions


@pytest.mark.parametrize("failure", ["avatar", "storage", "expiry_during_avatar"])
def test_preparation_or_storage_failure_never_loses_the_code(exchange, monkeypatch, failure):
    service, store, avatars = exchange
    before = store.grant
    if failure == "avatar":

        def unavailable(**kwargs):
            raise AuthFailure("avatar_unavailable")

        monkeypatch.setattr(avatars, "requester_avatar_url", unavailable)
    elif failure == "storage":
        store.fail_issue = True
    else:
        service._clock = iter((NOW, datetime.fromtimestamp(1100, UTC))).__next__
    with pytest.raises(AuthFailure):
        service.exchange(REQUEST)
    assert store.grant == before and not store.sessions


def test_nonadmin_placeholder_and_session_storage_boundaries(exchange):
    service, store, _avatars = exchange
    service._admin_requester_key = "other-requester"
    store.grant = store.grant.model_copy(update={"avatar_asset_key": None})
    result = service.exchange(REQUEST)
    assert not result.is_admin and result.user.avatar.kind == "placeholder"
    saved = next(iter(store.sessions.values()))
    for changes in (
        {"expires_at": saved.expires_at + 1},
        {"avatar_asset_key": f"requesters/{'x' * 43}/avatar.webp"},
        {"access_token": result.access_token},
    ):
        with pytest.raises(ValidationError):
            MobileSessionRecord.model_validate({**saved.model_dump(), **changes})
