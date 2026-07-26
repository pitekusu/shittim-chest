"""Narrow, content-free component authorization reads for the HTTP path."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactGetItemTypeDef
else:
    TransactGetItemTypeDef = object

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import (
    CURRENT_SCHEMA_VERSION,
    DynamoItem,
    DynamoValue,
)
from shittim_chest.application.models import DebateAuthorizationSnapshot
from shittim_chest.application.ports import RepositoryUnavailable
from shittim_chest.domain import AttemptId, DebateId, DebatePhase


class DynamoDbDebateAuthorizationLookup:
    """Read debate META only, with a legacy attempt fallback for phase."""

    __slots__ = ("_client", "_table_name")

    def __init__(self, *, client: DynamoDBClient, table_name: str) -> None:
        if not table_name.strip():
            raise ValueError("table name must not be empty")
        self._client = client
        self._table_name = table_name

    async def get(
        self,
        debate_id: DebateId,
        expected_attempt_id: AttemptId,
    ) -> DebateAuthorizationSnapshot | None:
        """Return only fields required for requester, location, panel, and phase checks."""

        try:
            return await asyncio.to_thread(self._get, debate_id, expected_attempt_id)
        except BotoCoreError, ClientError, KeyError, TypeError, ValueError:
            raise RepositoryUnavailable from None

    def _get(
        self,
        debate_id: DebateId,
        expected_attempt_id: AttemptId,
    ) -> DebateAuthorizationSnapshot | None:
        partition_key = f"DEBATE#{debate_id}"
        attempt_key = f"ATTEMPT#{expected_attempt_id}#META"
        meta, attempt = self._transact_get_items(
            (
                {"PK": partition_key, "SK": "META"},
                {"PK": partition_key, "SK": attempt_key},
            )
        )
        if meta is None:
            return None
        _require_record(meta, pk=partition_key, sk="META", record_type="debate_meta")
        if _text(meta, "debate_id") != str(debate_id):
            raise ValueError("debate identity mismatch")
        attempt_id = AttemptId.parse(_text(meta, "current_attempt_id"))
        if attempt_id != expected_attempt_id:
            return None
        phase_text = _optional_text(meta, "current_phase")
        if phase_text is None:
            if attempt is None:
                raise ValueError("current attempt metadata is missing")
            _require_record(
                attempt,
                pk=partition_key,
                sk=attempt_key,
                record_type="attempt_meta",
            )
            if _text(attempt, "attempt_id") != str(attempt_id):
                raise ValueError("attempt identity mismatch")
            phase_text = _text(attempt, "phase")
        return DebateAuthorizationSnapshot(
            debate_id=debate_id,
            attempt_id=attempt_id,
            phase=DebatePhase(phase_text),
            requester_id=_text(meta, "requester_id"),
            guild_id=_text(meta, "guild_id"),
            channel_id=_text(meta, "channel_id"),
            thread_id=_optional_text(meta, "thread_id"),
            control_panel_message_id=_optional_text(meta, "control_panel_message_id"),
        )

    def _transact_get_items(
        self,
        keys: tuple[DynamoItem, ...],
    ) -> tuple[DynamoItem | None, ...]:
        actions = [
            cast(
                TransactGetItemTypeDef,
                {
                    "Get": {
                        "TableName": self._table_name,
                        "Key": marshal_item(key),
                    }
                },
            )
            for key in keys
        ]
        response = self._client.transact_get_items(
            TransactItems=actions,
            ReturnConsumedCapacity="NONE",
        )
        raw_responses = response.get("Responses")
        if raw_responses is None or len(raw_responses) != len(keys):
            raise ValueError("authorization transaction response is incomplete")
        items: list[DynamoItem | None] = []
        for raw_response in raw_responses:
            raw_item = raw_response.get("Item")
            items.append(None if raw_item is None else unmarshal_item(raw_item))
        return tuple(items)


def _require_record(
    item: Mapping[str, DynamoValue],
    *,
    pk: str,
    sk: str,
    record_type: str,
) -> None:
    if (
        _text(item, "PK") != pk
        or _text(item, "SK") != sk
        or _text(item, "record_type") != record_type
        or _integer(item, "schema_version") != CURRENT_SCHEMA_VERSION
    ):
        raise ValueError("authorization record metadata mismatch")


def _text(item: Mapping[str, DynamoValue], name: str) -> str:
    value = item[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("authorization text field is invalid")
    return value


def _optional_text(item: Mapping[str, DynamoValue], name: str) -> str | None:
    if name not in item:
        return None
    return _text(item, name)


def _integer(item: Mapping[str, DynamoValue], name: str) -> int:
    value = item[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("authorization integer field is invalid")
    return value


__all__ = ("DynamoDbDebateAuthorizationLookup",)
