"""Image uploads share the native credential and membership boundary."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from tests.test_debate_attachments import ID, REQUEST, image
from tests.test_mobile_debate import TOKEN
from tests.test_mobile_debate import setup as setup

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
