"""Mobile ownership is additive; legacy ingress serialization stays unchanged."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from shittim_chest.adapters.dynamodb.serializer import (
    PersistenceFormatError,
    deserialize_ingress_request,
    serialize_ingress_request,
)
from shittim_chest.application.scale_to_zero import (
    IngressRequest,
    IngressSource,
    mobile_ingress_id,
)

NOW = datetime(2026, 10, 4, tzinfo=UTC)
OWNER = "a" * 43
REQUEST_ID = "00000000-0000-4000-8000-000000000001"


def mobile_request() -> IngressRequest:
    return IngressRequest.mobile_debate(
        request_id=REQUEST_ID,
        owner_key=OWNER,
        application_id="application-id",
        question="A fictional question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        created_at=NOW,
    )


def test_owner_scoped_operation_and_request_round_trip() -> None:
    request = mobile_request()
    assert request.interaction_id == request.operation_id == mobile_ingress_id(OWNER, REQUEST_ID)
    assert request.interaction_id != mobile_ingress_id("b" * 43, REQUEST_ID)
    assert not request.interaction_id.isdecimal()
    assert OWNER not in request.interaction_id and REQUEST_ID not in request.interaction_id
    item = serialize_ingress_request(request)
    assert item["source"] == "mobile"
    assert deserialize_ingress_request(item) == request


def test_legacy_record_omits_mobile_fields_and_reserializes_identically() -> None:
    request = IngressRequest.new_debate(
        interaction_id="legacy-interaction",
        operation_id="legacy-interaction",
        application_id="application-id",
        question="A fictional question",
        requester_id="requester-id",
        requester_username="requester",
        requester_display_name="Requester",
        guild_id="guild-id",
        channel_id="channel-id",
        command_name="shittim",
        created_at=NOW,
    )
    item = serialize_ingress_request(request)
    assert not {"source", "owner_key", "mobile_request_id"} & item.keys()
    assert serialize_ingress_request(deserialize_ingress_request(item)) == item


@pytest.mark.parametrize("field,value", [("owner_key", "b" * 43), ("source", "discord")])
def test_tampered_mobile_ownership_is_rejected(field: str, value: str) -> None:
    with pytest.raises(PersistenceFormatError):
        deserialize_ingress_request({**serialize_ingress_request(mobile_request()), field: value})


@pytest.mark.parametrize(
    "request_id",
    ["00000000-0000-1000-8000-000000000001", "invalid", REQUEST_ID.upper().replace("0001", "ABCD")],
)
def test_only_canonical_uuidv4_is_accepted(request_id: str) -> None:
    with pytest.raises(ValueError):
        mobile_ingress_id(OWNER, request_id)


def test_discord_cannot_acquire_mobile_ownership_without_source() -> None:
    with pytest.raises(ValueError):
        replace(mobile_request(), source=IngressSource.DISCORD)
