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
    MobileSessionRecord,
    MobileStartedTransaction,
    MobileTransaction,
    MobileTransactionState,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import PutTypeDef, TransactWriteItemTypeDef


class DynamoMobileAuthStore:
    """CAS the entire state, preserving the original deadline and device binding.

    Use the existing low-level client/codec to consume a grant and issue its session
    atomically. No raw transaction ID, code, verifier or token is accepted.
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

        The exchange service verifies the code and S256 before issue_session uses
        this Put. Never execute it alone: a partial failure could lose the session.
        """

        _require_live(expected, now_epoch)
        consumed = MobileConsumedTransaction(
            **expected.model_dump(include=set(MobileTransaction.model_fields)),
            status="consumed",
            consumed_at=now_epoch,
        )
        return {"Put": self._replacement_write(expected, consumed, now_epoch)}

    def issue_session(
        self,
        expected: MobileAuthorizedTransaction,
        *,
        session_hash: str,
        session: MobileSessionRecord,
        now_epoch: int,
    ) -> None:
        """Consume the exact grant, create a Bearer session and update its profile together."""

        if (
            re.fullmatch(r"[0-9a-f]{64}", session_hash) is None
            or session.created_at != now_epoch
            or any(
                getattr(session, field) != getattr(expected, field)
                for field in (
                    "requester_key",
                    "display_name",
                    "avatar_asset_key",
                    "guild_verified_at",
                )
            )
        ):
            raise AuthFailure("mobile_grant_invalid")
        writes: list[TransactWriteItemTypeDef] = [
            self.consumption_write(expected, now_epoch=now_epoch),
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(_session_item(session_hash, session)),
                    "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
                }
            },
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": marshal_item(
                        {
                            "PK": "PROFILE#REQUESTER",
                            "SK": session.requester_key,
                            "schema_version": 1,
                            "record_type": "requester_profile",
                            "display_name": session.display_name,
                            "avatar_asset_key": session.avatar_asset_key,
                            "updated_at": session.guild_verified_at.isoformat(),
                        }
                    ),
                }
            },
        ]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except self._client.exceptions.TransactionCanceledException as error:
            reasons = error.response.get("CancellationReasons", [])
            # Only the grant's failed CAS means an invalid/reused code. Capacity,
            # transaction conflicts or session-key collisions must remain retryable.
            if [reason.get("Code") for reason in reasons] == [
                "ConditionalCheckFailed",
                "None",
                "None",
            ]:
                raise AuthFailure("mobile_grant_invalid") from None
            raise AuthFailure("mobile_session_unavailable") from None

    def get_session(self, *, session_hash: str) -> MobileSessionRecord | None:
        """Read the latest session; the service checks its absolute deadline after I/O."""

        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item(_session_key(session_hash)),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            return None
        try:
            item = unmarshal_item(raw)
            payload = item.get("payload")
            if not isinstance(payload, str):
                raise ValueError
            session = MobileSessionRecord.model_validate_json(payload)
            if (
                type(item.get("schema_version")) is not int
                or type(item.get("expiresAt")) is not int
                or item != _session_item(session_hash, session)
            ):
                raise ValueError
        except ValueError, PersistenceFormatError:
            raise AuthFailure("mobile_session_record_invalid") from None
        return session

    def delete_session(self, *, session_hash: str) -> None:
        """Revoke only this token, never its profile, other devices or Web sessions."""

        self._client.delete_item(
            TableName=self._table_name,
            Key=marshal_item(_session_key(session_hash)),
        )

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


def _session_key(session_hash: str) -> dict[str, str]:
    if re.fullmatch(r"[0-9a-f]{64}", session_hash) is None:
        raise AuthFailure("session_required")
    return {"PK": f"MOBILE_SESSION#{session_hash}", "SK": "META"}


def _session_item(session_hash: str, session: MobileSessionRecord) -> dict[str, str | int]:
    return {
        **_session_key(session_hash),
        "schema_version": 1,
        "record_type": "mobile_session",
        "expiresAt": session.expires_at,
        "payload": session.model_dump_json(),
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
