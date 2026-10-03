"""DynamoDB receipts and Firebase Admin SDK; no content or token logging."""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import firebase_admin
from boto3.dynamodb.conditions import Attr
from firebase_admin import credentials, exceptions, messaging
from pydantic import ValidationError

from shittim_records.auth import AuthFailure
from shittim_records.mobile_notifications import (
    DELIVERY_PREFIX,
    DEVICE_PK,
    MAX_DELIVERY_ATTEMPTS,
    OUTBOX_PK,
    NotificationDevice,
    PushDeliveryFailed,
    device_token_hash,
    timestamp,
)

_FIREBASE_CREDENTIAL_FINGERPRINT: str | None = None


class DynamoMobileNotificationStore:
    """Use resource/native types; CAS transaction only where a session race matters."""

    def __init__(
        self, resource: Any, *, statistics: str, sessions: str, archive: str | None = None
    ) -> None:
        self._table = resource.Table(statistics)
        self._archive = None if archive is None else resource.Table(archive)
        self._client = resource.meta.client
        self._session_table = sessions

    def register(self, device: NotificationDevice, *, now_epoch: int) -> None:
        previous = self.current_device(device.token_hash)
        if previous is not None and previous.session_created_at > device.session_created_at:
            raise AuthFailure("mobile_request_invalid")
        if previous is not None and (
            previous.binding_id == device.binding_id
            and previous.session_hash == device.session_hash
        ):
            # Foreground/token refresh does not make a previously registered device new.
            device = device.model_copy(update={"registered_at": previous.registered_at})
        condition = "attribute_not_exists(PK)"
        values: dict[str, str] = {}
        if previous is not None:
            condition = "binding_id = :binding AND session_hash = :session"
            values = {":binding": previous.binding_id, ":session": previous.session_hash}
        put: dict[str, Any] = {
            "TableName": self._table.name,
            "Item": _device_item(device),
            "ConditionExpression": condition,
        }
        if values:
            put["ExpressionAttributeValues"] = values
        # A simple Put cannot prevent logout after authentication but before registration.
        # Keep session existence/absolute expiry and device rebind in one transaction.
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._session_table,
                            "Key": {"PK": f"MOBILE_SESSION#{device.session_hash}", "SK": "META"},
                            "ConditionExpression": "expiresAt = :expiry AND expiresAt > :now",
                            "ExpressionAttributeValues": {
                                ":expiry": device.expires_at,
                                ":now": now_epoch,
                            },
                        }
                    },
                    {"Put": put},
                ]
            )
        except self._client.exceptions.TransactionCanceledException:
            raise AuthFailure("mobile_registration_unavailable") from None

    def unregister(self, *, token_hash: str, binding_id: str, session_hash: str) -> None:
        try:
            self._table.delete_item(
                Key={"PK": DEVICE_PK, "SK": token_hash},
                ConditionExpression=Attr("binding_id").eq(binding_id)
                & Attr("session_hash").eq(session_hash),
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            # A delayed DELETE or invalid-token result must not erase a newer login.
            return

    def current_device(self, token_hash: str) -> NotificationDevice | None:
        item = self._table.get_item(
            Key={"PK": DEVICE_PK, "SK": token_hash}, ConsistentRead=True
        ).get("Item")
        return None if item is None else _read_device(item)

    def devices(self, *, after: str | None = None) -> Iterable[NotificationDevice]:
        # No full-table Scan or truncated first page: Query the dedicated registry PK.
        paginator = self._client.get_paginator("query")
        arguments: dict[str, Any] = {
            "TableName": self._table.name,
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": DEVICE_PK},
            "ConsistentRead": True,
        }
        if after is not None:
            arguments["ExclusiveStartKey"] = {"PK": DEVICE_PK, "SK": after}
        for page in paginator.paginate(**arguments):
            for item in page.get("Items", []):
                yield _read_device(item)

    def checkpoint(self, record_id: str, *, cursor: str | None, retry_needed: bool) -> None:
        self._table.update_item(
            Key={"PK": OUTBOX_PK, "SK": record_id},
            UpdateExpression="SET #cursor = :cursor, retry_needed = :retry",
            ExpressionAttributeNames={"#cursor": "cursor"},
            ExpressionAttributeValues={":cursor": cursor, ":retry": retry_needed},
            ConditionExpression=Attr("state").eq("processing"),
        )

    def cleanup(self, *, now_epoch: int, limit: int = 100) -> int:
        """Statistics has no TTL: delete expired private addresses and bounded receipts."""
        deleted = 0
        for device in self.devices():
            if device.expires_at <= now_epoch:
                self.unregister(
                    token_hash=device.token_hash,
                    binding_id=device.binding_id,
                    session_hash=device.session_hash,
                )
                deleted += 1
                if deleted >= limit:
                    return deleted
        paginator = self._client.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table.name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": OUTBOX_PK},
            ConsistentRead=True,
        ):
            for item in page.get("Items", []):
                if item.get("expiresAt", now_epoch + 1) > now_epoch:
                    continue
                for receipts in paginator.paginate(
                    TableName=self._table.name,
                    KeyConditionExpression="PK = :pk",
                    ExpressionAttributeValues={":pk": DELIVERY_PREFIX + item["record_id"]},
                    ProjectionExpression="PK, SK",
                    ConsistentRead=True,
                ):
                    for receipt in receipts.get("Items", []):
                        self._table.delete_item(Key={"PK": receipt["PK"], "SK": receipt["SK"]})
                        deleted += 1
                        if deleted >= limit:
                            return deleted
                self._table.delete_item(
                    Key={"PK": OUTBOX_PK, "SK": item["SK"]},
                    ConditionExpression=Attr("expiresAt").lte(now_epoch),
                )
                deleted += 1
                if deleted >= limit:
                    return deleted
        return deleted

    def get_event(self, record_id: str) -> Mapping[str, object] | None:
        item = self._table.get_item(
            Key={"PK": OUTBOX_PK, "SK": record_id}, ConsistentRead=True
        ).get("Item")
        if item is not None:
            for field in ("schema_version", "expiresAt", "runs", "lease_until"):
                if field in item:
                    item[field] = int(item[field])
        return item

    def pending_events(self, *, now_epoch: int) -> Iterable[str]:
        paginator = self._client.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table.name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": OUTBOX_PK},
            ConsistentRead=True,
        ):
            for item in page.get("Items", []):
                if item.get("state") == "pending" or (
                    item.get("state") == "processing" and item.get("lease_until", 0) <= now_epoch
                ):
                    yield item["record_id"]

    def claim_event(self, record_id: str, *, now_epoch: int) -> bool:
        try:
            self._table.update_item(
                Key={"PK": OUTBOX_PK, "SK": record_id},
                UpdateExpression="SET #state = :processing, lease_until = :lease ADD runs :one",
                ConditionExpression=Attr("state").eq("pending")
                | (Attr("state").eq("processing") & Attr("lease_until").lte(now_epoch)),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":processing": "processing",
                    ":lease": now_epoch + 180,
                    ":one": 1,
                },
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def finish_event(self, record_id: str, *, state: str, wait_only: bool = False) -> None:
        update = "SET #state = :state REMOVE lease_until"
        values: dict[str, str | int] = {":state": state}
        if wait_only:
            # Refund only normally completed wait-only sweeps, never a crashed lease.
            update += " ADD runs :refund"
            values[":refund"] = -1
        self._table.update_item(
            Key={"PK": OUTBOX_PK, "SK": record_id},
            UpdateExpression=update,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
            ConditionExpression=Attr("state").eq("processing"),
        )

    def claim_delivery(self, record_id: str, device: NotificationDevice, *, now_epoch: int) -> str:
        key = _receipt_key(record_id, device)
        old = self._table.get_item(Key=key, ConsistentRead=True).get("Item")
        if old is not None:
            if old.get("state") != "retry":
                return "done"  # Includes ambiguous prior in-flight send after a worker crash.
            if old.get("attempts", MAX_DELIVERY_ATTEMPTS) >= MAX_DELIVERY_ATTEMPTS:
                return "done"
            if old.get("retry_at", now_epoch + 1) > now_epoch:
                return "wait"
        attempts = 1 if old is None else int(old["attempts"]) + 1
        condition = (
            Attr("PK").not_exists()
            if old is None
            else Attr("state").eq("retry") & Attr("attempts").eq(old["attempts"])
        )
        try:
            self._table.put_item(
                Item={
                    **key,
                    "state": "sending",
                    "attempts": attempts,
                    "expiresAt": now_epoch + 2 * 24 * 60 * 60,
                },
                ConditionExpression=condition,
            )
        except self._client.exceptions.ConditionalCheckFailedException:
            return "done"
        return "send"

    def finish_delivery(
        self,
        record_id: str,
        device: NotificationDevice,
        *,
        state: str,
        now_epoch: int,
        retry_after: int = 60,
    ) -> None:
        self._table.update_item(
            Key=_receipt_key(record_id, device),
            UpdateExpression="SET #state = :state, retry_at = :retry_at",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":state": state, ":retry_at": now_epoch + retry_after},
            ConditionExpression=Attr("state").eq("sending"),
        )

    def record_requester_name(self, record_id: str) -> str | None:
        if self._archive is None:
            raise ValueError("mobile_push_archive_unavailable")
        item = self._archive.get_item(
            Key={"PK": f"RECORD#{record_id}", "SK": "META"},
            ConsistentRead=True,
            ProjectionExpression="PK, SK, record_id, record_type, requester_display_name",
        ).get("Item")
        if (
            item is None
            or item.get("record_type") != "archive_meta"
            or item.get("record_id") != record_id
        ):
            return None
        name = item.get("requester_display_name")
        return name if isinstance(name, str) else "依頼者"


