"""AWS and Discord adapters for Records OAuth and session handling."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.auth import (
    AuthConfiguration,
    AuthFailure,
    DiscordIdentity,
    DiscordTokens,
    OAuthState,
    RecordsOAuthConfig,
    SessionRecord,
)

DISCORD_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"  # noqa: S105
DISCORD_API_ROOT = "https://discord.com/api/v10"
DISCORD_CDN_ROOT = "https://cdn.discordapp.com"
MAX_AVATAR_BYTES = 512 * 1024


class AuthConfigurationRepository:
    """Load and validate the four private values used by the Auth Lambda."""

    def __init__(
        self,
        client: SSMClient,
        *,
        identity_parameter_name: str,
        oauth_parameter_name: str,
        client_secret_parameter_name: str,
        session_key_parameter_name: str,
    ) -> None:
        self._client = client
        self._names = (
            identity_parameter_name,
            oauth_parameter_name,
            client_secret_parameter_name,
            session_key_parameter_name,
        )
        self._cached: AuthConfiguration | None = None

    def load(self) -> AuthConfiguration:
        if self._cached is not None:
            return self._cached
        response = self._client.get_parameters(Names=list(self._names), WithDecryption=True)
        if response.get("InvalidParameters"):
            raise AuthFailure("configuration_unavailable")
        values = {
            parameter["Name"]: parameter.get("Value", "")
            for parameter in response.get("Parameters", [])
        }
        if set(values) != set(self._names):
            raise AuthFailure("configuration_unavailable")
        try:
            identity_key = values[self._names[0]].encode()
            oauth = RecordsOAuthConfig.model_validate_json(values[self._names[1]])
            client_secret = values[self._names[2]]
            session_key = values[self._names[3]].encode()
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            raise AuthFailure("configuration_invalid") from error
        if len(identity_key) < 32 or len(session_key) < 32 or not client_secret:
            raise AuthFailure("configuration_invalid")
        self._cached = AuthConfiguration(
            identity_hmac_key=identity_key,
            session_hmac_key=session_key,
            oauth=oauth,
            client_secret=client_secret,
        )
        return self._cached


class DynamoAuthStore:
    """Persist only hashed OAuth and session identifiers."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self._client = client
        self._table_name = table_name

    def create_oauth_state(self, *, state_hash: str, state: OAuthState) -> None:
        self._client.put_item(
            TableName=self._table_name,
            Item=marshal_item(
                {
                    "PK": f"OAUTH#{state_hash}",
                    "SK": "STATE",
                    "schema_version": 1,
                    "record_type": "oauth_state",
                    "nonce_hash": state.nonce_hash,
                    "return_to": state.return_to,
                    "expiresAt": state.expires_at,
                }
            ),
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    def claim_oauth_state(
        self,
        *,
        state_hash: str,
        nonce_hash: str,
        now_epoch: int,
        claimed_at: str,
    ) -> OAuthState:
        key = marshal_item({"PK": f"OAUTH#{state_hash}", "SK": "STATE"})
        response = self._client.get_item(
            TableName=self._table_name,
            Key=key,
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            raise AuthFailure("oauth_state_invalid")
        item = unmarshal_item(raw)
        expires_at = item.get("expiresAt")
        if (
            item.get("schema_version") != 1
            or item.get("record_type") != "oauth_state"
            or item.get("nonce_hash") != nonce_hash
            or not isinstance(item.get("return_to"), str)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= now_epoch
            or item.get("claimed_at") is not None
        ):
            raise AuthFailure("oauth_state_invalid")
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=key,
                UpdateExpression="SET claimed_at = :claimed_at",
                ConditionExpression=(
                    "attribute_not_exists(claimed_at) AND nonce_hash = :nonce AND expiresAt > :now"
                ),
                ExpressionAttributeValues=marshal_item(
                    {":claimed_at": claimed_at, ":nonce": nonce_hash, ":now": now_epoch}
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise AuthFailure("oauth_state_invalid") from None
            raise
        return OAuthState(
            nonce_hash=nonce_hash,
            return_to=str(item["return_to"]),
            expires_at=expires_at,
        )

    def create_session(
        self,
        *,
        session_hash: str,
        session: SessionRecord,
    ) -> None:
        self._client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": marshal_item(
                            {
                                "PK": f"SESSION#{session_hash}",
                                "SK": "META",
                                "schema_version": 1,
                                "record_type": "session",
                                "requester_key": session.requester_key,
                                "display_name": session.display_name,
                                "avatar_asset_key": session.avatar_asset_key,
                                "csrf_hash": session.csrf_hash,
                                "guild_verified_at": session.guild_verified_at,
                                "expiresAt": session.expires_at,
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
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
                                "updated_at": session.guild_verified_at,
                            }
                        ),
                    }
                },
            ]
        )

    def get_session(self, *, session_hash: str) -> SessionRecord | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": f"SESSION#{session_hash}", "SK": "META"}),
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            return None
        item = unmarshal_item(raw)
        required_strings = ("requester_key", "display_name", "csrf_hash", "guild_verified_at")
        expires_at = item.get("expiresAt")
        if (
            item.get("schema_version") != 1
            or item.get("record_type") != "session"
            or any(
                not isinstance(item.get(field), str) or not item[field]
                for field in required_strings
            )
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
        ):
            raise AuthFailure("session_record_invalid")
        avatar = item.get("avatar_asset_key")
        if avatar is not None and (
            not isinstance(avatar, str) or not avatar.startswith("requesters/")
        ):
            raise AuthFailure("session_record_invalid")
        return SessionRecord(
            requester_key=str(item["requester_key"]),
            display_name=str(item["display_name"]),
            avatar_asset_key=avatar,
            csrf_hash=str(item["csrf_hash"]),
            guild_verified_at=str(item["guild_verified_at"]),
            expires_at=expires_at,
        )

    def delete_session(self, *, session_hash: str) -> None:
        self._client.delete_item(
            TableName=self._table_name,
            Key=marshal_item({"PK": f"SESSION#{session_hash}", "SK": "META"}),
        )


