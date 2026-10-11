"""Owner-bound image uploads with atomic ingress consumption and orphan fencing."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError
from pydantic import ValidationError
from shittim_chest.adapters.assets.images import DebateImageFailure, S3DebateImageStore
from shittim_chest.adapters.dynamodb.codec import marshal_item, unmarshal_item
from shittim_chest.domain.attachments import DebateImage

from shittim_records.contracts import DebateAttachmentUploadRequest

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.client import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef

    from shittim_records.contracts import DebateAttachmentUploadRequest

RESERVATION_TTL = timedelta(days=1)
PROMOTION_LEASE = timedelta(minutes=2)


class AttachmentFailure(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def image_payload(image: DebateImage) -> dict[str, Any]:
    return asdict(image)


def public_image(image: DebateImage) -> dict[str, Any]:
    return {
        "attachmentId": image.attachment_id,
        "contentType": image.content_type,
        "sizeBytes": image.size_bytes,
        "sha256": image.sha256,
        "width": image.width,
        "height": image.height,
    }


class DynamoDebateAttachmentRepository:
    # The client is required for the existing Core cross-table enqueue transaction;
    # the same codec ensures its exact image consumption fence uses identical data.
    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        self.client, self.table_name = client, table_name

    def get(self, owner: str, attachment_id: str) -> dict[str, Any] | None:
        raw = self.client.get_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": f"DEBATE_UPLOAD#{owner}", "SK": f"IMAGE#{attachment_id}"}),
            ConsistentRead=True,
        ).get("Item")
        if raw is None:
            return None
        item: dict[str, Any] = unmarshal_item(raw)
        try:
            payload = DebateAttachmentUploadRequest.model_validate(
                {
                    "requestId": item["request_id"],
                    "attachmentId": item["attachment_id"],
                    "contentType": item["content_type"],
                    "sizeBytes": item["size_bytes"],
                    "sha256": item["sha256"],
                },
                strict=True,
            )
            if (
                item.get("PK") != f"DEBATE_UPLOAD#{owner}"
                or item.get("SK") != f"IMAGE#{attachment_id}"
                or item.get("owner_key") != owner
                or payload.attachment_id != attachment_id
                or item.get("record_type") != "debate_image_upload"
                or type(item.get("schema_version")) is not int
                or item["schema_version"] != 1
                or item.get("state")
                not in {"reserved", "promoting", "ready", "consumed", "expired"}
                or type(item.get("expires_at")) is not int
                or item["expires_at"] < 0
                or not isinstance(item.get("upload_key"), str)
                or re.fullmatch(r"debate-uploads/[A-Za-z0-9_-]{43}\.bin", item["upload_key"])
                is None
            ):
                raise ValueError
            if item["state"] in {"ready", "consumed"} or "image" in item:
                image = DebateImage(**item["image"])
                if (image.attachment_id, image.content_type, image.size_bytes, image.sha256) != (
                    payload.attachment_id,
                    payload.content_type,
                    payload.size_bytes,
                    payload.sha256,
                ):
                    raise ValueError
            binding_raw = self.client.get_item(
                TableName=self.table_name,
                Key=marshal_item({"PK": f"DEBATE_UPLOAD_ID#{attachment_id}", "SK": "OWNER"}),
                ConsistentRead=True,
            ).get("Item")
            binding = None if binding_raw is None else unmarshal_item(binding_raw)
            if (
                binding is None
                or binding.get("owner_key") != owner
                or binding.get("request_id") != payload.request_id
            ):
                raise ValueError
        except KeyError, TypeError, ValueError, ValidationError:
            raise AttachmentFailure("ATTACHMENT_UNAVAILABLE", 503) from None
        return item

    def reserve(
        self, owner: str, payload: DebateAttachmentUploadRequest, now: datetime
    ) -> dict[str, Any]:
        existing = self.get(owner, payload.attachment_id)
        identity = {
            "request_id": payload.request_id,
            "attachment_id": payload.attachment_id,
            "content_type": payload.content_type,
            "size_bytes": payload.size_bytes,
            "sha256": payload.sha256,
        }
        if existing is not None:
            if any(existing.get(k) != v for k, v in identity.items()):
                raise AttachmentFailure("ATTACHMENT_CONFLICT")
            if existing["state"] == "expired" or (
                existing["state"] != "consumed" and existing["expires_at"] <= int(now.timestamp())
            ):
                raise AttachmentFailure("ATTACHMENT_EXPIRED")
            return existing
        expiry = int((now + RESERVATION_TTL).timestamp())
        item = {
            "PK": f"DEBATE_UPLOAD#{owner}",
            "SK": f"IMAGE#{payload.attachment_id}",
            "record_type": "debate_image_upload",
            "schema_version": 1,
            "owner_key": owner,
            **identity,
            "state": "reserved",
            "upload_key": f"debate-uploads/{secrets.token_urlsafe(32)}.bin",
            "expires_at": expiry,
            "gsi1pk": "ATTACHMENT#EXPIRY",
            "gsi1sk": f"{expiry:020d}#{payload.attachment_id}",
        }
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": marshal_item(
                                {
                                    "PK": f"DEBATE_UPLOAD_ID#{payload.attachment_id}",
                                    "SK": "OWNER",
                                    "owner_key": owner,
                                    "request_id": payload.request_id,
                                }
                            ),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table_name,
                            "Item": marshal_item(item),
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": marshal_item(
                                {"PK": item["PK"], "SK": f"REQUEST#{payload.request_id}"}
                            ),
                            "UpdateExpression": "ADD image_count :one",
                            "ConditionExpression": (
                                "attribute_not_exists(image_count) OR image_count < :maximum"
                            ),
                            "ExpressionAttributeValues": marshal_item({":one": 1, ":maximum": 3}),
                        }
                    },
                ]
            )
        except self.client.exceptions.TransactionCanceledException:
            existing = self.get(owner, payload.attachment_id)
            if existing is not None:
                return self.reserve(owner, payload, now)
            binding = self.client.get_item(
                TableName=self.table_name,
                Key=marshal_item(
                    {"PK": f"DEBATE_UPLOAD_ID#{payload.attachment_id}", "SK": "OWNER"}
                ),
                ConsistentRead=True,
            ).get("Item")
            if binding is not None:
                raise AttachmentFailure("ATTACHMENT_CONFLICT") from None
            raise AttachmentFailure("ATTACHMENT_LIMIT", 400) from None
        return item

    def claim(self, item: dict[str, Any], now: datetime) -> str:
        token = secrets.token_urlsafe(16)
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                UpdateExpression=(
                    "SET #state = :promoting, claim_token = :token, claim_until = :lease"
                ),
                ConditionExpression=(
                    "expires_at > :now AND (#state = :reserved OR (#state = "
                    ":promoting AND claim_until <= :now))"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=marshal_item(
                    {
                        ":promoting": "promoting",
                        ":reserved": "reserved",
                        ":token": token,
                        ":lease": int((now + PROMOTION_LEASE).timestamp()),
                        ":now": int(now.timestamp()),
                    }
                ),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            raise AttachmentFailure("ATTACHMENT_NOT_READY") from None
        return token

    def complete(self, item: dict[str, Any], token: str, image: DebateImage, now: datetime) -> None:
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                UpdateExpression=(
                    "SET #state = :ready, image = :image REMOVE claim_token, claim_until"
                ),
                ConditionExpression=(
                    "#state = :promoting AND claim_token = :token AND expires_at "
                    "> :now AND claim_until > :now"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=marshal_item(
                    {
                        ":promoting": "promoting",
                        ":ready": "ready",
                        ":token": token,
                        ":image": image_payload(image),
                        ":now": int(now.timestamp()),
                    }
                ),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            raise AttachmentFailure("ATTACHMENT_NOT_READY") from None

    def release(self, item: dict[str, Any], token: str) -> None:
        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                UpdateExpression="SET #state = :reserved REMOVE claim_token, claim_until",
                ConditionExpression="#state = :promoting AND claim_token = :token",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues=marshal_item(
                    {":reserved": "reserved", ":promoting": "promoting", ":token": token}
                ),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return

    def ready_images(
        self, owner: str, request_id: str, attachment_ids: tuple[str, ...], now: datetime
    ) -> tuple[DebateImage, ...]:
        result = []
        for attachment_id in attachment_ids:
            item = self.get(owner, attachment_id)
            if item is None:
                raise AttachmentFailure("ATTACHMENT_NOT_FOUND", 404)
            if item["request_id"] != request_id:
                raise AttachmentFailure("ATTACHMENT_CONFLICT")
            if item["expires_at"] <= int(now.timestamp()) or item["state"] == "expired":
                raise AttachmentFailure("ATTACHMENT_EXPIRED")
            if item["state"] != "ready":
                raise AttachmentFailure("ATTACHMENT_NOT_READY")
            result.append(DebateImage(**item["image"]))
        return tuple(result)

    def consume_actions(
        self, owner: str, request_id: str, images: tuple[DebateImage, ...], now: datetime
    ) -> tuple[TransactWriteItemTypeDef, ...]:
        return tuple(
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": marshal_item(
                        {"PK": f"DEBATE_UPLOAD#{owner}", "SK": f"IMAGE#{image.attachment_id}"}
                    ),
                    "UpdateExpression": "SET #state = :consumed REMOVE gsi1pk, gsi1sk",
                    "ConditionExpression": (
                        "#state = :ready AND request_id = :request AND image = :image"
                        " AND expires_at > :now"
                    ),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": marshal_item(
                        {
                            ":consumed": "consumed",
                            ":ready": "ready",
                            ":request": request_id,
                            ":image": image_payload(image),
                            ":now": int(now.timestamp()),
                        }
                    ),
                }
            }
            for image in images
        )

    def expire(self, now: datetime, *, limit: int = 100) -> Iterator[dict[str, Any]]:
        pages = self.client.get_paginator("query").paginate(
            TableName=self.table_name,
            IndexName="gsi1",
            KeyConditionExpression="gsi1pk = :pk AND gsi1sk <= :cutoff",
            ExpressionAttributeValues=marshal_item(
                {":pk": "ATTACHMENT#EXPIRY", ":cutoff": f"{int(now.timestamp()):020d}#~"}
            ),
            PaginationConfig={"PageSize": limit},
        )
        for page in pages:
            for raw in page.get("Items", []):
                candidate = unmarshal_item(raw)
                owner, attachment_id = candidate.get("owner_key"), candidate.get("attachment_id")
                if not isinstance(owner, str) or not isinstance(attachment_id, str):
                    raise AttachmentFailure("ATTACHMENT_UNAVAILABLE", 503)
                item = self.get(owner, attachment_id)
                if item is None:
                    continue
                try:
                    self.client.update_item(
                        TableName=self.table_name,
                        Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
                        UpdateExpression="SET #state = :expired",
                        ConditionExpression=(
                            "expires_at <= :now AND (#state = :reserved OR #state = "
                            ":ready OR #state = :expired OR (#state = :promoting AND "
                            "claim_until <= :now))"
                        ),
                        ExpressionAttributeNames={"#state": "state"},
                        ExpressionAttributeValues=marshal_item(
                            {
                                ":expired": "expired",
                                ":reserved": "reserved",
                                ":ready": "ready",
                                ":promoting": "promoting",
                                ":now": int(now.timestamp()),
                            }
                        ),
                    )
                except self.client.exceptions.ConditionalCheckFailedException:
                    continue
                yield item

    def cleaned(self, item: dict[str, Any]) -> None:
        self.client.update_item(
            TableName=self.table_name,
            Key=marshal_item({"PK": item["PK"], "SK": item["SK"]}),
            UpdateExpression="REMOVE gsi1pk, gsi1sk",
            ConditionExpression="#state = :expired",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=marshal_item({":expired": "expired"}),
        )


class DebateAttachmentService:
    def __init__(
        self, repository: DynamoDebateAttachmentRepository, assets: S3DebateImageStore
    ) -> None:
        self.repository, self.assets = repository, assets

    def reserve(
        self, owner: str, payload: DebateAttachmentUploadRequest, now: datetime
    ) -> dict[str, Any]:
        item = self.repository.reserve(owner, payload, now)
        if item["state"] != "reserved":
            raise AttachmentFailure("ATTACHMENT_CONFLICT")
        ticket = self.assets.upload_ticket(
            key=item["upload_key"],
            content_type=payload.content_type,
            sha256=payload.sha256,
            size_bytes=payload.size_bytes,
        )
        return {
            "schemaVersion": 1,
            "attachmentId": payload.attachment_id,
            "method": "POST",
            "uploadUrl": ticket["url"],
            "fields": ticket["fields"],
            "expiresAt": (now + timedelta(minutes=10)).isoformat(),
        }

    def status(self, owner: str, attachment_id: str, now: datetime) -> dict[str, Any]:
        item = self.repository.get(owner, attachment_id)
        if item is None:
            raise AttachmentFailure("ATTACHMENT_NOT_FOUND", 404)
        state = item["state"]
        if state not in {"consumed", "expired"} and item["expires_at"] <= int(now.timestamp()):
            state = "expired"
        return {
            "schemaVersion": 1,
            "attachmentId": attachment_id,
            "requestId": item["request_id"],
            "status": state,
            "image": public_image(DebateImage(**item["image"])) if "image" in item else None,
        }

    def complete(self, owner: str, attachment_id: str, now: datetime) -> dict[str, Any]:
        item = self.repository.get(owner, attachment_id)
        if item is None:
            raise AttachmentFailure("ATTACHMENT_NOT_FOUND", 404)
        if item["state"] in {"ready", "consumed"}:
            return self.status(owner, attachment_id, now)
        if item["state"] == "expired" or item["expires_at"] <= int(now.timestamp()):
            raise AttachmentFailure("ATTACHMENT_EXPIRED")
        token = self.repository.claim(item, now)
        try:
            image = self.assets.import_upload(
                attachment_id=attachment_id,
                key=item["upload_key"],
                content_type=item["content_type"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
            )
            self.repository.complete(item, token, image, datetime.now(UTC))
        except ClientError as error:
            self.repository.release(item, token)
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise AttachmentFailure("ATTACHMENT_NOT_READY") from None
            raise AttachmentFailure("ATTACHMENT_UNAVAILABLE", 503) from None
        except DebateImageFailure as error:
            self.repository.release(item, token)
            raise AttachmentFailure(
                error.code, 400 if error.code == "ATTACHMENT_INVALID" else 409
            ) from None
        except Exception:
            self.repository.release(item, token)
            raise
        return self.status(owner, attachment_id, datetime.now(UTC))

    def cleanup(self, now: datetime) -> int:
        count = 0
        for item in self.repository.expire(now):
            self.assets.delete_orphan(item["attachment_id"], item["content_type"], item["sha256"])
            self.repository.cleaned(item)
            count += 1
        return count