def _device_item(device: NotificationDevice) -> dict[str, Any]:
    return {
        "PK": DEVICE_PK,
        "SK": device.token_hash,
        "schema_version": 1,
        **device.model_dump(exclude={"expires_at"}),
        "expiresAt": device.expires_at,
    }


def _read_device(item: Mapping[str, Any]) -> NotificationDevice:
    try:
        values = {key: item[key] for key in NotificationDevice.model_fields if key != "expires_at"}
        values["session_created_at"] = int(values["session_created_at"])
        values["expires_at"] = int(item["expiresAt"])
        device = NotificationDevice.model_validate(values)
        if (
            item.get("PK") != DEVICE_PK
            or item.get("SK") != device.token_hash
            or device_token_hash(device.token) != device.token_hash
        ):
            raise ValueError
        if timestamp(datetime.fromisoformat(device.registered_at)) != device.registered_at:
            raise ValueError
        return device
    except KeyError, ValueError, TypeError, ValidationError:
        raise ValueError("mobile_push_device_invalid") from None


def _receipt_key(record_id: str, device: NotificationDevice) -> dict[str, str]:
    return {"PK": DELIVERY_PREFIX + record_id, "SK": f"{device.token_hash}#{device.binding_id}"}


class SqsMobileNotificationQueue:
    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def enqueue(self, record_id: str, *, generation: str = "published") -> None:
        self._client.send_message(
            QueueUrl=self._url,
            MessageBody=json.dumps({"recordId": record_id}, separators=(",", ":")),
            MessageGroupId="mobile-push",
            MessageDeduplicationId=hashlib.sha256(f"{record_id}:{generation}".encode()).hexdigest(),
        )


