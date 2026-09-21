"""C05 boundaries; persistence, one-time consumption and HTTP routing are later work."""

import json

import pytest
from pydantic import ValidationError

from shittim_records.auth import AuthFailure
from shittim_records.mobile_auth import (
    MOBILE_SESSION_TTL_SECONDS,
    MOBILE_TRANSACTION_ADAPTER,
    MobileExchangeRequest,
    MobileStartRequest,
    build_mobile_auth_schema,
    parse_mobile_request,
)

START = {"codeChallenge": "a" * 43, "codeChallengeMethod": "S256", "state": "s" * 43}
TRANSACTION = {
    "transaction_hash": "a" * 64,
    "code_challenge": "b" * 43,
    "client_state": "s" * 43,
    "return_to": "/",
    "created_at": 1000,
    "expires_at": 1600,
}


def test_start_allows_only_owned_destinations_and_hides_correlation_values() -> None:
    for destination in ("/", f"/records/{'r' * 43}"):
        request = parse_mobile_request(MobileStartRequest, {**START, "returnTo": destination})
        assert request.return_to == destination
        assert request.model_dump(by_alias=True)["codeChallenge"] == START["codeChallenge"]
        assert START["state"] not in repr(request)
        assert START["codeChallenge"] not in repr(request)


@pytest.mark.parametrize(
    "overrides",
    [
        {"returnTo": "https://evil.example"},
        {"returnTo": "//evil.example"},
        {"returnTo": "/records/" + "r" * 43 + "?next=evil"},
        {"returnTo": "/records/" + "r" * 43 + "\n"},
        {"returnTo": "/admin"},
        {"returnTo": "/%2f%2fevil.example"},
        {"redirectUri": "https://evil.example"},
        {"codeChallengeMethod": "plain"},
        {"codeChallenge": "a" * 42},
        {"codeChallenge": "a" * 43 + "="},
        {"state": ""},
    ],
)
def test_invalid_start_is_sanitized(overrides: dict[str, str]) -> None:
    with pytest.raises(AuthFailure, match=r"^mobile_request_invalid$") as failure:
        parse_mobile_request(MobileStartRequest, {**START, **overrides})
    assert failure.value.__suppress_context__


def test_exchange_requires_rfc7636_verifier_and_rejects_extra_fields() -> None:
    base = {"transactionId": "t" * 43, "code": "c" * 43}
    for verifier in ("v" * 43, "v" * 124 + ".~_-", "v" * 128):
        request = parse_mobile_request(MobileExchangeRequest, {**base, "codeVerifier": verifier})
        assert request.code_verifier == verifier
        assert verifier not in repr(request)
    for invalid in ("v" * 42, "v" * 129, "v" * 42 + "!", "あ" * 43):
        with pytest.raises(AuthFailure):
            parse_mobile_request(MobileExchangeRequest, {**base, "codeVerifier": invalid})
    with pytest.raises(AuthFailure):
        parse_mobile_request(
            MobileExchangeRequest, {**base, "codeVerifier": "v" * 43, "accessToken": "secret"}
        )


def test_internal_state_requires_phase_specific_fields_and_bounded_lifetimes() -> None:
    started = {**TRANSACTION, "status": "started"}
    authorizing = {
        **TRANSACTION,
        "status": "authorizing",
        "browser_nonce_hash": "c" * 64,
        "oauth_state_hash": "d" * 64,
    }
    authorized = {
        **TRANSACTION,
        "status": "authorized",
        "code_hash": "c" * 64,
        "code_issued_at": 1580,
        "code_expires_at": 1600,
        "requester_key": "r" * 43,
        "display_name": "Test",
        "avatar_asset_key": f"requesters/{'r' * 43}/avatar.webp",
        "guild_verified_at": "2026-09-20T00:00:00Z",
    }
    consumed = {**TRANSACTION, "status": "consumed", "consumed_at": 1599}
    for valid in (started, authorizing, authorized, consumed):
        assert MOBILE_TRANSACTION_ADAPTER.validate_python(valid).status == valid["status"]
    for invalid in (
        {**started, "expires_at": 1601},
        {**started, "expires_at": 1000},
        {**started, "created_at": True},
        {**started, "status": "authorized"},
        {**started, "code_verifier": "v" * 43},
        {**authorizing, "browser_nonce_hash": "bad"},
        {**authorized, "code_issued_at": 1539},
        {**authorized, "code_expires_at": 1601},
        {**authorized, "avatar_asset_key": f"requesters/{'x' * 43}/avatar.webp"},
        {**authorized, "avatar_asset_key": "https://media.invalid/signed-avatar"},
        {**consumed, "consumed_at": 1600},
    ):
        with pytest.raises(ValidationError):
            MOBILE_TRANSACTION_ADAPTER.validate_python(invalid)
    assert MOBILE_SESSION_TTL_SECONDS == 90 * 24 * 60 * 60


def test_mobile_schema_has_no_internal_records_or_live_routes() -> None:
    schema = build_mobile_auth_schema()
    encoded = json.dumps(schema)
    assert "paths" not in schema
    for private_field in ("requester_key", "transaction_hash", "browser_nonce_hash", "code_hash"):
        assert private_field not in encoded
    assert "MobileSessionRecord" not in encoded
    # Web consumers keep the existing contracts; C12 will explicitly connect public routes.
    from shittim_records.generate_contracts import build_openapi

    assert not any("/auth/mobile/" in path for path in build_openapi()["paths"])
