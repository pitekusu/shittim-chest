"""Narrow AWS reads and REST-only Discord permission checks for native requests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import discord
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.ingress import DynamoDbIngressRepository
from shittim_chest.adapters.dynamodb.serializer import SUPPORTED_SCHEMA_VERSIONS
from shittim_chest.application.ports import MobileIngressSessionInvalid
from shittim_chest.application.scale_to_zero import EnqueuedIngress, IngressRequest
from shittim_chest.domain import DebatePhase

from shittim_records.archive import ARCHIVE_SCHEMA_VERSION, derive_record_key
from shittim_records.auth import AuthFailure
from shittim_records.mobile_auth import MobileSessionRecord
from shittim_records.mobile_auth_adapters import _session_item
from shittim_records.mobile_debate import (
    DebateProgress,
    DebateRequestFailure,
    DebateRequestPage,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef


class DiscordDebatePermissionGuard:
    """Use discord.py's role/overwrite resolution without opening a Gateway socket."""

    def __init__(self, *, bot_token: str, guild_id: str, channel_id: str, application_id: str):
        self._token = bot_token
        self._guild = int(guild_id)
        self._channel = int(channel_id) if channel_id else 0
        self._application = int(application_id)

    async def check(self, *, requester_id: str) -> None:
        try:
            # Each invocation owns its loop/session. Client.login is REST-only;
            # Client.start/connect would unnecessarily create a second Bot runtime.
            async with (
                asyncio.timeout(12),
                discord.Client(intents=discord.Intents.none()) as client,
            ):
                await client.login(self._token)
                if client.application_id != self._application or client.user is None:
                    raise DebateRequestFailure("DISCORD_UNAVAILABLE", 503)
                guild = await client.fetch_guild(self._guild, with_counts=False)
                try:
                    member = await guild.fetch_member(int(requester_id))
                except discord.NotFound:
                    raise DebateRequestFailure("GUILD_MEMBERSHIP_REQUIRED", 403) from None
                channel = await guild.fetch_channel(self._channel)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
                    raise DebateRequestFailure("CHANNEL_PERMISSION_REQUIRED", 403)
                permissions = channel.permissions_for(member)
                if (
                    member.pending
                    or member.is_timed_out()
                    or not permissions.view_channel
                    or not permissions.send_messages
                ):
                    raise DebateRequestFailure("CHANNEL_PERMISSION_REQUIRED", 403)
                bot_member = await guild.fetch_member(client.user.id)
                bot_permissions = channel.permissions_for(bot_member)
                if not all(
                    (
                        bot_permissions.view_channel,
                        bot_permissions.send_messages,
                        bot_permissions.read_message_history,
                        bot_permissions.create_public_threads,
                        bot_permissions.send_messages_in_threads,
                    )
                ):
                    raise DebateRequestFailure("DISCORD_UNAVAILABLE", 503)
        except discord.HTTPException, discord.LoginFailure, TimeoutError, OSError:
            raise DebateRequestFailure("DISCORD_UNAVAILABLE", 503) from None


