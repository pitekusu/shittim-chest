"""Metadata-only progress, exact session CAS, and SDK permission resolution."""

import asyncio
import ssl
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import aiohttp
import discord
import pytest
from shittim_chest.adapters.dynamodb.codec import marshal_item
from shittim_chest.application.ports import MobileIngressSessionInvalid
from shittim_chest.application.scale_to_zero import IngressRequest
from shittim_chest.domain import AttemptId, DebateId
from tests.test_mobile_debate import CHANNEL, GUILD, KEY, NOW, REQUEST_ID, USER, Sessions

from shittim_records.archive import derive_record_key
from shittim_records.auth import AuthFailure
from shittim_records.mobile_auth_adapters import _session_item
from shittim_records.mobile_debate import DebateRequestFailure
from shittim_records.mobile_debate_adapters import (
    DiscordDebatePermissionGuard,
    DynamoMobileDebateRepository,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient


def request():
    session = Sessions().session
    assert session is not None
    return replace(
        IngressRequest.mobile_debate(
            request_id=REQUEST_ID,
            owner_key=session.requester_key,
            application_id="fixture-application",
            question="Fictional question",
            requester_id=USER,
            requester_username="fixture",
            requester_display_name="Fixture",
            guild_id=GUILD,
            channel_id=CHANNEL,
            created_at=NOW,
        ),
        accepted_debate_id=DebateId.new(),
        accepted_attempt_id=AttemptId.new(),
    )


class MetadataClient:
    def __init__(self, request):
        self.calls = []
        self.meta = {
            "PK": f"DEBATE#{request.accepted_debate_id}",
            "SK": "META",
            "schema_version": 9,
            "record_type": "debate_meta",
            "debate_id": str(request.accepted_debate_id),
            "requester_id": USER,
            "guild_id": GUILD,
            "channel_id": CHANNEL,
            "current_phase": "completed",
            "current_attempt_id": str(request.accepted_attempt_id),
            "updated_at": (NOW + timedelta(seconds=1)).isoformat(),
        }
        self.archive = None

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        item = self.meta if kwargs["TableName"] == "source" else self.archive
        return {} if item is None else {"Item": marshal_item(item)}


def test_publication_waits_for_archive_meta_and_never_loads_generated_content():
    owned = request()
    client = MetadataClient(owned)
    repo = DynamoMobileDebateRepository(
        client=cast("DynamoDBClient", client),
        source_table="source",
        session_table="sessions",
        archive_table="archive",
        identity_key=KEY,
    )
    assert repo.progress(owned).record_id is None
    record_id = derive_record_key(KEY, str(owned.accepted_debate_id))
    client.archive = {
        "PK": f"RECORD#{record_id}",
        "SK": "META",
        "schema_version": 3,
        "record_type": "archive_meta",
        "record_id": record_id,
        "requester_key": owned.owner_key,
        "question": owned.question,
    }
    assert repo.progress(owned).record_id == record_id
    for call in client.calls:
        assert call["Key"]["SK"] == {"S": "META"} and call["ConsistentRead"]
        assert not set(call["ExpressionAttributeNames"].values()) & {
            "body",
            "persona",
            "initial_opinions",
            "final_proposals",
            "votes",
        }
    client.archive["requester_key"] = "another-owner"
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUESTS_UNAVAILABLE"):
        repo.progress(owned)


@pytest.mark.parametrize("version", [None, True, 999])
def test_unknown_progress_schema_is_not_exposed(version):
    owned = request()
    client = MetadataClient(owned)
    repo = DynamoMobileDebateRepository(
        client=cast("DynamoDBClient", client),
        source_table="source",
        session_table="sessions",
        archive_table="archive",
        identity_key=KEY,
    )
    client.meta["schema_version"] = version
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUESTS_UNAVAILABLE"):
        repo.progress(owned)

    client.meta["schema_version"] = 10
    record_id = derive_record_key(KEY, str(owned.accepted_debate_id))
    client.archive = {
        "PK": f"RECORD#{record_id}",
        "SK": "META",
        "schema_version": version,
        "record_type": "archive_meta",
        "record_id": record_id,
        "requester_key": owned.owner_key,
        "question": owned.question,
    }
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUESTS_UNAVAILABLE"):
        repo.progress(owned)


def test_session_condition_uses_canonical_payload_and_revocation_is_not_accepted(monkeypatch):
    session = Sessions().session
    assert session is not None
    repo = DynamoMobileDebateRepository(
        client=cast("DynamoDBClient", SimpleNamespace()),
        source_table="source",
        session_table="sessions",
        archive_table="archive",
        identity_key=KEY,
    )
    seen = []

    async def enqueue_mobile(_request, *, session_condition, attachment_actions=()):
        assert not attachment_actions
        seen.append(session_condition)
        raise MobileIngressSessionInvalid()

    monkeypatch.setattr(repo._ingress, "enqueue_mobile", enqueue_mobile)
    with pytest.raises(AuthFailure, match="session_required"):
        asyncio.run(repo.enqueue(request(), session_hash="a" * 64, session=session, now=NOW))
    condition = seen[0]["ConditionCheck"]
    assert condition["TableName"] == "sessions"
    assert condition["Key"] == marshal_item({"PK": "MOBILE_SESSION#" + "a" * 64, "SK": "META"})
    assert condition["ExpressionAttributeValues"][":payload"] == {
        "S": _session_item("a" * 64, session)["payload"],
    }
    assert "expiresAt > :now" in condition["ConditionExpression"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("allowed", None),
        ("overwrite", "CHANNEL_PERMISSION_REQUIRED"),
        ("screening", "CHANNEL_PERMISSION_REQUIRED"),
        ("missing", "GUILD_MEMBERSHIP_REQUIRED"),
        ("bot_permission", "DISCORD_UNAVAILABLE"),
    ],
)
def test_rest_guard_uses_sdk_roles_overwrites_and_bot_permissions(monkeypatch, mode, expected):
    real_client_class = discord.Client
    calls = []
    connectors = []

    class Client:
        application_id = 10**17 + 10
        user = SimpleNamespace(id=10**17 + 10)

        async def __aenter__(self):
            self.real = real_client_class(intents=discord.Intents.none())
            state = self.real._connection
            public = discord.Permissions(view_channel=True, send_messages=True)
            bot = discord.Permissions(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                create_public_threads=True,
                send_messages_in_threads=True,
            )
            if mode == "bot_permission":
                bot.create_public_threads = False
            self.guild = discord.Guild(
                state=state,
                data=cast(
                    Any,
                    {
                        "id": GUILD,
                        "name": "Fixture",
                        "owner_id": str(10**17 + 99),
                        "roles": [
                            {"id": GUILD, "name": "everyone", "permissions": str(public.value)},
                            {
                                "id": str(10**17 + 50),
                                "name": "bot",
                                "permissions": str(bot.value),
                            },
                        ],
                    },
                ),
            )
            overwrites = (
                []
                if mode != "overwrite"
                else [
                    {
                        "id": USER,
                        "type": 1,
                        "allow": "0",
                        "deny": str(discord.Permissions(send_messages=True).value),
                    }
                ]
            )
            self.channel = discord.TextChannel(
                state=state,
                guild=self.guild,
                data=cast(
                    Any,
                    {
                        "id": CHANNEL,
                        "name": "fixture",
                        "type": 0,
                        "position": 0,
                        "permission_overwrites": overwrites,
                    },
                ),
            )
            self.members = {
                int(identifier): discord.Member(
                    guild=self.guild,
                    state=state,
                    data=cast(
                        Any,
                        {
                            "user": {
                                "id": identifier,
                                "username": "fixture",
                                "discriminator": "0",
                                "avatar": None,
                            },
                            "roles": roles,
                            "flags": 0,
                            "pending": identifier == USER and mode == "screening",
                        },
                    ),
                )
                for identifier, roles in ((USER, []), (str(self.user.id), [str(10**17 + 50)]))
            }
            return self

        async def __aexit__(self, *_args):
            await self.real.close()

        async def login(self, _token):
            calls.append("login")

        async def fetch_guild(self, _guild, *, with_counts):
            return SimpleNamespace(
                id=int(GUILD), fetch_member=self.member, fetch_channel=self.channel_get
            )

        async def member(self, member_id):
            if member_id == int(USER) and mode == "missing":
                raise discord.NotFound(
                    cast(Any, SimpleNamespace(status=404, reason="Fixture")), "Fixture"
                )
            return self.members[member_id]

        async def channel_get(self, _channel):
            return self.channel

    def create_client(*, connector, **_kwargs):
        connectors.append(connector)
        assert isinstance(connector, aiohttp.TCPConnector)
        assert connector.limit == 0
        assert isinstance(connector._ssl, ssl.SSLContext)
        assert connector._ssl.verify_mode == ssl.CERT_REQUIRED
        assert connector._ssl.check_hostname
        return Client()

    monkeypatch.setattr(discord, "Client", create_client)
    guard = DiscordDebatePermissionGuard(
        bot_token="fixture-token",  # noqa: S106 - non-authentic offline fixture
        guild_id=GUILD,
        channel_id=CHANNEL,
        application_id=str(10**17 + 10),
    )
    if expected is None:
        asyncio.run(guard.check(requester_id=USER))
    else:
        with pytest.raises(DebateRequestFailure, match=expected):
            asyncio.run(guard.check(requester_id=USER))
    assert calls == ["login"]  # Never Client.start/connect: no second Gateway connection.
    assert len(connectors) == 1
    assert connectors[0].closed


