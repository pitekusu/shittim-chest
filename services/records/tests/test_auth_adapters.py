"""OAuth, DynamoDB, SSM, and avatar adapter contracts."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from botocore.exceptions import ClientError
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.archive import derive_requester_key
from shittim_records.auth import (
    AuthConfiguration,
    AuthFailure,
    DiscordTokens,
    RecordsOAuthConfig,
)
from shittim_records.auth_adapters import (
    MAX_AVATAR_BYTES,
    AuthConfigurationRepository,
    DiscordOAuthClient,
    DynamoAuthStore,
    S3AvatarStore,
)

CLIENT_ID = "1" * 18
GUILD_ID = "2" * 18
USER_ID = "3" * 18


class FakeSsm:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _client_error(code: str, message: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "GetParameters",
    )


class FakeDynamo:
    def __init__(self, item: dict[str, Any] | None = None) -> None:
        self.item = item
        self.updated = 0

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["ConsistentRead"] is True
        return {} if self.item is None else {"Item": self.item}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        assert "attribute_not_exists(claimed_at)" in kwargs["ConditionExpression"]
        self.updated += 1
        return {}


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.presigns: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.puts.append(kwargs)
        return {}

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.presigns.append({"operation": operation, **kwargs})
        return "https://media.example.invalid/signed"


def oauth_config() -> RecordsOAuthConfig:
    return RecordsOAuthConfig(
        schema_version=1,
        client_id=CLIENT_ID,
        guild_id=GUILD_ID,
        allowed_origin="https://records.example.invalid",
        oauth_callback_url="https://records.example.invalid/api/v1/auth/discord/callback",
    )


def test_configuration_repository_loads_exact_five_parameters_once() -> None:
    names = ("identity", "oauth", "secret", "session", "admin")
    response = {
        "Parameters": [
            {"Name": "identity", "Value": "i" * 32},
            {"Name": "oauth", "Value": oauth_config().model_dump_json()},
            {"Name": "secret", "Value": "client-secret"},
            {"Name": "session", "Value": "s" * 32},
            {"Name": "admin", "Value": USER_ID},
        ]
    }
    client = FakeSsm(response)
    repository = AuthConfigurationRepository(
        cast(Any, client),
        identity_parameter_name=names[0],
        oauth_parameter_name=names[1],
        client_secret_parameter_name=names[2],
        session_key_parameter_name=names[3],
        admin_user_id_parameter_name=names[4],
    )

    first = repository.load()
    second = repository.load()

    assert first is second
    assert first.oauth == oauth_config()
    assert first.admin_requester_key == derive_requester_key(b"i" * 32, USER_ID)
    assert client.calls == [{"Names": list(names), "WithDecryption": True}]


def test_configuration_repository_rejects_missing_or_extra_oauth_fields() -> None:
    invalid = json.dumps({**oauth_config().model_dump(), "unexpected": True})
    client = FakeSsm(
        {
            "Parameters": [
                {"Name": "identity", "Value": "i" * 32},
                {"Name": "oauth", "Value": invalid},
                {"Name": "secret", "Value": "client-secret"},
                {"Name": "session", "Value": "s" * 32},
                {"Name": "admin", "Value": USER_ID},
            ]
        }
    )
    repository = AuthConfigurationRepository(
        cast(Any, client),
        identity_parameter_name="identity",
        oauth_parameter_name="oauth",
        client_secret_parameter_name="secret",  # noqa: S106 - parameter name, not a value.
        session_key_parameter_name="session",
        admin_user_id_parameter_name="admin",
    )

    with pytest.raises(AuthFailure) as caught:
        repository.load()
    assert caught.value.code == "configuration_invalid"


def test_configuration_repository_hides_provider_and_validation_inputs() -> None:
    private_user_id = "123456789" + "01234567"

    class InvalidSsm(FakeSsm):
        def get_parameters(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "Parameters": [
                    {"Name": "identity", "Value": "i" * 32},
                    {"Name": "oauth", "Value": json.dumps({"client_id": private_user_id})},
                    {"Name": "secret", "Value": "client-secret"},
                    {"Name": "session", "Value": "s" * 32},
                    {"Name": "admin", "Value": private_user_id},
                ]
            }

    repository = AuthConfigurationRepository(
        cast(Any, InvalidSsm({})),
        identity_parameter_name="identity",
        oauth_parameter_name="oauth",
        client_secret_parameter_name="secret",  # noqa: S106 - parameter name only.
        session_key_parameter_name="session",
        admin_user_id_parameter_name="admin",
    )

    with pytest.raises(AuthFailure) as caught:
        repository.load()

    assert caught.value.__cause__ is None
    assert private_user_id not in repr(caught.value)

    class FailingSsm(FakeSsm):
        def get_parameters(self, **_kwargs: Any) -> dict[str, Any]:
            raise _client_error("AccessDeniedException", private_user_id)

    provider_repository = AuthConfigurationRepository(
        cast(Any, FailingSsm({})),
        identity_parameter_name="identity",
        oauth_parameter_name="oauth",
        client_secret_parameter_name="secret",  # noqa: S106 - parameter name only.
        session_key_parameter_name="session",
        admin_user_id_parameter_name="admin",
    )

    with pytest.raises(AuthFailure) as provider_caught:
        provider_repository.load()

    assert provider_caught.value.__cause__ is None
    assert private_user_id not in repr(provider_caught.value)


def test_oauth_state_claim_uses_strong_read_and_conditional_update() -> None:
    item = marshal_item(
        {
            "PK": "OAUTH#opaque",
            "SK": "STATE",
            "schema_version": 1,
            "record_type": "oauth_state",
            "nonce_hash": "nonce-hash",
            "return_to": "/",
            "expiresAt": 200,
        }
    )
    client = FakeDynamo(item)

    state = DynamoAuthStore(cast(Any, client), "sessions").claim_oauth_state(
        state_hash="opaque",
        nonce_hash="nonce-hash",
        now_epoch=100,
        claimed_at="2026-08-17T00:00:00+00:00",
    )

    assert state.return_to == "/"
    assert client.updated == 1


def test_discord_oauth_uses_form_token_exchange_and_guild_member_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v10/oauth2/token":
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert b"grant_type=authorization_code" in request.content
            return httpx.Response(
                200,
                json={"access_token": "access-token", "token_type": "Bearer"},
            )
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={
                    "id": USER_ID,
                    "username": "user",
                    "global_name": "Global",
                    "avatar": "a" * 32,
                },
            )
        if request.url.path.endswith(f"/guilds/{GUILD_ID}/member"):
            return httpx.Response(200, json={"nick": "Guild", "avatar": "b" * 32})
        if request.url.host == "cdn.discordapp.com":
            return httpx.Response(200, content=b"webp", headers={"content-type": "image/webp"})
        raise AssertionError(f"unexpected request path: {request.url}")

    client = DiscordOAuthClient(httpx.Client(transport=httpx.MockTransport(handler)))
    configuration = AuthConfiguration(
        identity_hmac_key=b"i" * 32,
        session_hmac_key=b"s" * 32,
        admin_requester_key=derive_requester_key(b"i" * 32, USER_ID),
        oauth=oauth_config(),
        client_secret="client-secret",  # noqa: S106 - inert test credential.
    )

    tokens = client.exchange_code(code="code", configuration=configuration)
    identity = client.get_identity(tokens=tokens, guild_id=configuration.oauth.guild_id)
    avatar = client.fetch_avatar(identity=identity, guild_id=configuration.oauth.guild_id)

    assert identity.guild_nickname == "Guild"
    assert avatar == b"webp"
    assert requests[-1].url.path.startswith(f"/guilds/{GUILD_ID}/users/")


def test_discord_non_member_never_returns_an_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "user", "username": "name"})
        return httpx.Response(404)

    client = DiscordOAuthClient(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthFailure) as caught:
        client.get_identity(
            tokens=DiscordTokens(access_token="token", token_type="Bearer"),  # noqa: S106
            guild_id=GUILD_ID,
        )
    assert caught.value.code == "guild_membership_required"


def test_avatar_store_limits_prefix_size_and_presign_ttl() -> None:
    client = FakeS3()
    store = S3AvatarStore(cast(Any, client), "media")

    store.put_requester_avatar(object_key="requesters/opaque/avatar.webp", body=b"webp")
    url = store.requester_avatar_url(object_key="requesters/opaque/avatar.webp")

    assert url.startswith("https://")
    assert client.puts[0]["ServerSideEncryption"] == "AES256"
    assert client.presigns[0]["ExpiresIn"] == 300
    with pytest.raises(ValueError):
        store.put_requester_avatar(object_key="participants/a.webp", body=b"webp")
    with pytest.raises(ValueError):
        store.put_requester_avatar(
            object_key="requesters/opaque/avatar.webp",
            body=b"x" * (MAX_AVATAR_BYTES + 1),
        )


def test_session_record_roundtrip_rejects_private_key_shape() -> None:
    item = marshal_item(
        {
            "PK": "SESSION#opaque",
            "SK": "META",
            "schema_version": 1,
            "record_type": "session",
            "requester_key": "requester",
            "display_name": "Requester",
            "avatar_asset_key": "private/raw/avatar.webp",
            "csrf_hash": "csrf",
            "guild_verified_at": "2026-08-17T00:00:00+00:00",
            "expiresAt": 200,
        }
    )
    client = FakeDynamo(item)

    with pytest.raises(AuthFailure) as caught:
        DynamoAuthStore(cast(Any, client), "sessions").get_session(session_hash="opaque")
    assert caught.value.code == "session_record_invalid"
    assert unmarshal_item(item)["requester_key"] == "requester"
