"""Image uploads share the native credential and membership boundary."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from shittim_chest.application.models import FollowUpParent
from shittim_chest.domain import AttemptId, DebateId
from tests.test_debate_attachments import ID, REQUEST, image
from tests.test_mobile_debate import (
    CHANNEL,
    GUILD,
    KEY,
    NOW,
    OPERATION_ID,
    REQUEST_ID,
    TOKEN,
    USER,
    prepared_control,
)
from tests.test_mobile_debate import event as debate_event
from tests.test_mobile_debate import setup as setup

from shittim_records.archive import derive_record_key
from shittim_records.contracts import DebateStartRequest
from shittim_records.debate_attachments import public_image
from shittim_records.mobile_debate import DebateRequestFailure
from shittim_records.mobile_debate_http import MobileDebateHttpController

PREFIX = "/api/v1/debate-attachments/uploads"


class Uploads:
    def __init__(self):
        self.calls = []

    def reserve(self, owner, payload, now):
        self.calls.append((owner, payload))
        return {
            "schemaVersion": 1,
            "attachmentId": ID,
            "method": "POST",
            "uploadUrl": "https://upload.example.invalid",
            "expiresAt": datetime.now(UTC).isoformat(),
            "fields": {"key": "debate-uploads/opaque.bin"},
        }

    def status(self, owner, attachment_id, now):
        self.calls.append((owner, attachment_id))
        return {
            "schemaVersion": 1,
            "attachmentId": ID,
            "requestId": REQUEST,
            "status": "ready",
            "image": public_image(image()),
        }

    def complete(self, owner, attachment_id, now):
        return self.status(owner, attachment_id, now)


def event(*, method="POST", suffix="", body=None):
    return {
        "routeKey": f"{method} {PREFIX}{suffix}",
        "requestContext": {"requestId": "fixture-http"},
        "rawQueryString": "",
        "headers": {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        "pathParameters": {"attachmentId": ID},
        "body": json.dumps(
            {
                "requestId": REQUEST,
                "attachmentId": ID,
                "contentType": "image/png",
                "sizeBytes": image().size_bytes,
                "sha256": image().sha256,
            }
        )
        if body is None
        else body,
    }


@pytest.mark.parametrize(
    "change,status",
    [
        ({"headers": {}}, 401),
        ({"cookies": ["session=fixture"]}, 400),
        (
            {
                "headers": {
                    "authorization": f"Bearer {TOKEN}",
                    "origin": "https://other.example.invalid",
                }
            },
            403,
        ),
        ({"body": '{"requestId":"a","requestId":"b"}'}, 400),
    ],
)
def test_uploads_reject_anonymous_mixed_origin_and_duplicate_json(setup, change, status):
    service, _, _, _ = setup
    uploads = Uploads()
    controller = MobileDebateHttpController(
        service, allowed_origin="https://records.example.invalid", attachments=cast(Any, uploads)
    )
    request = event()
    request.update(change)
    assert controller.handle(request)["statusCode"] == status
    assert not uploads.calls


def test_membership_is_checked_before_a_ticket_and_revocation_is_rechecked(setup):
    service, sessions, _, permissions = setup
    uploads = Uploads()
    controller = MobileDebateHttpController(
        service, allowed_origin="https://records.example.invalid", attachments=cast(Any, uploads)
    )
    permissions.fail = "GUILD_MEMBERSHIP_REQUIRED"
    assert controller.handle(event())["statusCode"] == 403
    assert sessions.session is None and not uploads.calls


def test_complete_and_status_return_only_public_metadata(setup):
    service, _, _, permissions = setup
    uploads = Uploads()
    controller = MobileDebateHttpController(
        service, allowed_origin="https://records.example.invalid", attachments=cast(Any, uploads)
    )
    for request in (
        event(method="GET", suffix="/{attachmentId}", body=""),
        event(suffix="/{attachmentId}/complete", body=""),
    ):
        response = controller.handle(request)
        assert response["statusCode"] == 200
        payload = json.loads(response["body"])
        assert payload["image"]["sha256"] == image().sha256
        assert "asset" not in response["body"]
    assert len(permissions.checked) == 2


def test_follow_up_uploads_authorize_the_verified_parent_channel(setup, monkeypatch):
    service, sessions, repository, permissions = setup
    parent = FollowUpParent(DebateId.new(), AttemptId.new())
    parent_id = derive_record_key(KEY, str(parent.debate_id))
    other_channel = str(10**17 + 7)
    service._runtime = replace(
        service._runtime, allowed_channel_ids=frozenset({CHANNEL, other_channel})
    )
    service._follow_ups_enabled = True
    monkeypatch.setattr(
        repository,
        "follow_up_parent",
        lambda record_id: (parent, GUILD, other_channel) if record_id == parent_id else None,
    )
    checked = []

    async def check(*, requester_id, channel_id=CHANNEL):
        checked.append((requester_id, channel_id))
        if channel_id != other_channel:
            raise DebateRequestFailure("CHANNEL_PERMISSION_REQUIRED", 403)

    monkeypatch.setattr(permissions, "check", check)
    uploads = Uploads()
    controller = MobileDebateHttpController(
        service, allowed_origin="https://records.example.invalid", attachments=cast(Any, uploads)
    )
    assert controller.handle(event())["statusCode"] == 403
    parent_query = f"parentRecordId={parent_id}"
    for query, status in (
        ("parentRecordId=invalid", 400),
        ("parentRecordId=", 400),
        (f"{parent_query}&{parent_query}", 400),
        (f"{parent_query}&unknown=1", 400),
        (f"parentRecordId={'p' * 43}", 404),
    ):
        assert controller.handle({**event(), "rawQueryString": query})["statusCode"] == status
    assert not uploads.calls

    for request in (
        event(),
        event(method="GET", suffix="/{attachmentId}", body=""),
        event(suffix="/{attachmentId}/complete", body=""),
    ):
        assert controller.handle({**request, "rawQueryString": parent_query})["statusCode"] == 200
    assert checked == [(USER, CHANNEL)] + [(USER, other_channel)] * 3
    assert [owner for owner, _ in uploads.calls] == [sessions.session.requester_key] * 3


def test_changed_attachment_set_conflicts_before_any_new_upload_check(setup):
    service, _, _, _ = setup

    class Ready:
        def ready_images(self, owner, request_id, ids, now):
            return (image(),)

    service._attachments = Ready()
    accepted, created = asyncio.run(
        service.start(
            token=TOKEN,
            payload=DebateStartRequest(request_id=REQUEST, question="Fixture", attachment_ids=[ID]),
        )
    )
    assert created and accepted.attachment_ids == [ID]
    controller = MobileDebateHttpController(service, allowed_origin="https://records.example")
    response = controller.handle(debate_event(method="GET", body=""))
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["items"][0]["attachmentIds"] == [ID]
    replay, created = asyncio.run(
        service.start(
            token=TOKEN,
            payload=DebateStartRequest(request_id=REQUEST, question="Fixture", attachment_ids=[ID]),
        )
    )
    assert not created and replay == accepted
    with pytest.raises(DebateRequestFailure, match="REQUEST_ID_CONFLICT"):
        asyncio.run(
            service.start(
                token=TOKEN,
                payload=DebateStartRequest(
                    request_id=REQUEST, question="Fixture", attachment_ids=[]
                ),
            )
        )


def test_follow_up_accepts_ordered_attachments_and_replays_without_rechecking(setup, monkeypatch):
    service, sessions, repository, _ = setup
    parent = FollowUpParent(DebateId.new(), AttemptId.new())
    parent_id = derive_record_key(KEY, str(parent.debate_id))
    attachment_ids = ["00000000-0000-4000-8000-00000000000c", ID]
    images = tuple(image(attachment_id) for attachment_id in attachment_ids)
    calls = []

    class Ready:
        def ready_images(self, owner, request_id, ids, now):
            calls.append((owner, request_id, ids, now))
            return images

    service._follow_ups_enabled = True
    service._attachments = Ready()
    monkeypatch.setattr(repository, "follow_up_parent", lambda _: (parent, GUILD, CHANNEL))
    controller = MobileDebateHttpController(service, allowed_origin="https://records.example")
    payload = {
        "requestId": REQUEST,
        "question": "Fixture follow-up",
        "parentRecordId": parent_id,
        "attachmentIds": attachment_ids,
    }
    request = debate_event(body=json.dumps(payload))
    accepted = controller.handle(request)
    assert accepted["statusCode"] == 202
    response = json.loads(accepted["body"])
    assert response["parentRecordId"] == parent_id
    assert response["attachmentIds"] == attachment_ids
    ingress = next(iter(repository.requests.values()))
    assert ingress.follow_up_parent == parent and ingress.attachments == images
    assert controller.handle(request) == accepted

    payload["attachmentIds"] = attachment_ids[::-1]
    conflict = controller.handle(debate_event(body=json.dumps(payload)))
    assert conflict["statusCode"] == 409
    assert json.loads(conflict["body"])["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert calls == [(sessions.session.requester_key, REQUEST, tuple(attachment_ids), NOW)]
    assert len(repository.saved) == 1

    for suffix in ("", "/{requestId}"):
        lookup = debate_event(
            method="GET", body="", suffix=suffix, pathParameters={"requestId": REQUEST}
        )
        for contract, expected_ids in ((None, []), ("debate-follow-up-images-v1", attachment_ids)):
            if contract is not None:
                lookup["headers"]["X-Records-Contract"] = contract
            response = controller.handle(lookup)
            assert response["statusCode"] == 200
            body = json.loads(response["body"])
            item = body if suffix else body["items"][0]
            assert item["parentRecordId"] == parent_id
            assert item["attachmentIds"] == expected_ids


def test_follow_up_control_receipts_keep_legacy_attachment_shape(setup):
    service, _, repository, _ = setup
    receipt = prepared_control(setup)
    key, request = next(iter(repository.requests.items()))
    parent = FollowUpParent(DebateId.new(), AttemptId.new())
    repository.requests[key] = replace(request, follow_up_parent=parent, attachments=(image(),))
    controller = MobileDebateHttpController(service, allowed_origin="https://records.example")
    cancel_request = debate_event(
        suffix="/{requestId}/cancel",
        body=json.dumps({"operationId": OPERATION_ID, "attemptToken": receipt.attempt_token}),
    )
    operation_request = debate_event(
        method="GET",
        suffix="/{requestId}/operations/{operationId}",
        body="",
        pathParameters={"requestId": REQUEST_ID, "operationId": OPERATION_ID},
    )
    for event_request, status in ((cancel_request, 202), (operation_request, 200)):
        legacy = controller.handle(event_request)
        assert legacy["statusCode"] == status
        legacy_request = json.loads(legacy["body"])["request"]
        assert legacy_request["parentRecordId"] == derive_record_key(KEY, str(parent.debate_id))
        assert legacy_request["attachmentIds"] == []

        event_request["headers"]["X-Records-Contract"] = "debate-follow-up-images-v1"
        full = controller.handle(event_request)
        assert full["statusCode"] == status
        assert json.loads(full["body"])["request"]["attachmentIds"] == [ID]
