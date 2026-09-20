"""Private mobile handoff storage; deliberately not wired to an HTTP handler yet."""

import re
from typing import TYPE_CHECKING

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import PersistenceFormatError

from shittim_records.auth import AuthFailure
from shittim_records.mobile_auth import (
    MOBILE_TRANSACTION_ADAPTER,
    MobileAuthorizedTransaction,
    MobileAuthorizingTransaction,
    MobileConsumedTransaction,
    MobileStartedTransaction,
    MobileTransaction,
    MobileTransactionState,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import PutTypeDef, TransactWriteItemTypeDef


class DynamoMobileAuthStore:
    """CAS the entire state, preserving the original deadline and device binding.

    Use the existing low-level client/codec so consumption can join the session
    transaction in C09. No raw transaction ID, code, verifier or token is accepted.
    """

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def create(self, state: MobileStartedTransaction, *, now_epoch: int) -> None:
        _require_live(state, now_epoch)
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=marshal_item(_item(state)),
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            raise AuthFailure("mobile_grant_invalid") from None

    def get(self, transaction_hash: str, *, now_epoch: int) -> MobileTransactionState:
        if re.fullmatch(r"[0-9a-f]{64}", transaction_hash) is None:
            raise AuthFailure("mobile_grant_invalid")
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": f"MOBILE#{transaction_hash}", "SK": "TRANSACTION"}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            raise AuthFailure("mobile_grant_invalid")
        try:
            item = unmarshal_item(raw)
            payload = item.get("payload")
            if not isinstance(payload, str):
                raise ValueError
            state = MOBILE_TRANSACTION_ADAPTER.validate_json(payload)
            if (
                type(item.get("schema_version")) is not int
                or type(item.get("expiresAt")) is not int
                or item != _item(state)
                or state.transaction_hash != transaction_hash
            ):
                raise ValueError
        except ValueError, PersistenceFormatError:
            raise AuthFailure("mobile_transaction_invalid") from None
        _require_live(state, now_epoch)
        return state

    def advance(
        self,
        expected: MobileStartedTransaction | MobileAuthorizingTransaction,
        replacement: MobileAuthorizingTransaction | MobileAuthorizedTransaction,
        *,
        now_epoch: int,
    ) -> None:
        """Only started -> authorizing -> authorized; never reissue or extend a grant."""

        if (expected.status, replacement.status) not in {
            ("started", "authorizing"),
            ("authorizing", "authorized"),
        }:
            raise AuthFailure("mobile_grant_invalid")
        if (
            isinstance(replacement, MobileAuthorizedTransaction)
            and replacement.code_issued_at != now_epoch
        ):
            raise AuthFailure("mobile_grant_invalid")
        put = self._replacement_write(expected, replacement, now_epoch)
        try:
            self._client.put_item(**put)
        except self._client.exceptions.ConditionalCheckFailedException:
            raise AuthFailure("mobile_grant_invalid") from None

    def consumption_write(
        self, expected: MobileAuthorizedTransaction, *, now_epoch: int
    ) -> TransactWriteItemTypeDef:
        """Build, but do not execute, the consume write for atomic session issuance.

        C09 must verify the supplied code and S256 first, then submit this Put and
        session creation together. A standalone consume method would allow a lost
        session on partial failure, so it is intentionally not provided.
        """

        _require_live(expected, now_epoch)
        consumed = MobileConsumedTransaction(
            **expected.model_dump(include=set(MobileTransaction.model_fields)),
            status="consumed",
            consumed_at=now_epoch,
        )
        return {"Put": self._replacement_write(expected, consumed, now_epoch)}

    def _replacement_write(
        self,
        expected: MobileTransactionState,
        replacement: MobileTransactionState,
        now_epoch: int,
    ) -> PutTypeDef:
        _require_live(expected, now_epoch)
        if not isinstance(replacement, MobileConsumedTransaction):
            _require_live(replacement, now_epoch)
        if any(
            getattr(expected, field) != getattr(replacement, field)
            for field in MobileTransaction.model_fields
        ):
            raise AuthFailure("mobile_grant_invalid")
        return {
            "TableName": self._table_name,
            "Item": marshal_item(_item(replacement)),
            "ConditionExpression": (
                "schema_version = :version AND record_type = :kind "
                "AND payload = :expected AND expiresAt = :expires AND expiresAt > :now"
            ),
            "ExpressionAttributeValues": marshal_item(
                {
                    ":version": 1,
                    ":kind": "mobile_transaction",
                    ":expected": expected.model_dump_json(by_alias=True),
                    ":expires": expected.expires_at,
                    ":now": now_epoch,
                }
            ),
        }


def _item(state: MobileTransactionState) -> dict[str, str | int]:
    return {
        "PK": f"MOBILE#{state.transaction_hash}",
        "SK": "TRANSACTION",
        "schema_version": 1,
        "record_type": "mobile_transaction",
        "expiresAt": state.expires_at,
        "payload": state.model_dump_json(by_alias=True),
    }


def _require_live(state: MobileTransactionState, now_epoch: int) -> None:
    # TTL is cleanup, not authorization; reject even if DynamoDB has not deleted it.
    if not state.created_at <= now_epoch < state.expires_at:
        raise AuthFailure("mobile_grant_invalid")
    if isinstance(state, MobileConsumedTransaction):
        raise AuthFailure("mobile_grant_invalid")
    if isinstance(state, MobileAuthorizedTransaction) and not (
        state.code_issued_at <= now_epoch < state.code_expires_at
    ):
        raise AuthFailure("mobile_grant_invalid")
