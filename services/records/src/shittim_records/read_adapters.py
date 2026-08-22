"""AWS adapters for the authenticated Records read API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_ssm.client import SSMClient

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.adapters.dynamodb.serializer import DynamoItem

from shittim_records.auth import AuthFailure
from shittim_records.read_api import (
    ArchivePage,
    ParticipantSlot,
    ReadFailure,
    RequesterProfile,
    SortOrder,
)

MAX_BATCH_GET_ATTEMPTS = 5


class ReadConfigurationRepository:
    """Load only the Session HMAC key needed by the Read Lambda."""

    def __init__(self, client: SSMClient, parameter_name: str) -> None:
        self._client = client
        self._name = parameter_name
        self._cached: bytes | None = None

    def load_session_key(self) -> bytes:
        if self._cached is not None:
            return self._cached
        response = self._client.get_parameters(Names=[self._name], WithDecryption=True)
        if response.get("InvalidParameters"):
            raise AuthFailure("configuration_unavailable")
        parameters = response.get("Parameters", [])
        if len(parameters) != 1 or parameters[0].get("Name") != self._name:
            raise AuthFailure("configuration_unavailable")
        key = parameters[0].get("Value", "").encode()
        if len(key) < 32:
            raise AuthFailure("configuration_invalid")
        self._cached = key
        return key


class DynamoRecordsReader:
    """Read immutable archive records and expiring requester profiles."""

    def __init__(
        self,
        client: DynamoDBClient,
        s3_client: S3Client,
        *,
        archive_table_name: str,
        statistics_table_name: str,
        session_table_name: str,
        media_bucket_name: str,
    ) -> None:
        self._client = client
        self._s3 = s3_client
        self._archive_table = archive_table_name
        self._statistics_table = statistics_table_name
        self._session_table = session_table_name
        self._media_bucket = media_bucket_name

    def list_meta(
        self,
        *,
        limit: int,
        sort: SortOrder,
        winner: ParticipantSlot | None,
        exclusive_start_key: DynamoItem | None,
    ) -> ArchivePage:
        index_name = "gsi2" if winner else "gsi1"
        partition_value = f"WINNER#{winner}" if winner else "ARCHIVE#COMPLETED"
        pk_name = f"{index_name}pk"
        expression = "#pk = :pk"
        values: DynamoItem = {":pk": partition_value}
        parameters: dict[str, Any] = {
            "TableName": self._archive_table,
            "IndexName": index_name,
            "KeyConditionExpression": expression,
            "ExpressionAttributeNames": {"#pk": pk_name},
            "ExpressionAttributeValues": marshal_item(values),
            "ScanIndexForward": sort == "oldest",
            "Limit": limit,
        }
        if exclusive_start_key is not None:
            parameters["ExclusiveStartKey"] = marshal_item(exclusive_start_key)
        response = self._client.query(**parameters)
        items = tuple(unmarshal_item(item) for item in response.get("Items", []))
        if any(item.get("record_type") != "archive_meta" for item in items):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        raw_key = response.get("LastEvaluatedKey")
        last_key = None if not raw_key else unmarshal_item(raw_key)
        return ArchivePage(items=items, last_evaluated_key=last_key, index_name=index_name)

    def load_record(self, *, record_id: str) -> tuple[DynamoItem, ...]:
        items: list[DynamoItem] = []
        start_key: dict[str, AttributeValueTypeDef] | None = None
        while True:
            parameters: dict[str, Any] = {
                "TableName": self._archive_table,
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": marshal_item({":pk": f"RECORD#{record_id}"}),
                "ConsistentRead": True,
            }
            if start_key is not None:
                parameters["ExclusiveStartKey"] = start_key
            response = self._client.query(**parameters)
            items.extend(unmarshal_item(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        return tuple(items)

    def load_ranking_snapshots(self) -> tuple[DynamoItem, ...]:
        response = self._client.transact_get_items(
            TransactItems=[
                {
                    "Get": {
                        "TableName": self._statistics_table,
                        "Key": marshal_item({"PK": pk, "SK": "CURRENT"}),
                    }
                }
                for pk in ("RANKING#WINS", "RANKING#REQUESTS")
            ]
        )
        return tuple(
            unmarshal_item(raw["Item"])
            for raw in response.get("Responses", [])
            if raw.get("Item") is not None
        )

    def load_profiles(self, *, requester_keys: tuple[str, ...]) -> dict[str, RequesterProfile]:
        if not requester_keys:
            return {}
        if len(requester_keys) > 50 or len(set(requester_keys)) != len(requester_keys):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        request_items: dict[str, Any] = {
            self._session_table: {
                "Keys": [
                    marshal_item({"PK": "PROFILE#REQUESTER", "SK": key}) for key in requester_keys
                ],
                "ConsistentRead": True,
            }
        }
        items: list[dict[str, AttributeValueTypeDef]] = []
        for _attempt in range(MAX_BATCH_GET_ATTEMPTS):
            response = self._client.batch_get_item(RequestItems=request_items)
            items.extend(response.get("Responses", {}).get(self._session_table, []))
            unprocessed = response.get("UnprocessedKeys", {})
            if not unprocessed:
                break
            request_items = cast(dict[str, Any], unprocessed)
        else:
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        profiles: dict[str, RequesterProfile] = {}
        for raw in items:
            item = unmarshal_item(raw)
            key = item.get("SK")
            name = item.get("display_name")
            avatar = item.get("avatar_asset_key")
            legacy_expires_at = item.get("expiresAt")
            if (
                item.get("schema_version") != 1
                or item.get("record_type") != "requester_profile"
                or not isinstance(key, str)
                or key not in requester_keys
                or not isinstance(name, str)
                or not name.strip()
                or (avatar is not None and not isinstance(avatar, str))
                or (
                    legacy_expires_at is not None
                    and (
                        isinstance(legacy_expires_at, bool)
                        or not isinstance(legacy_expires_at, int)
                    )
                )
                or key in profiles
            ):
                raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
            profiles[key] = RequesterProfile(
                display_name=name,
                avatar_asset_key=avatar,
            )
        return profiles

    def avatar_url(self, *, asset_key: str) -> str:
        if not asset_key.startswith(("participants/", "requesters/")):
            raise ReadFailure("ARCHIVE_UNAVAILABLE", 503)
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._media_bucket, "Key": asset_key},
            ExpiresIn=300,
        )
