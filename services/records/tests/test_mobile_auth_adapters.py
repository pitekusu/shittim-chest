"""Fail-closed mobile storage boundaries; real CAS behavior is tested with DynamoDB Local."""

from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from shittim_chest.adapters.dynamodb.codec import marshal_item

from shittim_records.auth import AuthFailure
from shittim_records.mobile_auth import (
    MOBILE_SESSION_TTL_SECONDS,
    MobileAuthorizedTransaction,
    MobileAuthorizingTransaction,
    MobileSessionRecord,
    MobileStartedTransaction,
)
from shittim_records.mobile_auth_adapters import DynamoMobileAuthStore


def mobile_states() -> tuple[
    MobileStartedTransaction, MobileAuthorizingTransaction, MobileAuthorizedTransaction
]:
    base = {
        "transaction_hash": "a" * 64,
        "code_challenge": "b" * 43,
        "client_state": "s" * 43,
        "return_to": "/",
        "created_at": 1000,
        "expires_at": 1600,
    }
    return (
        MobileStartedTransaction.model_validate({**base, "status": "started"}),
        MobileAuthorizingTransaction.model_validate(
            {
                **base,
                "status": "authorizing",
                "browser_nonce_hash": "c" * 64,
                "oauth_state_hash": "d" * 64,
            }
        ),
        MobileAuthorizedTransaction.model_validate(
            {
                **base,
                "status": "authorized",
                "code_hash": "e" * 64,
                "code_issued_at": 1040,
                "code_expires_at": 1100,
                "requester_key": "r" * 43,
                "display_name": "Test",
                "avatar_asset_key": None,
                "guild_verified_at": "2026-09-20T00:00:00Z",
            }
        ),
    )


def mobile_session(*, now_epoch: int = 1041) -> MobileSessionRecord:
    authorized = mobile_states()[2]
    return MobileSessionRecord(
        requester_key=authorized.requester_key,
        display_name=authorized.display_name,
        avatar_asset_key=authorized.avatar_asset_key,
        guild_verified_at=authorized.guild_verified_at,
        created_at=now_epoch,
        expires_at=now_epoch + MOBILE_SESSION_TTL_SECONDS,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"payload": "private invalid data"},
        {"schema_version": 2},
        {"expiresAt": 1700},
        {"record_type": "session"},
        {"PK": "MOBILE#" + "b" * 64},
    ],
)
def test_corrupt_records_have_safe_errors(change: dict[str, Any]) -> None:
    state, _, _ = mobile_states()
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - offline Stubber dummy credential.
    )
    store = DynamoMobileAuthStore(client, "test-mobile-auth")
    item = {
        "PK": "MOBILE#" + state.transaction_hash,
        "SK": "TRANSACTION",
        "schema_version": 1,
        "record_type": "mobile_transaction",
        "expiresAt": 1600,
        "payload": state.model_dump_json(by_alias=True),
        **change,
    }
    with Stubber(client) as stub:
        stub.add_response("get_item", {"Item": marshal_item(item)})
        with pytest.raises(AuthFailure, match=r"^mobile_transaction_invalid$") as failure:
            store.get(state.transaction_hash, now_epoch=1040)
        assert failure.value.__suppress_context__


def test_invalid_transitions_and_deadlines_do_not_call_dynamodb() -> None:
    started, authorizing, authorized = mobile_states()
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - offline Stubber dummy credential.
    )
    store = DynamoMobileAuthStore(client, "test-mobile-auth")
    with Stubber(client):  # Any attempted I/O fails the test.
        for replacement in (
            authorized,  # Cannot skip browser binding.
            authorizing.model_copy(update={"expires_at": 1599}),
            authorizing.model_copy(update={"code_challenge": "z" * 43}),
            authorizing.model_copy(update={"client_state": "z" * 43}),
            authorizing.model_copy(update={"return_to": "/records/" + "r" * 43}),
        ):
            with pytest.raises(AuthFailure, match=r"^mobile_grant_invalid$"):
                store.advance(started, replacement, now_epoch=1040)
        for now in (999, 1600):
            with pytest.raises(AuthFailure):
                store.create(started, now_epoch=now)
        for now in (1039, 1100, 1600):
            with pytest.raises(AuthFailure):
                store.consumption_write(authorized, now_epoch=now)
        with pytest.raises(AuthFailure):
            store.advance(authorizing, authorized, now_epoch=1041)
        with pytest.raises(AuthFailure):
            store.get("not-a-digest", now_epoch=1040)


def test_storage_failure_is_not_reported_as_an_invalid_grant() -> None:
    started, _, _ = mobile_states()
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - offline Stubber dummy credential.
    )
    with Stubber(client) as stub:
        stub.add_client_error(
            "put_item", service_error_code="ProvisionedThroughputExceededException"
        )
        with pytest.raises(client.exceptions.ProvisionedThroughputExceededException):
            DynamoMobileAuthStore(client, "test-mobile-auth").create(started, now_epoch=1000)


@pytest.mark.parametrize(
    "reasons,category",
    [
        (["ConditionalCheckFailed", "None", "None"], "mobile_grant_invalid"),
        (["None", "ConditionalCheckFailed", "None"], "mobile_session_unavailable"),
        (["TransactionConflict", "None", "None"], "mobile_session_unavailable"),
        (["None", "ProvisionedThroughputExceeded", "None"], "mobile_session_unavailable"),
        ([], "mobile_session_unavailable"),
    ],
)
def test_exchange_cancellation_is_sanitized_and_distinguishes_grant_reuse(reasons, category):
    client = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - Stubber only.
    )
    store = DynamoMobileAuthStore(client, "test-mobile-auth")
    with Stubber(client) as stub:
        stub.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            service_message="private storage detail",
            modeled_fields={"CancellationReasons": [{"Code": code} for code in reasons]}
            if reasons
            else {},
        )
        with pytest.raises(AuthFailure, match=f"^{category}$") as failure:
            store.issue_session(
                mobile_states()[2], session_hash="a" * 64, session=mobile_session(), now_epoch=1041
            )
        assert failure.value.__suppress_context__
