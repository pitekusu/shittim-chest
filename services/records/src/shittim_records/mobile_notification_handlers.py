"""Independent push worker and minute outbox recovery; never logs request contents."""

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config

from shittim_records.mobile_auth_adapters import DynamoMobileAuthStore
from shittim_records.mobile_notification_adapters import (
    DynamoMobileNotificationStore,
    SqsMobileNotificationQueue,
    load_firebase_sender,
)
from shittim_records.mobile_notifications import MobileNotificationDispatchService

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
SDK_CONFIG = Config(
    retries={"total_max_attempts": 2, "mode": "standard"}, connect_timeout=2, read_timeout=5
)
_STORE: DynamoMobileNotificationStore | None = None
_SESSIONS: DynamoMobileAuthStore | None = None
_QUEUE: SqsMobileNotificationQueue | None = None


def _dependencies() -> tuple[
    DynamoMobileNotificationStore, DynamoMobileAuthStore, SqsMobileNotificationQueue
]:
    global _STORE, _SESSIONS, _QUEUE
    if _STORE is None or _SESSIONS is None or _QUEUE is None:
        _STORE = DynamoMobileNotificationStore(
            boto3.resource("dynamodb", config=SDK_CONFIG),
            statistics=os.environ["STATISTICS_TABLE_NAME"],
            sessions=os.environ["SESSION_TABLE_NAME"],
            archive=os.environ["ARCHIVE_TABLE_NAME"],
        )
        _SESSIONS = DynamoMobileAuthStore(
            boto3.client("dynamodb", config=SDK_CONFIG), os.environ["SESSION_TABLE_NAME"]
        )
        _QUEUE = SqsMobileNotificationQueue(
            boto3.client("sqs", config=SDK_CONFIG), os.environ["MOBILE_PUSH_QUEUE_URL"]
        )
    return _STORE, _SESSIONS, _QUEUE


def handler(event: Mapping[str, Any], context: object) -> dict[str, Any]:
    """SQS batch size one; recovery only queues, so all sends use the same FIFO group."""
    if "Records" not in event:
        if event.get("source") != "aws.events":
            raise ValueError("mobile_push_event_invalid")
        store, _sessions, queue = _dependencies()
        now_epoch = int(datetime.now(UTC).timestamp())
        queued = 0
        try:
            cleaned = store.cleanup(now_epoch=now_epoch)
            for record_id in store.pending_events(now_epoch=now_epoch):
                queue.enqueue(_record_id(record_id), generation=str(now_epoch // 60))
                queued += 1
                if queued >= 100 or _remaining(context) < 10:
                    break
        except Exception:
            LOGGER.error("MOBILE_PUSH_SWEEP_FAILED")
            raise RuntimeError("mobile_push_sweep_failed") from None
        LOGGER.info("MOBILE_PUSH_SWEEP queued=%d cleaned=%d", queued, cleaned)
        return {"queued": queued, "cleaned": cleaned}
    records = event["Records"]
    if not isinstance(records, list) or len(records) != 1:
        raise ValueError("mobile_push_batch_invalid")
    record = records[0]
    identifier = record.get("messageId", "")
    try:
        body = record.get("body")
        if not isinstance(body, str) or len(body.encode("utf-8")) > 256:
            raise ValueError("mobile_push_job_invalid")
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {"recordId"}:
            raise ValueError("mobile_push_job_invalid")
        record_id = _record_id(payload["recordId"])
        store, sessions, _queue = _dependencies()
        sender = load_firebase_sender(
            boto3.client("ssm", config=SDK_CONFIG),
            os.environ["FIREBASE_SERVICE_ACCOUNT_PARAMETER_NAME"],
        )
        summary = MobileNotificationDispatchService(
            store=store, sessions=sessions, sender=sender
        ).dispatch(record_id, remaining_seconds=lambda: _remaining(context))
        LOGGER.info(
            "MOBILE_PUSH_DISPATCH sent=%d failed=%d pending=%s configured=%s",
            summary.sent,
            summary.failed,
            summary.pending,
            sender is not None,
        )
    except Exception:
        LOGGER.error("MOBILE_PUSH_DISPATCH_FAILED")
        return {"batchItemFailures": [{"itemIdentifier": identifier}]}
    return {"batchItemFailures": []}


def _record_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        raise ValueError("mobile_push_record_invalid")
    return value


def _remaining(context: object) -> float:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    return 120 if not callable(remaining) else max(0, remaining() / 1000)
