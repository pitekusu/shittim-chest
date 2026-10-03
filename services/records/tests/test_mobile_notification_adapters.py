"""Actual boto resource document marshalling and Firebase public API boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.stub import ANY, Stubber
from firebase_admin import exceptions, messaging
from shittim_chest.adapters.dynamodb.codec import marshal_item

from shittim_records.mobile_notification_adapters import (
    DynamoMobileNotificationStore,
    FirebaseNotificationSender,
    SqsMobileNotificationQueue,
    _device_item,
    load_firebase_sender,
)
from shittim_records.mobile_notifications import (
    NotificationDevice,
    PushDeliveryFailed,
    device_token_hash,
    pending_mobile_notification,
    timestamp,
)

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def resource():
    return boto3.resource(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="invented",
        aws_secret_access_key="invented",  # noqa: S106 - Stubber-only credential.
    )


def device():
    return NotificationDevice(
        token="invented-fid",  # noqa: S106 - invented address, no FCM calls.
        token_hash=device_token_hash("invented-fid"),
        binding_id="b" * 43,
        session_hash="a" * 64,
        session_created_at=1000,
        registered_at=timestamp(NOW),
        expires_at=1000 + 90 * 24 * 60 * 60,
    )


def test_real_resource_marshals_transaction_and_paginator_native_types():
    dynamodb = resource()
    store = DynamoMobileNotificationStore(dynamodb, statistics="statistics", sessions="sessions")
    row = device()
    with Stubber(dynamodb.meta.client) as stub:
        stub.add_response(
            "get_item",
            {},
            {
                "TableName": "statistics",
                "Key": {"PK": "MOBILE_PUSH_DEVICE", "SK": row.token_hash},
                "ConsistentRead": True,
            },
        )
        stub.add_response("transact_write_items", {}, {"TransactItems": ANY})
        store.register(row, now_epoch=1001)
        stub.add_response(
            "query",
            {"Items": [marshal_item(cast(Any, _device_item(row)))]},
            {
                "TableName": "statistics",
                "KeyConditionExpression": "PK = :pk",
                "ExpressionAttributeValues": {":pk": "MOBILE_PUSH_DEVICE"},
                "ConsistentRead": True,
            },
        )
        assert list(store.devices()) == [row]
        stub.assert_no_pending_responses()


def test_resource_numbers_are_read_as_ints_for_outbox_deadlines():
    dynamodb = resource()
    store = DynamoMobileNotificationStore(dynamodb, statistics="statistics", sessions="sessions")
    row = pending_mobile_notification(record_id="r" * 43, created_at=NOW)
    with Stubber(dynamodb.meta.client) as stub:
        stub.add_response("get_item", {"Item": marshal_item(cast(Any, row))})
        value = store.get_event("r" * 43)
        assert value is not None and type(value["expiresAt"]) is int and type(value["runs"]) is int


def test_firebase_sender_uses_current_fid_data_only_high_priority(monkeypatch):
    calls = []
    monkeypatch.setattr(messaging, "send", lambda message, **kwargs: calls.append(message))
    sender = FirebaseNotificationSender(cast(Any, object()))
    data = {
        "type": "record_published",
        "schemaVersion": "1",
        "recordId": "r" * 43,
        "bindingId": "b" * 43,
        "publishedAt": timestamp(datetime.now(UTC)),
    }
    sender.send(token="invented-fid", data=data)  # noqa: S106 - invented address.
    message = calls[0]
    assert message.fid == "invented-fid" and message.token is None
    assert message.notification is None and message.data == data
    assert (
        message.android.priority == "high"
        and message.android.restricted_package_name == "dev.pitekusu.shittim.records"
    )


@pytest.mark.parametrize(
    "failure,code",
    [
        (messaging.UnregisteredError("private"), "invalid_token"),
        (exceptions.UnavailableError("private"), "unknown"),
        (
            exceptions.UnavailableError(
                "private", http_response=SimpleNamespace(headers={"Retry-After": "120"})
            ),
            "retryable",
        ),
    ],
)
def test_provider_errors_sanitized_unknown_not_retryable(monkeypatch, failure, code):
    def failed(*args, **kwargs):
        raise failure

    monkeypatch.setattr(messaging, "send", failed)
    with pytest.raises(PushDeliveryFailed) as caught:
        FirebaseNotificationSender(cast(Any, object())).send(
            token="private-address",  # noqa: S106 - invented fixture.
            data={"publishedAt": timestamp(NOW)},
        )
    assert caught.value.code == code and "private" not in str(caught.value)
    if code == "retryable":
        assert caught.value.retry_after == 120


def test_uncreated_firebase_parameter_is_explicitly_disabled():
    class Missing(Exception):
        pass

    def missing(**kwargs):
        raise Missing

    ssm = SimpleNamespace(
        get_parameter=missing, exceptions=SimpleNamespace(ParameterNotFound=Missing)
    )
    assert load_firebase_sender(ssm, "/invented/firebase") is None


def test_queue_contains_only_record_id_with_fifo_deduplication():
    calls = []
    client = SimpleNamespace(send_message=lambda **kwargs: calls.append(kwargs))
    queue = SqsMobileNotificationQueue(client, "https://sqs.invalid/mobile-push.fifo")
    queue.enqueue("r" * 43)
    queue.enqueue("r" * 43)
    queue.enqueue("r" * 43, generation="2")
    assert calls[0] == calls[1] and calls[0]["MessageGroupId"] == "mobile-push"
    assert calls[0]["MessageDeduplicationId"] != calls[2]["MessageDeduplicationId"]
    assert calls[0]["MessageBody"] == '{"recordId":"' + "r" * 43 + '"}'
