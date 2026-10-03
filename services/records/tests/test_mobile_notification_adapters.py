"""Actual boto resource document marshalling and Firebase public API boundaries."""

import json
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import boto3
import firebase_admin
import pytest
from botocore.stub import ANY, Stubber
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from firebase_admin import exceptions, messaging
from shittim_chest.adapters.dynamodb.codec import marshal_item

from shittim_records import mobile_notification_adapters as adapters
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
    monkeypatch.setattr(
        messaging, "send", lambda message, **kwargs: calls.append((message, kwargs))
    )
    app = cast(Any, object())
    sender = FirebaseNotificationSender(app)
    data = {
        "type": "record_published",
        "schemaVersion": "1",
        "recordId": "r" * 43,
        "bindingId": "b" * 43,
        "publishedAt": timestamp(datetime.now(UTC)),
    }
    sender.send(token="invented-fid", data=data)  # noqa: S106 - invented address.
    message, kwargs = calls[0]
    assert kwargs == {"app": app}
    assert message.fid == "invented-fid" and message.token is None
    assert message.notification is None and message.data == data
    assert message.android.priority == "high"
    assert message.android.restricted_package_name is None


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


@pytest.fixture
def firebase_credential(monkeypatch):
    monkeypatch.setattr(adapters, "_FIREBASE_CREDENTIAL_FINGERPRINT", None)
    # This key is generated only for the in-process SDK test; no cloud calls or saved files.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    yield {
        "type": "service_account",
        "project_id": "invented-project",
        "private_key_id": "invented-key",
        "private_key": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        "client_email": "notifications@invented-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    with suppress(ValueError):
        firebase_admin.delete_app(firebase_admin.get_app("records-mobile-notifications"))


def ssm_client():
    return boto3.client(
        "ssm",
        region_name="ap-northeast-1",
        aws_access_key_id="invented",
        aws_secret_access_key="invented",  # noqa: S106 - Stubber-only credential.
    )


def add_firebase_parameter(stub, value):
    stub.add_response(
        "get_parameter",
        {"Parameter": {"Value": value}},
        {"Name": "/invented/firebase", "WithDecryption": True},
    )


def test_warm_firebase_app_reused_for_equivalent_parameter_json(firebase_credential):
    ssm = ssm_client()
    with Stubber(ssm) as stub:
        add_firebase_parameter(stub, json.dumps(firebase_credential))
        first = load_firebase_sender(ssm, "/invented/firebase")
        add_firebase_parameter(stub, json.dumps(firebase_credential, sort_keys=True, indent=2))
        second = load_firebase_sender(ssm, "/invented/firebase")
        assert first is not None and second is not None
        assert first._app is second._app
        stub.assert_no_pending_responses()


def test_warm_firebase_app_replaced_after_credential_rotation(firebase_credential, monkeypatch):
    deleted = []
    delete_app = firebase_admin.delete_app

    def delete(app):
        deleted.append(app)
        delete_app(app)

    monkeypatch.setattr(firebase_admin, "delete_app", delete)
    ssm = ssm_client()
    rotated = firebase_credential | {"private_key_id": "rotated-invented-key"}
    with Stubber(ssm) as stub:
        add_firebase_parameter(stub, json.dumps(firebase_credential))
        first = load_firebase_sender(ssm, "/invented/firebase")
        add_firebase_parameter(stub, json.dumps(rotated))
        second = load_firebase_sender(ssm, "/invented/firebase")
        add_firebase_parameter(stub, json.dumps(rotated))
        third = load_firebase_sender(ssm, "/invented/firebase")
        assert first is not None and second is not None and third is not None
        assert first._app is not second._app
        assert deleted == [first._app]
        assert second._app is third._app is firebase_admin.get_app("records-mobile-notifications")
        stub.assert_no_pending_responses()


def test_invalid_rotated_firebase_credential_does_not_return_stale_sender(firebase_credential):
    ssm = ssm_client()
    invalid = firebase_credential | {"private_key": "invented-invalid-key"}
    with Stubber(ssm) as stub:
        add_firebase_parameter(stub, json.dumps(firebase_credential))
        first = load_firebase_sender(ssm, "/invented/firebase")
        add_firebase_parameter(stub, json.dumps(invalid))
        with pytest.raises(RuntimeError, match=r"^mobile_push_configuration_invalid$"):
            load_firebase_sender(ssm, "/invented/firebase")
        # A bad rotation is rejected before deleting the previous valid SDK app.
        add_firebase_parameter(stub, json.dumps(firebase_credential))
        restored = load_firebase_sender(ssm, "/invented/firebase")
        assert first is not None and restored is not None and first._app is restored._app
        stub.assert_no_pending_responses()


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
