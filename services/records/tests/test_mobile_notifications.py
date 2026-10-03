"""Notification behavior uses invented records/devices and never calls FCM."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.test_auth import FakeAvatars, FakeDiscord, configuration
from tests.test_http_api import event
from tests.test_mobile_exchange import KEY
from tests.test_mobile_login import MemoryStore
from tests.test_mobile_session import SessionStore

from shittim_records.auth import AuthFailure, _digest
from shittim_records.mobile_auth import (
    MOBILE_SESSION_TTL_SECONDS,
    MobileNotificationDeviceRequest,
    MobileSessionRecord,
    parse_mobile_request,
)
from shittim_records.mobile_http import MobileAuthHttpController
from shittim_records.mobile_login import MobileLoginService
from shittim_records.mobile_notifications import (
    MAX_DELIVERY_ATTEMPTS,
    MobileNotificationDispatchService,
    MobileNotificationRegistrationService,
    NotificationDevice,
    NotificationSummary,
    PushDeliveryFailed,
    device_token_hash,
    pending_mobile_notification,
    safe_requester_name,
    timestamp,
)
from shittim_records.mobile_session import MobileSessionService

RECORD_ID = "R" * 43
NOW = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
TOKEN = "T" * 43
BINDING = "B" * 43
REQUEST = {"token": "invented-fcm:token_1", "bindingId": BINDING}
HASH = _digest(KEY, "mobile-session", TOKEN)


class PushStore:
    def __init__(self) -> None:
        self.registry: dict[str, NotificationDevice] = {}
        self.events: dict[str, dict[str, Any]] = {
            RECORD_ID: pending_mobile_notification(record_id=RECORD_ID, created_at=NOW)
        }
        self.receipts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.visible = True

    def register(self, device: NotificationDevice, *, now_epoch: int) -> None:
        self.registry[device.token_hash] = device

    def unregister(self, *, token_hash, binding_id, session_hash):
        current = self.registry.get(token_hash)
        if current and (current.binding_id, current.session_hash) == (binding_id, session_hash):
            self.registry.pop(token_hash)

    def get_event(self, record_id):
        value = self.events.get(record_id)
        return None if value is None else dict(value)

    def claim_event(self, record_id, *, now_epoch):
        value = self.events[record_id]
        if value["state"] != "pending":
            return False
        value["state"] = "processing"
        value["runs"] += 1
        return True

    def finish_event(self, record_id, *, state):
        self.events[record_id]["state"] = state

    def devices(self, *, after=None):
        return [
            device for key, device in sorted(self.registry.items()) if after is None or key > after
        ]

    def checkpoint(self, record_id, *, cursor, retry_needed):
        self.events[record_id].update(cursor=cursor, retry_needed=retry_needed)

    def current_device(self, token_hash):
        return self.registry.get(token_hash)

    def claim_delivery(self, record_id, device, *, now_epoch):
        key = (record_id, device.token_hash, device.binding_id)
        old = self.receipts.get(key)
        if old:
            if old["state"] != "retry" or old["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                return "done"
            if old["retry_at"] > now_epoch:
                return "wait"
        self.receipts[key] = {
            "state": "sending",
            "attempts": 1 if old is None else old["attempts"] + 1,
        }
        return "send"

    def finish_delivery(self, record_id, device, *, state, now_epoch, retry_after=60):
        self.receipts[(record_id, device.token_hash, device.binding_id)].update(
            state=state, retry_at=now_epoch + retry_after
        )

    def record_requester_name(self, record_id):
        return "架空の依頼者" if self.visible else None


class Sender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures: dict[str, str] = {}

    def send(self, *, token, data):
        self.calls.append({"token": token, "data": data})
        failure = self.failures.get(token)
        if failure == "unknown":
            raise TimeoutError("do not log this private request")
        if failure:
            raise PushDeliveryFailed(failure)


@pytest.fixture
def setup():
    sessions = SessionStore()
    sessions.sessions[HASH] = MobileSessionRecord(
        requester_key="r" * 43,
        display_name="Invented user",
        avatar_asset_key=None,
        guild_verified_at=NOW,
        created_at=int((NOW - timedelta(hours=1)).timestamp()),
        expires_at=int((NOW - timedelta(hours=1)).timestamp()) + MOBILE_SESSION_TTL_SECONDS,
    )
    store = PushStore()
    clock = [NOW - timedelta(minutes=1)]
    registration = MobileNotificationRegistrationService(
        sessions=sessions, store=store, session_key=KEY, clock=lambda: clock[0]
    )
    registration.update(
        raw_token=TOKEN,
        request=parse_mobile_request(MobileNotificationDeviceRequest, REQUEST),
        delete=False,
    )
    clock[0] = NOW
    sender = Sender()
    dispatcher = MobileNotificationDispatchService(
        store=store, sessions=sessions, sender=sender, clock=lambda: clock[0]
    )
    return sessions, store, clock, registration, sender, dispatcher


def test_registration_is_absolute_session_bound_and_response_has_no_identifiers(setup):
    sessions, store, _clock, registration, _sender, _dispatch = setup
    request = parse_mobile_request(MobileNotificationDeviceRequest, REQUEST)
    response = registration.update(raw_token=TOKEN, request=request, delete=False)
    assert response is not None
    assert set(response.model_dump(by_alias=True)) == {"schemaVersion", "expiresAt"}
    assert response.expires_at.timestamp() == sessions.sessions[HASH].expires_at
    device = next(iter(store.registry.values()))
    assert device.session_hash == HASH and device.binding_id == BINDING
    for secret in (TOKEN, REQUEST["token"], BINDING):
        assert secret not in response.model_dump_json() and secret not in repr(request)


def test_logout_or_absolute_expiry_prevents_send_even_when_device_row_survives(setup):
    sessions, store, clock, registration, sender, dispatch = setup
    sessions.delete_session(session_hash=HASH)
    assert dispatch.dispatch(RECORD_ID) == NotificationSummary()
    assert not sender.calls and not store.registry
    with pytest.raises(AuthFailure, match="session_required"):
        registration.update(
            raw_token=TOKEN,
            request=parse_mobile_request(MobileNotificationDeviceRequest, REQUEST),
            delete=False,
        )
    clock[0] += timedelta(days=91)
    assert dispatch.dispatch(RECORD_ID) == NotificationSummary()


def test_each_visible_new_record_is_content_free_and_delivered_once(setup):
    _sessions, store, _clock, _registration, sender, dispatch = setup
    assert dispatch.dispatch(RECORD_ID).sent == 1
    assert dispatch.dispatch(RECORD_ID).sent == 0
    assert len(sender.calls) == 1
    assert sender.calls[0]["data"] == {
        "type": "record_published",
        "schemaVersion": "1",
        "recordId": RECORD_ID,
        "bindingId": BINDING,
        "publishedAt": timestamp(NOW),
        "requesterName": "架空の依頼者",
    }
    # Replayed jobs cannot re-create a device receipt, even if the event is pending again.
    store.events[RECORD_ID]["state"] = "pending"
    assert dispatch.dispatch(RECORD_ID).sent == 0
    assert len(sender.calls) == 1


def test_registration_after_publication_and_stale_binding_receive_nothing(setup):
    _sessions, store, _clock, _registration, sender, dispatch = setup
    device = next(iter(store.registry.values()))
    store.registry[device.token_hash] = device.model_copy(
        update={"registered_at": timestamp(NOW + timedelta(microseconds=1))}
    )
    dispatch.dispatch(RECORD_ID)
    assert not sender.calls
    store.events[RECORD_ID]["state"] = "pending"
    store.registry[device.token_hash] = device
    store.current_device = lambda _key: device.model_copy(update={"binding_id": "N" * 43})
    dispatch.dispatch(RECORD_ID)
    assert not sender.calls


def test_old_binding_delete_cannot_remove_rebound_token(setup):
    _sessions, store, _clock, registration, _sender, _dispatch = setup
    device = next(iter(store.registry.values()))
    replacement = device.model_copy(update={"binding_id": "N" * 43})
    store.registry[device.token_hash] = replacement
    registration.update(
        raw_token=TOKEN,
        request=parse_mobile_request(MobileNotificationDeviceRequest, REQUEST),
        delete=True,
    )
    assert store.registry[device.token_hash] == replacement


@pytest.mark.parametrize("reason", ["invalid_token", "unknown", "failed"])
def test_per_device_failure_isolated_and_ambiguous_outcome_never_retried(setup, reason):
    sessions, store, _clock, _registration, sender, dispatch = setup
    device = next(iter(store.registry.values()))
    second = device.model_copy(
        update={"token": "second-fake", "token_hash": device_token_hash("second-fake")}
    )
    store.registry[second.token_hash] = second
    sender.failures[device.token] = reason
    summary = dispatch.dispatch(RECORD_ID)
    assert summary.sent == 1 and summary.failed == 1
    assert len(sender.calls) == 2 and len(sessions.sessions) == 1
    if reason == "invalid_token":
        assert device.token_hash not in store.registry
    store.events[RECORD_ID]["state"] = "pending"
    dispatch.dispatch(RECORD_ID)
    assert len(sender.calls) == 2


def test_known_transient_failure_waits_and_is_bounded(setup):
    _sessions, store, clock, _registration, sender, dispatch = setup
    device = next(iter(store.registry.values()))
    sender.failures[device.token] = "retryable"
    assert dispatch.dispatch(RECORD_ID).pending
    assert dispatch.dispatch(RECORD_ID).pending
    assert len(sender.calls) == 1
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        clock[0] += timedelta(minutes=1)
        dispatch.dispatch(RECORD_ID)
    assert len(sender.calls) == MAX_DELIVERY_ATTEMPTS
    assert store.events[RECORD_ID]["state"] == "complete"


def test_crash_after_sending_receipt_and_deadline_do_not_resend(setup):
    _sessions, store, _clock, _registration, sender, dispatch = setup
    assert dispatch.dispatch(RECORD_ID, remaining_seconds=lambda: 10).pending
    assert not sender.calls
    device = next(iter(store.registry.values()))
    store.claim_delivery(RECORD_ID, device, now_epoch=int(NOW.timestamp()))
    dispatch.dispatch(RECORD_ID)
    assert not sender.calls


def test_deadline_resumes_after_last_device_without_repeating_a_delivery(setup):
    _sessions, store, _clock, _registration, sender, dispatch = setup
    device = next(iter(store.registry.values()))
    second = device.model_copy(
        update={"token": "second-fake", "token_hash": device_token_hash("second-fake")}
    )
    store.registry[second.token_hash] = second
    budget = iter((120, 10))
    assert dispatch.dispatch(RECORD_ID, remaining_seconds=lambda: next(budget)).pending
    assert len(sender.calls) == 1
    assert store.events[RECORD_ID]["cursor"] == sorted(store.registry)[0]
    assert dispatch.dispatch(RECORD_ID).sent == 1
    assert len({call["token"] for call in sender.calls}) == 2
    assert store.events[RECORD_ID]["state"] == "complete"


def test_logout_after_receipt_acquisition_prevents_provider_send(setup):
    sessions, store, _clock, _registration, sender, dispatch = setup
    claim = store.claim_delivery

    def revoke_then_claim(record_id, device, *, now_epoch):
        result = claim(record_id, device, now_epoch=now_epoch)
        sessions.delete_session(session_hash=HASH)
        return result

    store.claim_delivery = revoke_then_claim
    dispatch.dispatch(RECORD_ID)
    assert not sender.calls
    assert next(iter(store.receipts.values()))["state"] == "cancelled"


@pytest.mark.parametrize("state", ["disabled", "expired", "cancelled"])
def test_unconfigured_expired_or_removed_record_is_not_sent(setup, state):
    sessions, store, clock, _registration, sender, dispatch = setup
    if state == "disabled":
        dispatch = MobileNotificationDispatchService(
            store=store, sessions=sessions, sender=None, clock=lambda: NOW
        )
    elif state == "expired":
        clock[0] += timedelta(days=1)
    else:
        store.visible = False
    dispatch.dispatch(RECORD_ID)
    assert not sender.calls and store.events[RECORD_ID]["state"] == state


def test_http_device_api_strict_bearer_json_and_no_store(setup):
    sessions, store, _clock, registration, _sender, _dispatch = setup
    config = configuration()
    controller = MobileAuthHttpController(
        login=MobileLoginService(
            store=MemoryStore(), discord=FakeDiscord(), oauth=config.oauth, hmac_key=KEY
        ),
        callback=cast(Any, SimpleNamespace()),
        exchange=cast(Any, SimpleNamespace()),
        sessions=MobileSessionService(
            store=sessions,
            avatars=FakeAvatars(),
            session_key=KEY,
            admin_requester_key="r" * 43,
            clock=lambda: NOW,
        ),
        notifications=registration,
        allowed_origin=config.oauth.allowed_origin,
    )
    request = {
        **event(
            "PUT /api/v1/auth/mobile/notifications/device",
            headers={"content-type": "application/json", "authorization": "Bearer " + TOKEN},
        ),
        "body": '{"token":"invented-fcm:token_1","bindingId":"' + BINDING + '"}',
    }
    response = controller.handle(request, now=NOW)
    assert (
        response["statusCode"] == 200
        and response["headers"]["Cache-Control"] == "private, no-store"
    )
    for private in (TOKEN, REQUEST["token"], BINDING):
        assert private not in response["body"]
    for override, expected in (
        ({"cookies": ["session=fake"]}, 400),
        ({"headers": {"content-type": "application/json"}}, 401),
        ({"rawQueryString": "token=fake"}, 400),
        ({"body": '{"token":"a","token":"b","bindingId":"' + BINDING + '"}'}, 400),
        ({"body": '{"token":"x","bindingId":"bad"}'}, 400),
    ):
        assert controller.handle({**request, **override}, now=NOW)["statusCode"] == expected
    deletion = {**request, "routeKey": "DELETE /api/v1/auth/mobile/notifications/device"}
    assert controller.handle(deletion, now=NOW)["statusCode"] == 204
    assert not store.registry


def test_requester_name_is_bounded_single_line_and_keeps_japanese():
    assert safe_requester_name("  架空\n\x00利用者\r\t ") == "架空 利用者"
    assert safe_requester_name("  架空\u2028利用者\u2029 ") == "架空 利用者"
    assert len(safe_requester_name("あ" * 101)) == 100
    assert safe_requester_name("\r\n\x00") == "依頼者"


def test_requester_name_remains_nfc_after_removing_format_controls():
    assert safe_requester_name("A\u202e\u030a") == "Å"


@pytest.mark.parametrize("token", ["", "x" * 2049, "a b", "a\nb", "日本語", "a\x00b"])
def test_token_input_rejected_without_disclosing_it(token):
    with pytest.raises(AuthFailure, match=r"^mobile_request_invalid$"):
        parse_mobile_request(
            MobileNotificationDeviceRequest, {"token": token, "bindingId": BINDING}
        )