class DiscordOAuthClient:
    """Bounded Discord OAuth and avatar client."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def exchange_code(self, *, code: str, configuration: AuthConfiguration) -> DiscordTokens:
        try:
            response = self._client.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": configuration.oauth.client_id,
                    "client_secret": configuration.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": configuration.oauth.oauth_callback_url,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as error:
            raise AuthFailure("discord_token_exchange_failed") from error
        if response.status_code != 200:
            raise AuthFailure("discord_token_exchange_failed")
        try:
            payload = response.json()
            token = payload["access_token"]
            token_type = payload["token_type"]
        except (KeyError, TypeError, ValueError) as error:
            raise AuthFailure("discord_token_response_invalid") from error
        if not isinstance(token, str) or not token or token_type != "Bearer":  # noqa: S105
            raise AuthFailure("discord_token_response_invalid")
        return DiscordTokens(access_token=token, token_type=token_type)

    def get_identity(
        self,
        *,
        tokens: DiscordTokens,
        guild_id: str,
    ) -> DiscordIdentity:
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        try:
            user_response = self._client.get(f"{DISCORD_API_ROOT}/users/@me", headers=headers)
            member_response = self._client.get(
                f"{DISCORD_API_ROOT}/users/@me/guilds/{guild_id}/member",
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise AuthFailure("discord_identity_unavailable") from error
        if member_response.status_code in {401, 403, 404}:
            raise AuthFailure("guild_membership_required")
        if user_response.status_code != 200 or member_response.status_code != 200:
            raise AuthFailure("discord_identity_unavailable")
        try:
            user = user_response.json()
            member = member_response.json()
            user_id = user["id"]
            username = user["username"]
        except (KeyError, TypeError, ValueError) as error:
            raise AuthFailure("discord_identity_invalid") from error
        if (
            not isinstance(user_id, str)
            or not 17 <= len(user_id) <= 20
            or not user_id.isdecimal()
            or not isinstance(username, str)
            or not username.strip()
        ):
            raise AuthFailure("discord_identity_invalid")
        user_avatar = _optional_text(user.get("avatar"))
        guild_avatar = _optional_text(member.get("avatar"))
        if any(
            value is not None and not _valid_avatar_hash(value)
            for value in (user_avatar, guild_avatar)
        ):
            raise AuthFailure("discord_identity_invalid")
        return DiscordIdentity(
            user_id=user_id,
            username=username,
            global_name=_optional_text(user.get("global_name")),
            user_avatar_hash=user_avatar,
            guild_nickname=_optional_text(member.get("nick")),
            guild_avatar_hash=guild_avatar,
        )

    def fetch_avatar(self, *, identity: DiscordIdentity, guild_id: str) -> bytes | None:
        if identity.guild_avatar_hash:
            url = (
                f"{DISCORD_CDN_ROOT}/guilds/{guild_id}/users/{identity.user_id}/avatars/"
                f"{identity.guild_avatar_hash}.webp?size=128"
            )
        elif identity.user_avatar_hash:
            url = (
                f"{DISCORD_CDN_ROOT}/avatars/{identity.user_id}/"
                f"{identity.user_avatar_hash}.webp?size=128"
            )
        else:
            return None
        try:
            response = self._client.get(url)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        body = response.content
        if content_type != "image/webp" or not body or len(body) > MAX_AVATAR_BYTES:
            return None
        return body


class S3AvatarStore:
    """Store only opaque requester avatar keys in the private media bucket."""

    def __init__(self, client: S3Client, bucket_name: str) -> None:
        self._client = client
        self._bucket_name = bucket_name

    def put_requester_avatar(self, *, object_key: str, body: bytes) -> None:
        if not _valid_requester_key(object_key) or not body or len(body) > MAX_AVATAR_BYTES:
            raise ValueError("requester avatar is outside the permitted boundary")
        self._client.put_object(
            Bucket=self._bucket_name,
            Key=object_key,
            Body=body,
            ContentType="image/webp",
            CacheControl="private,max-age=300",
            ServerSideEncryption="AES256",
        )

    def requester_avatar_url(self, *, object_key: str) -> str:
        if not _valid_requester_key(object_key):
            raise ValueError("requester avatar is outside the permitted boundary")
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket_name, "Key": object_key},
            ExpiresIn=300,
        )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _valid_requester_key(value: str) -> bool:
    return (
        value.startswith("requesters/")
        and len(value) <= 256
        and ".." not in value
        and all(character.isalnum() or character in "/._-" for character in value)
    )


def _valid_avatar_hash(value: str) -> bool:
    payload = value.removeprefix("a_")
    return len(payload) == 32 and all(character in "0123456789abcdef" for character in payload)
