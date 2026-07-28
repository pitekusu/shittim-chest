"""Asynchronous, content-free invocation of the durable status publisher."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.application.ports import (
    ReconciliationTriggerUnavailable,
    StatusTriggerUnavailable,
)

if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient

_STATUS_EVENT_SCHEMA_VERSION = 1


class LambdaStatusPublicationTrigger:
    """Kick one configured publisher Lambda using only an interaction snowflake."""

    __slots__ = ("_client", "_function_name")

    def __init__(self, *, client: LambdaClient, function_name: str) -> None:
        if not function_name or function_name != function_name.strip():
            raise ValueError("status function name must not be empty or padded")
        self._client = client
        self._function_name = function_name

    async def request_publication(self, interaction_id: str) -> None:
        """Queue an idempotent publication kick without blocking the event loop."""

        _require_canonical_snowflake(interaction_id)
        await asyncio.to_thread(self._request_publication, interaction_id)

    def _request_publication(self, interaction_id: str) -> None:
        payload = _payload(interaction_id)
        try:
            _invoke(self._client, function_name=self._function_name, payload=payload)
        except BotoCoreError, ClientError:
            raise StatusTriggerUnavailable from None
        except _InvocationRejected:
            raise StatusTriggerUnavailable from None


class LambdaRuntimeReconciliationTrigger:
    """Kick the scheduled reconciler without performing wake work in the HTTP path."""

    __slots__ = ("_client", "_function_name")

    def __init__(self, *, client: LambdaClient, function_name: str) -> None:
        if not function_name or function_name != function_name.strip():
            raise ValueError("reconciler function name must not be empty or padded")
        self._client = client
        self._function_name = function_name

    async def request_reconciliation(self, interaction_id: str) -> None:
        """Queue one content-free lost-wake recovery hint."""

        _require_canonical_snowflake(interaction_id)
        await asyncio.to_thread(self._request_reconciliation, interaction_id)

    def _request_reconciliation(self, interaction_id: str) -> None:
        try:
            _invoke(
                self._client,
                function_name=self._function_name,
                payload=_payload(interaction_id),
            )
        except BotoCoreError, ClientError, _InvocationRejected:
            raise ReconciliationTriggerUnavailable from None


class _InvocationRejected(Exception):
    pass


def _payload(interaction_id: str) -> bytes:
    return json.dumps(
        {
            "schema_version": _STATUS_EVENT_SCHEMA_VERSION,
            "interaction_id": interaction_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _invoke(client: LambdaClient, *, function_name: str, payload: bytes) -> None:
    response = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=payload,
    )
    status_code = response.get("StatusCode")
    if isinstance(status_code, bool) or status_code != 202:
        raise _InvocationRejected


def _require_canonical_snowflake(value: str) -> None:
    if (
        not value.isascii()
        or not value.isdecimal()
        or not 0 < int(value) < 2**64
        or value != str(int(value))
    ):
        raise ValueError("interaction ID must be a canonical Discord snowflake")
