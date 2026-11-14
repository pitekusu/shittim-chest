"""Read and sign gallery assets using the existing Records storage clients."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item

from shittim_records.gallery_keys import GALLERY_RESULT_PREFIX
from shittim_records.gallery_read import (
    GALLERY_IMAGE_URL_TTL_SECONDS,
    GALLERY_PAGE_SIZE,
    GalleryAsset,
)
from shittim_records.read_adapters import MAX_BATCH_GET_ATTEMPTS, sign_private_image
from shittim_records.read_api import ReadFailure

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef
    from mypy_boto3_s3.client import S3Client
    from shittim_chest.adapters.dynamodb.serializer import DynamoItem


class DynamoGalleryStore:
    def __init__(self, dynamodb: DynamoDBClient, s3: S3Client, *, table: str, bucket: str) -> None:
        self._dynamodb = dynamodb
        self._s3 = s3
        self._table = table
        self._bucket = bucket

    def list_page(self, partition: str, after: str | None) -> tuple[list[DynamoItem], str | None]:
        arguments: dict[str, Any] = {}
        if after is not None:
            arguments["ExclusiveStartKey"] = marshal_item({"PK": partition, "SK": after})
        result = self._dynamodb.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues=marshal_item(
                {":pk": partition, ":prefix": GALLERY_RESULT_PREFIX}
            ),
            Limit=GALLERY_PAGE_SIZE,
            ScanIndexForward=False,
            **arguments,
        )
        last_key = result.get("LastEvaluatedKey")
        next_key = None
        if last_key:
            key = unmarshal_item(last_key)
            if key.get("PK") != partition or not isinstance(key.get("SK"), str):
                raise ReadFailure("GALLERY_UNAVAILABLE", 503)
            next_key = cast(str, key["SK"])
        return [unmarshal_item(item) for item in result.get("Items", [])], next_key

    def load_card(self, partition: str, sort_key: str) -> DynamoItem | None:
        result = self._dynamodb.get_item(
            TableName=self._table, Key=marshal_item({"PK": partition, "SK": sort_key})
        )
        item = result.get("Item")
        return unmarshal_item(item) if item is not None else None

    def load_cards(self, partition: str, sort_keys: tuple[str, ...]) -> list[DynamoItem]:
        request_items: dict[str, Any] = {
            self._table: {"Keys": [marshal_item({"PK": partition, "SK": key}) for key in sort_keys]}
        }
        items: list[dict[str, AttributeValueTypeDef]] = []
        for _attempt in range(MAX_BATCH_GET_ATTEMPTS):
            result = self._dynamodb.batch_get_item(RequestItems=request_items)
            items.extend(result.get("Responses", {}).get(self._table, []))
            pending = result.get("UnprocessedKeys", {})
            if not pending:
                return [unmarshal_item(item) for item in items]
            request_items = cast(dict[str, Any], pending)
        raise ReadFailure("GALLERY_UNAVAILABLE", 503)

    def sign(self, asset: GalleryAsset, *, thumbnail: bool) -> str:
        return sign_private_image(
            self._s3,
            bucket=self._bucket,
            key=asset.key,
            version=asset.version_id,
            content_type="image/webp" if thumbnail else "image/png",
            expires_in=GALLERY_IMAGE_URL_TTL_SECONDS,
        )
