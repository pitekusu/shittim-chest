"""AWS and Discord adapters for post-projection record links."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from shittim_chest.application import DebateSnapshot
    from shittim_chest.application.status_publication import DiscordStatusGateway

from shittim_chest.adapters.aws import SsmParameterReader
from shittim_chest.adapters.discord import DiscordRestStatusGateway
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.application.discord import DiscordBotSlot, DiscordRuntimeConfig
from shittim_chest.config import parse_discord_runtime_config
from shittim_chest.config.models import StartupConfigurationError
from shittim_chest.config.status_publisher import MODERATOR_TOKEN_PARAMETER

from shittim_records.adapters import ProjectionConflict
from shittim_records.record_link_notifications import (
    RECORD_LINK_NOTIFICATION_PK,
    RECORD_LINK_NOTIFICATION_SCHEMA_VERSION,
    RecordLinkNotificationReceipt,
    RecordLinkNotificationState,
)

_RUNTIME_PARAMETER = re.compile(r"/shittim-chest/production/runtime/(?P<version>v[0-9]{4})\Z")


class DynamoRecordLinkNotificationStore:
    """Read and settle the content-free notification receipt."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def load(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
    ) -> RecordLinkNotificationReceipt | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": RECORD_LINK_NOTIFICATION_PK, "SK": record_id}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            return None
        item = unmarshal_item(raw)
        expected_fields = {
            "PK",
            "SK",
            "record_type",
            "schema_version",
            "source_fingerprint",
            "state",
            "attempted",
            "created_at",
        }
        if item.get("state") == RecordLinkNotificationState.SENT.value:
            expected_fields.add("sent_at")
        if item.get("attempted") is True:
            expected_fields.add("attempted_at")
        if (
            set(item) != expected_fields
            or item.get("PK") != RECORD_LINK_NOTIFICATION_PK
            or item.get("SK") != record_id
            or item.get("record_type") != "record_link_notification"
            or item.get("schema_version") != RECORD_LINK_NOTIFICATION_SCHEMA_VERSION
            or item.get("source_fingerprint") != source_fingerprint
            or item.get("state")
            not in {
                RecordLinkNotificationState.PENDING.value,
                RecordLinkNotificationState.SENT.value,
            }
            or not isinstance(item.get("attempted"), bool)
            or (
                item.get("state") == RecordLinkNotificationState.SENT.value
                and item.get("attempted") is not True
            )
            or not isinstance(item.get("created_at"), str)
            or ("sent_at" in item and not isinstance(item.get("sent_at"), str))
            or ("attempted_at" in item and not isinstance(item.get("attempted_at"), str))
        ):
            raise ProjectionConflict("record-link notification receipt is inconsistent")
        return RecordLinkNotificationReceipt(
            record_id=record_id,
            source_fingerprint=source_fingerprint,
            state=RecordLinkNotificationState(str(item["state"])),
            attempted=bool(item["attempted"]),
        )

    def mark_attempted(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: datetime,
    ) -> None:
        attempted_at = _timestamp(at)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": RECORD_LINK_NOTIFICATION_PK, "SK": record_id}),
                UpdateExpression="SET attempted = :true, attempted_at = :attempted_at",
                ConditionExpression=(
                    "source_fingerprint = :fingerprint AND #state = :pending AND attempted = :false"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=marshal_item(
                    {
                        ":true": True,
                        ":false": False,
                        ":attempted_at": attempted_at,
                        ":fingerprint": source_fingerprint,
                        ":pending": RecordLinkNotificationState.PENDING.value,
                    }
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            receipt = self.load(
                record_id=record_id,
                source_fingerprint=source_fingerprint,
            )
            if (
                receipt is None
                or receipt.state is not RecordLinkNotificationState.PENDING
                or not receipt.attempted
            ):
                raise ProjectionConflict("record-link notification attempt conflicts") from error

    def mark_sent(
        self,
        *,
        record_id: str,
        source_fingerprint: str,
        at: datetime,
    ) -> None:
        sent_at = _timestamp(at)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=marshal_item({"PK": RECORD_LINK_NOTIFICATION_PK, "SK": record_id}),
                UpdateExpression="SET #state = :sent, sent_at = :sent_at",
                ConditionExpression=(
                    "source_fingerprint = :fingerprint AND #state = :pending AND attempted = :true"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=marshal_item(
                    {
                        ":sent": RecordLinkNotificationState.SENT.value,
                        ":sent_at": sent_at,
                        ":fingerprint": source_fingerprint,
                        ":pending": RecordLinkNotificationState.PENDING.value,
                        ":true": True,
                    }
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            receipt = self.load(
                record_id=record_id,
                source_fingerprint=source_fingerprint,
            )
            if receipt is None or receipt.state is not RecordLinkNotificationState.SENT:
                raise ProjectionConflict("record-link notification settlement conflicts") from error


class DiscordRecordLinkGatewayFactory:
    """Load the fixed moderator identity and validate the source channel."""

    def __init__(
        self,
        *,
        reader: SsmParameterReader,
        http_client: httpx.Client,
        runtime_parameter_name: str,
        moderator_token_parameter_name: str,
    ) -> None:
        match = _RUNTIME_PARAMETER.fullmatch(runtime_parameter_name)
        if match is None or moderator_token_parameter_name != MODERATOR_TOKEN_PARAMETER:
            raise ValueError("record-link Discord parameters are invalid")
        self._reader = reader
        self._http_client = http_client
        self._runtime_parameter_name = runtime_parameter_name
        self._runtime_version = match.group("version")
        self._moderator_token_parameter_name = moderator_token_parameter_name
        self._runtime: DiscordRuntimeConfig | None = None
        self._gateway: DiscordRestStatusGateway | None = None

    async def create(self, snapshot: DebateSnapshot) -> DiscordStatusGateway:
        if self._runtime is None or self._gateway is None:
            raw_runtime = await self._reader.get_parameter(
                self._runtime_parameter_name,
                with_decryption=True,
            )
            runtime, version = parse_discord_runtime_config(raw_runtime)
            if version != self._runtime_version:
                raise StartupConfigurationError
            token = await self._reader.get_parameter(
                self._moderator_token_parameter_name,
                with_decryption=True,
            )
            self._runtime = runtime
            self._gateway = DiscordRestStatusGateway(
                client=self._http_client,
                bot_token=token,
                expected_application_id=runtime.application_id_for(DiscordBotSlot.MODERATOR),
                expected_guild_id=runtime.guild_id,
            )
        if not self._runtime.allows(
            guild_id=snapshot.guild_id,
            channel_id=snapshot.channel_id,
        ):
            raise StartupConfigurationError
        return self._gateway


__all__ = (
    "DiscordRecordLinkGatewayFactory",
    "DynamoRecordLinkNotificationStore",
)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record-link timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
