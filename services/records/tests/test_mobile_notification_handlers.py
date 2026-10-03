"""Content-free Lambda boundaries, sweep recovery and malformed queue messages."""

import json
from types import SimpleNamespace

import pytest

from shittim_records import mobile_notification_handlers as handlers
from shittim_records.mobile_notifications import NotificationSummary


def test_sweep_recovers_only_pending_publication_jobs_and_runs_bounded_cleanup(monkeypatch):
    queued = []
    store = SimpleNamespace(
        cleanup=lambda **kwargs: 2,
        pending_events=lambda **kwargs: ["r" * 43, "s" * 43],
    )
    queue = SimpleNamespace(enqueue=lambda record_id, **kwargs: queued.append(record_id))
    monkeypatch.setattr(handlers, "_dependencies", lambda: (store, None, queue))
    result = handlers.handler({"source": "aws.events"}, object())
    assert result == {"queued": 2, "cleaned": 2}
    assert queued == ["r" * 43, "s" * 43]


def test_missing_firebase_is_reported_without_infinite_batch_failure(monkeypatch, caplog):
    calls = []
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT_PARAMETER_NAME", "/invented/firebase")
    monkeypatch.setattr(handlers, "_dependencies", lambda: (object(), object(), object()))
    monkeypatch.setattr(handlers.boto3, "client", lambda *args, **kwargs: object())
    monkeypatch.setattr(handlers, "load_firebase_sender", lambda *args: None)
    monkeypatch.setattr(
        handlers,
        "MobileNotificationDispatchService",
        lambda **kwargs: SimpleNamespace(
            dispatch=lambda record_id, **args: calls.append(record_id) or NotificationSummary()
        ),
    )
    event = {"Records": [{"messageId": "opaque-id", "body": json.dumps({"recordId": "r" * 43})}]}
    assert handlers.handler(event, object()) == {"batchItemFailures": []}
    assert calls == ["r" * 43] and "configured=False" in caplog.text
    assert "r" * 43 not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "{}",
        '{"recordId":"private"}',
        '{"recordId":true}',
        '{"recordId":"' + "r" * 43 + '","token":"private-token"}',
        "a" * 257,
    ],
)
def test_bad_jobs_never_reach_aws_and_error_logs_have_no_input(monkeypatch, caplog, body):
    def unexpected():
        raise AssertionError("invalid job reached AWS")

    monkeypatch.setattr(handlers, "_dependencies", unexpected)
    event = {"Records": [{"messageId": "opaque-id", "body": body}]}
    assert handlers.handler(event, object()) == {
        "batchItemFailures": [{"itemIdentifier": "opaque-id"}]
    }
    assert "MOBILE_PUSH_DISPATCH_FAILED" in caplog.text
    assert "private" not in caplog.text and body not in caplog.text