@pytest.mark.parametrize("cancelled", [False, True])
def test_rest_guard_closes_fresh_connectors_across_invocation_loops(monkeypatch, cancelled):
    connectors = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def login(self, _token):
            if cancelled:
                raise asyncio.CancelledError
            raise discord.LoginFailure("offline fixture")

    def create_client(*, connector, **_kwargs):
        connectors.append(connector)
        return Client()

    monkeypatch.setattr(discord, "Client", create_client)
    guard = DiscordDebatePermissionGuard(
        bot_token="fixture-token",  # noqa: S106 - non-authentic offline fixture
        guild_id=GUILD,
        channel_id=CHANNEL,
        application_id=str(10**17 + 10),
    )
    for _ in range(2):
        expected = asyncio.CancelledError if cancelled else DebateRequestFailure
        with pytest.raises(expected):
            asyncio.run(guard.check(requester_id=USER))
    assert len(connectors) == 2
    assert connectors[0] is not connectors[1]
    assert all(connector.closed for connector in connectors)


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_attempt_id", "invalid"),
        ("thread_id", "001"),
        ("control_panel_message_id", str(2**64)),
        ("thread_id", 123),
    ],
)
def test_control_context_fails_closed_for_invalid_saved_identity(field, value):
    owned = request()
    client = MetadataClient(owned)
    client.meta[field] = value
    repo = DynamoMobileDebateRepository(
        client=cast("DynamoDBClient", client),
        source_table="source",
        session_table="sessions",
        archive_table="archive",
        identity_key=KEY,
    )
    with pytest.raises(DebateRequestFailure, match="DEBATE_REQUESTS_UNAVAILABLE"):
        repo.progress(owned)
