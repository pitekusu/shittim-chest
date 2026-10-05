"""Mobile ownership is additive; legacy ingress serialization stays unchanged."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from discord.utils import time_snowflake

from shittim_chest.adapters.dynamodb.serializer import (
    PersistenceFormatError,
    deserialize_ingress_request,
    deserialize_ingress_status_publication,
    serialize_ingress_request,
    serialize_ingress_status_publication,
)
from shittim_chest.application.scale_to_zero import (
    IngressRequest,
    IngressSource,
    IngressStatusPublication,
    StatusHistoryCheckpoint,
    mobile_ingress_id,
)
from shittim_chest.application.status_publication import render_public_status

NOW = datetime(2026, 10, 4, tzinfo=UTC)
OWNER = "a" * 43
REQUEST_ID = "11111111-2222-4333-8444-55555555555a"


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
    ["11111111-2222-1333-8444-55555555555a", "invalid", REQUEST_ID.upper()],
)
def test_only_canonical_uuidv4_is_accepted(request_id: str) -> None:
    with pytest.raises(ValueError):
        mobile_ingress_id(OWNER, request_id)


def test_discord_cannot_acquire_mobile_ownership_without_source() -> None:
    with pytest.raises(ValueError):
        replace(mobile_request(), source=IngressSource.DISCORD)


def test_mobile_history_checkpoint_uses_separate_snowflake_bound_and_round_trips() -> None:
    boundary = str(time_snowflake(NOW - timedelta(seconds=1)))
    request = replace(mobile_request(), history_after_snowflake=boundary)
    publication = IngressStatusPublication.prepared(
        request, content=render_public_status(request, request.status_message_state)
    )
    progress = replace(
        publication,
        history_reconciliation_required=True,
        history_checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id=str(int(boundary) + 1),
            history_verified_head_message_id=str(int(boundary) + 2),
        ),
    )
    assert progress.history_after_message_id == boundary
    assert progress.canonical_interaction_id != boundary
    assert (
        deserialize_ingress_status_publication(serialize_ingress_status_publication(progress))
        == progress
    )
    assert deserialize_ingress_request(serialize_ingress_request(request)) == request
    with pytest.raises(ValueError, match="follow the interaction"):
        replace(
            progress,
            history_checkpoint=StatusHistoryCheckpoint(
                history_cursor_message_id=boundary,
                history_verified_head_message_id=str(int(boundary) + 2),
            ),
        )