class DynamoMobileDebateRepository:
    """Reuse Core's transaction/codec; no general database layer or full debate loads.

    The low-level client is necessary here for Core's existing cross-table atomic
    enqueue plus exact session ConditionCheck, not for a new persistence framework.
    """

    def __init__(
        self,
        *,
        client: DynamoDBClient,
        source_table: str,
        session_table: str,
        archive_table: str,
        identity_key: bytes,
    ) -> None:
        self._client = client
        self._source = source_table
        self._sessions = session_table
        self._archive = archive_table
        self._identity_key = identity_key
        self._ingress = DynamoDbIngressRepository(client=client, table_name=source_table)

    async def get(self, *, owner_key: str, request_id: str) -> IngressRequest | None:
        return await self._ingress.get_mobile_request(owner_key=owner_key, request_id=request_id)

    async def enqueue(
        self,
        request: IngressRequest,
        *,
        session_hash: str,
        session: MobileSessionRecord,
        now: datetime,
    ) -> EnqueuedIngress:
        item = _session_item(session_hash, session)
        condition: TransactWriteItemTypeDef = {
            "ConditionCheck": {
                "TableName": self._sessions,
                "Key": marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                "ConditionExpression": (
                    "schema_version = :version AND record_type = :kind AND payload = :payload "
                    "AND expiresAt = :expires AND expiresAt > :now"
                ),
                "ExpressionAttributeValues": marshal_item(
                    {
                        ":version": item["schema_version"],
                        ":kind": item["record_type"],
                        ":payload": item["payload"],
                        ":expires": session.expires_at,
                        ":now": int(now.timestamp()),
                    }
                ),
            }
        }
        try:
            return await self._ingress.enqueue_mobile(request, session_condition=condition)
        except MobileIngressSessionInvalid:
            raise AuthFailure("session_required") from None

    async def page(
        self,
        *,
        owner_key: str,
        limit: int,
        after: tuple[datetime, str] | None,
    ) -> DebateRequestPage:
        page = await self._ingress.list_mobile_requests(
            owner_key=owner_key,
            since=datetime.fromtimestamp(0, UTC),
            limit=limit,
            cursor_created_at=None if after is None else after[0],
            cursor_request_id=None if after is None else after[1],
        )
        return DebateRequestPage(page.requests, page.next_created_at, page.next_request_id)

    def progress(self, request: IngressRequest) -> DebateProgress:
        if request.accepted_debate_id is None:
            return DebateProgress()
        debate_id = str(request.accepted_debate_id)
        meta = self._get(self._source, f"DEBATE#{debate_id}")
        if meta is None:
            raise DebateRequestFailure("DEBATE_REQUESTS_UNAVAILABLE", 503)
        if (
            type(meta.get("schema_version")) is not int
            or meta.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS
            or meta.get("record_type") != "debate_meta"
            or meta.get("debate_id") != debate_id
            or meta.get("requester_id") != request.requester_id
            or meta.get("guild_id") != request.guild_id
            or meta.get("channel_id") != request.channel_id
        ):
            raise DebateRequestFailure("DEBATE_REQUESTS_UNAVAILABLE", 503)
        try:
            phase = DebatePhase(meta["current_phase"])
            updated = datetime.fromisoformat(meta["updated_at"])
            if updated.tzinfo is None or updated.utcoffset() is None:
                raise ValueError
        except ValueError, TypeError, KeyError:
            raise DebateRequestFailure("DEBATE_REQUESTS_UNAVAILABLE", 503) from None
        record_id = None
        if phase is DebatePhase.COMPLETED:
            public_id = derive_record_key(self._identity_key, debate_id)
            archive = self._get(self._archive, f"RECORD#{public_id}")
            if archive is not None:
                if (
                    type(archive.get("schema_version")) is not int
                    or archive.get("schema_version") not in range(1, ARCHIVE_SCHEMA_VERSION + 1)
                    or archive.get("record_type") != "archive_meta"
                    or archive.get("record_id") != public_id
                    or archive.get("requester_key") != request.owner_key
                    or archive.get("question") != request.question
                ):
                    raise DebateRequestFailure("DEBATE_REQUESTS_UNAVAILABLE", 503)
                record_id = public_id
        return DebateProgress(phase, updated, record_id)

    def _get(self, table: str, pk: str) -> dict | None:
        # META-only projection is mirrored by IAM's allowed attribute boundary.
        fields = (
            (
                "PK",
                "SK",
                "schema_version",
                "record_type",
                "debate_id",
                "requester_id",
                "guild_id",
                "channel_id",
                "current_phase",
                "updated_at",
            )
            if table == self._source
            else (
                "PK",
                "SK",
                "schema_version",
                "record_type",
                "record_id",
                "requester_key",
                "question",
            )
        )
        raw = self._client.get_item(
            TableName=table,
            Key=marshal_item({"PK": pk, "SK": "META"}),
            ConsistentRead=True,
            ProjectionExpression=", ".join(f"#f{index}" for index in range(len(fields))),
            ExpressionAttributeNames={f"#f{index}": field for index, field in enumerate(fields)},
        ).get("Item")
        return None if raw is None else unmarshal_item(raw)