class FirebaseNotificationSender:
    """Public Admin SDK owns credentials, message serialization and bounded retries.

    Its synchronous HTTP client retries connect/read once and HTTP 500/503 four
    times. The five-second per-request timeout bounds that; application receipts
    never retry unknown outcomes. No private SDK transport monkey-patching.
    """

    def __init__(self, app: firebase_admin.App) -> None:
        self._app = app

    def send(self, *, token: str, data: dict[str, str]) -> None:
        try:
            messaging.send(
                messaging.Message(
                    # API's private `token` address carries an installation ID (FID).
                    # Admin 7.7 uses the current high-level field, not deprecated token.
                    fid=token,
                    data=data,
                    # FIDs are scoped to this credentialed Firebase project; dispatch
                    # rechecks the authenticated session and binding. A release-only
                    # package restriction would reject the registered .dev app.
                    android=messaging.AndroidConfig(
                        priority="high",
                        ttl=max(
                            timedelta(seconds=0),
                            timedelta(hours=24)
                            - (datetime.now(UTC) - datetime.fromisoformat(data["publishedAt"])),
                        ),
                    ),
                ),
                app=self._app,
            )
        except (
            messaging.UnregisteredError,
            messaging.SenderIdMismatchError,
            exceptions.InvalidArgumentError,
        ):
            raise PushDeliveryFailed("invalid_token") from None
        except (messaging.QuotaExceededError, exceptions.UnavailableError) as error:
            # Only a provider response proves a retryable rejection. Socket failures
            # may have happened after acceptance; treat them as unknown/terminal.
            response = error.http_response
            if response is None:
                raise PushDeliveryFailed("unknown") from None
            retry_after = response.headers.get("Retry-After", "60")
            delay = int(retry_after) if retry_after.isdecimal() else 60
            raise PushDeliveryFailed("retryable", retry_after=delay) from None
        except exceptions.FirebaseError:
            raise PushDeliveryFailed("failed") from None


def load_firebase_sender(ssm: Any, parameter_name: str) -> FirebaseNotificationSender | None:
    """Resolve only at Lambda runtime; the agent/operator never fetches this value."""
    global _FIREBASE_CREDENTIAL_FINGERPRINT

    try:
        raw = ssm.get_parameter(Name=parameter_name, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    try:
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "service_account"
            or payload.get("token_uri") != "https://oauth2.googleapis.com/token"
            or re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", payload.get("project_id", "")) is None
        ):
            raise ValueError
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        try:
            app = firebase_admin.get_app("records-mobile-notifications")
        except ValueError:
            app = None
        if app is None or fingerprint != _FIREBASE_CREDENTIAL_FINGERPRINT:
            # Validate before replacing the warm app, but never send with stale credentials.
            # JSON formatting alone must not recreate the SDK app or its HTTP client.
            credential = credentials.Certificate(payload)
            if app is not None:
                firebase_admin.delete_app(app)
            app = firebase_admin.initialize_app(
                credential,
                options={"httpTimeout": 5},
                name="records-mobile-notifications",
            )
            _FIREBASE_CREDENTIAL_FINGERPRINT = fingerprint
    except KeyError, ValueError, TypeError:
        raise RuntimeError("mobile_push_configuration_invalid") from None
    return FirebaseNotificationSender(app)
