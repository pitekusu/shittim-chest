"""Discord REST v10 status delivery contracts with an offline transport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from shittim_chest.adapters.discord.status import DiscordRestStatusGateway
from shittim_chest.application.scale_to_zero import StatusHistoryCheckpoint
from shittim_chest.application.status_publication import (
    StatusDeliveryError,
    StatusDeliveryErrorCode,
    StatusHistoryProgress,
    StatusMessageMissing,
    StatusWriteAmbiguous,
)

TOKEN = "test-moderator-token"  # noqa: S105 - offline fixture value.
CHANNEL_ID = "101"
GUILD_ID = "102"
APPLICATION_ID = "100"
AUTHOR_ID = "200"
NONCE = "abcdefghijklmnopqrstuv"
CONTENT = "状態: STARTING"
OPERATION_MARKER = "sc-0123456789abcdef0123"
STATUS_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 16) | (1 << 38)


def message(
    message_id: str = "500",
    *,
    content: str = CONTENT,
    nonce: str | None = NONCE,
    author_id: str = AUTHOR_ID,
) -> dict[str, object]:
    return {
        "id": message_id,
        "channel_id": CHANNEL_ID,
        "author": {"id": author_id},
        "content": content,
        "nonce": nonce,
    }


@pytest.mark.asyncio
async def test_create_uses_bot_auth_nonce_dedup_and_no_mentions() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=message(), request=request)

    gateway = gateway_with(respond)

    result = await gateway.create_message(
        channel_id=CHANNEL_ID,
        content=CONTENT,
        nonce=NONCE,
    )

    assert result.message_id == "500"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/v10/channels/{CHANNEL_ID}/messages"
    assert request.headers["Authorization"] == f"Bot {TOKEN}"
    assert request.headers["User-Agent"] == (
        "DiscordBot (https://github.com/pitekusu/shittim-chest, 1.0.0)"
    )
    assert json.loads(request.content) == {
        "content": CONTENT,
        "nonce": NONCE,
        "enforce_nonce": True,
        "allowed_mentions": {"parse": []},
    }


@pytest.mark.asyncio
async def test_current_bot_user_is_resolved_once_without_application_id_assumption() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v10/oauth2/applications/@me":
            return httpx.Response(200, json={"id": APPLICATION_ID}, request=request)
        return httpx.Response(
            200,
            json={"id": AUTHOR_ID, "bot": True},
            request=request,
        )

    gateway = gateway_with(respond, default_identity=False)

    assert await gateway.current_bot_user_id() == AUTHOR_ID
    assert await gateway.current_bot_user_id() == AUTHOR_ID
    assert [request.url.path for request in requests] == [
        "/api/v10/oauth2/applications/@me",
        "/api/v10/users/@me",
    ]


@pytest.mark.asyncio
async def test_token_for_another_application_is_rejected_before_message_access() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "999"}, request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, default_identity=False).current_bot_user_id()

    assert caught.value.code is StatusDeliveryErrorCode.CONFLICT
    assert caught.value.retryable is False
    assert [request.url.path for request in requests] == ["/api/v10/oauth2/applications/@me"]


@pytest.mark.asyncio
async def test_missing_read_history_permission_fails_closed_before_an_empty_scan() -> None:
    history_requests = 0

    def history(request: httpx.Request) -> httpx.Response:
        nonlocal history_requests
        history_requests += 1
        return httpx.Response(200, json=[], request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [
                        {
                            "id": GUILD_ID,
                            "type": 0,
                            "allow": "0",
                            "deny": str(1 << 16),
                        }
                    ],
                },
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(history, permission_handler=permissions).find_by_nonce(
            channel_id=CHANNEL_ID,
            author_id=AUTHOR_ID,
            nonce=NONCE,
            operation_marker=OPERATION_MARKER,
            after_message_id="300",
            checkpoint=None,
        )

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert caught.value.retryable is False
    assert history_requests == 0


@pytest.mark.asyncio
async def test_administrator_bypasses_channel_overwrites_for_history_diagnostics() -> None:
    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [
                        {
                            "id": AUTHOR_ID,
                            "type": 1,
                            "allow": "0",
                            "deny": str(STATUS_PERMISSIONS),
                        }
                    ],
                },
                request=request,
            )
        if request.url.path == f"/api/v10/guilds/{GUILD_ID}/roles":
            return httpx.Response(
                200,
                json=[{"id": GUILD_ID, "permissions": str(1 << 3)}],
                request=request,
            )
        return _default_permission_response(request)

    result = await gateway_with(
        lambda request: httpx.Response(200, json=[], request=request),
        permission_handler=permissions,
    ).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_public_thread_uses_parent_overwrites_and_thread_send_permission() -> None:
    parent_id = "103"

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            payload: object = {
                "id": CHANNEL_ID,
                "guild_id": GUILD_ID,
                "type": 11,
                "parent_id": parent_id,
                "thread_metadata": {"archived": False, "locked": False},
            }
            return httpx.Response(200, json=payload, request=request)
        if request.url.path == f"/api/v10/channels/{parent_id}":
            payload = {
                "id": parent_id,
                "guild_id": GUILD_ID,
                "type": 0,
                "parent_id": "104",
                "permission_overwrites": [],
            }
            return httpx.Response(200, json=payload, request=request)
        return _default_permission_response(request)

    result = await gateway_with(
        lambda request: httpx.Response(200, json=[], request=request),
        permission_handler=permissions,
    ).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_public_thread_rejects_send_messages_without_send_messages_in_threads() -> None:
    parent_id = "103"
    history_requests = 0

    def history(request: httpx.Request) -> httpx.Response:
        nonlocal history_requests
        history_requests += 1
        return httpx.Response(200, json=[], request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            payload: object = {
                "id": CHANNEL_ID,
                "guild_id": GUILD_ID,
                "type": 11,
                "parent_id": parent_id,
                "thread_metadata": {"archived": False, "locked": False},
            }
            return httpx.Response(200, json=payload, request=request)
        if request.url.path == f"/api/v10/channels/{parent_id}":
            payload = {
                "id": parent_id,
                "guild_id": GUILD_ID,
                "type": 0,
                "parent_id": None,
                "permission_overwrites": [],
            }
            return httpx.Response(200, json=payload, request=request)
        if request.url.path == f"/api/v10/guilds/{GUILD_ID}/roles":
            permissions_without_thread_send = STATUS_PERMISSIONS & ~(1 << 38)
            return httpx.Response(
                200,
                json=[
                    {
                        "id": GUILD_ID,
                        "permissions": str(permissions_without_thread_send),
                    }
                ],
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(history, permission_handler=permissions).find_by_nonce(
            channel_id=CHANNEL_ID,
            author_id=AUTHOR_ID,
            nonce=NONCE,
            operation_marker=OPERATION_MARKER,
            after_message_id="300",
            checkpoint=None,
        )

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert history_requests == 0


@pytest.mark.asyncio
async def test_create_in_an_archived_public_thread_relies_on_discord_auto_unarchive() -> None:
    parent_id = "103"
    request_methods: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        request_methods.append((request.method, request.url.path))
        return httpx.Response(200, json=message(), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        request_methods.append((request.method, request.url.path))
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 11,
                    "parent_id": parent_id,
                    "thread_metadata": {"archived": True, "locked": False},
                },
                request=request,
            )
        if request.url.path == f"/api/v10/channels/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [],
                },
                request=request,
            )
        return _default_permission_response(request)

    result = await gateway_with(respond, permission_handler=permissions).create_message(
        channel_id=CHANNEL_ID,
        content=CONTENT,
        nonce=NONCE,
    )

    assert result.message_id == "500"
    assert ("PATCH", f"/api/v10/channels/{CHANNEL_ID}") not in request_methods
    assert ("POST", f"/api/v10/channels/{CHANNEL_ID}/messages") in request_methods


@pytest.mark.asyncio
async def test_edit_unarchives_an_unlocked_public_thread_then_rechecks_permissions() -> None:
    parent_id = "103"
    archived = True
    requests: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=message(content="updated"), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        nonlocal archived
        requests.append((request.method, request.url.path))
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            if request.method == "PATCH":
                assert json.loads(request.content) == {"archived": False}
                archived = False
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 11,
                    "parent_id": parent_id,
                    "thread_metadata": {"archived": archived, "locked": False},
                },
                request=request,
            )
        if request.url.path == f"/api/v10/channels/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [],
                },
                request=request,
            )
        return _default_permission_response(request)

    result = await gateway_with(respond, permission_handler=permissions).edit_message(
        channel_id=CHANNEL_ID,
        message_id="500",
        content="updated",
    )

    assert result.content == "updated"
    assert requests.count(("PATCH", f"/api/v10/channels/{CHANNEL_ID}")) == 1
    assert requests.count(("GET", f"/api/v10/channels/{CHANNEL_ID}")) == 2
    assert requests[-1] == ("PATCH", f"/api/v10/channels/{CHANNEL_ID}/messages/500")


@pytest.mark.asyncio
async def test_edit_stops_if_thread_locks_during_post_unarchive_permission_recheck() -> None:
    parent_id = "103"
    channel_gets = 0
    message_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal message_requests
        message_requests += 1
        return httpx.Response(200, json=message(content="updated"), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        nonlocal channel_gets
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            if request.method == "GET":
                channel_gets += 1
            archived = request.method != "PATCH" and channel_gets == 1
            locked = request.method == "GET" and channel_gets == 2
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 11,
                    "parent_id": parent_id,
                    "thread_metadata": {"archived": archived, "locked": locked},
                },
                request=request,
            )
        if request.url.path == f"/api/v10/channels/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [],
                },
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, permission_handler=permissions).edit_message(
            channel_id=CHANNEL_ID,
            message_id="500",
            content="updated",
        )

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert channel_gets == 2
    assert message_requests == 0


@pytest.mark.asyncio
async def test_ambiguous_unarchive_never_attempts_the_message_edit() -> None:
    parent_id = "103"
    message_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal message_requests
        message_requests += 1
        return httpx.Response(200, json=message(content="updated"), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            if request.method == "PATCH":
                return httpx.Response(503, text="provider-secret", request=request)
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 11,
                    "parent_id": parent_id,
                    "thread_metadata": {"archived": True, "locked": False},
                },
                request=request,
            )
        if request.url.path == f"/api/v10/channels/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "guild_id": GUILD_ID,
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [],
                },
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusWriteAmbiguous) as caught:
        await gateway_with(respond, permission_handler=permissions).edit_message(
            channel_id=CHANNEL_ID,
            message_id="500",
            content="updated",
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert "provider-secret" not in str(caught.value)
    assert message_requests == 0


@pytest.mark.asyncio
async def test_locked_public_thread_fails_before_parent_or_message_requests() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        raise AssertionError("locked thread must fail before message access")

    def permissions(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == f"/api/v10/channels/{CHANNEL_ID}"
        return httpx.Response(
            200,
            json={
                "id": CHANNEL_ID,
                "guild_id": GUILD_ID,
                "type": 11,
                "parent_id": "103",
                "thread_metadata": {"archived": True, "locked": True},
            },
            request=request,
        )

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, permission_handler=permissions).edit_message(
            channel_id=CHANNEL_ID,
            message_id="500",
            content="updated",
        )

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert paths == [f"/api/v10/channels/{CHANNEL_ID}"]


@pytest.mark.asyncio
async def test_guild_member_identity_mismatch_fails_before_message_create() -> None:
    message_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal message_requests
        message_requests += 1
        return httpx.Response(200, json=message(), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/guilds/{GUILD_ID}/members/{AUTHOR_ID}":
            return httpx.Response(
                200,
                json={"user": {"id": "999"}, "roles": []},
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, permission_handler=permissions).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.CONFLICT
    assert message_requests == 0


@pytest.mark.asyncio
async def test_public_thread_parent_guild_mismatch_fails_before_message_create() -> None:
    parent_id = "103"
    message_requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal message_requests
        message_requests += 1
        return httpx.Response(200, json=message(), request=request)

    def permissions(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v10/channels/{CHANNEL_ID}":
            return httpx.Response(
                200,
                json={
                    "id": CHANNEL_ID,
                    "guild_id": GUILD_ID,
                    "type": 11,
                    "parent_id": parent_id,
                    "thread_metadata": {"archived": False, "locked": False},
                },
                request=request,
            )
        if request.url.path == f"/api/v10/channels/{parent_id}":
            return httpx.Response(
                200,
                json={
                    "id": parent_id,
                    "guild_id": "999",
                    "type": 0,
                    "parent_id": None,
                    "permission_overwrites": [],
                },
                request=request,
            )
        return _default_permission_response(request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, permission_handler=permissions).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.CONFLICT
    assert message_requests == 0


@pytest.mark.asyncio
async def test_edit_repeats_the_explicit_no_mentions_policy() -> None:
    bodies: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=message(content="updated"), request=request)

    result = await gateway_with(respond).edit_message(
        channel_id=CHANNEL_ID,
        message_id="500",
        content="updated",
    )

    assert result.content == "updated"
    assert bodies == [{"content": "updated", "allowed_mentions": {"parse": []}}]


@pytest.mark.asyncio
async def test_history_paginates_past_one_hundred_and_matches_author_and_nonce() -> None:
    cursors: list[tuple[str | None, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        cursors.append((after, before))
        if after == "300":
            payload = [
                message(str(identifier), nonce="other", author_id="201")
                for identifier in range(500, 400, -1)
            ]
        elif after == "500":
            payload = []
        elif before == "401":
            payload = [
                message(
                    str(identifier),
                    nonce=NONCE if identifier == 350 else "other",
                    author_id=AUTHOR_ID if identifier == 350 else "201",
                )
                for identifier in range(400, 300, -1)
            ]
        else:
            payload = [message("300", nonce="other", author_id="201")]
        return httpx.Response(200, json=payload, request=request)

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is not None
    assert result.message_id == "350"
    assert cursors == [("300", None), ("500", None), (None, "401")]


@pytest.mark.asyncio
async def test_history_selects_the_oldest_matching_message_deterministically() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[message("502"), message("501")],
            request=request,
        )

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is not None
    assert result.message_id == "501"


@pytest.mark.asyncio
async def test_history_uses_stable_marker_when_discord_omits_nonce() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[message("501", content=f"old state\n識別子: {OPERATION_MARKER}", nonce=None)],
            request=request,
        )

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is not None
    assert result.message_id == "501"


@pytest.mark.asyncio
async def test_history_does_not_use_marker_fallback_for_an_explicit_different_nonce() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = (
            []
            if request.url.params.get("after") == "501"
            else [
                message(
                    "501",
                    content=f"old state\n識別子: {OPERATION_MARKER}",
                    nonce="different",
                )
            ]
        )
        return httpx.Response(200, json=payload, request=request)

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_history_rejects_a_marker_embedded_in_another_request_topic() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = (
            []
            if request.url.params.get("after") == "501"
            else [
                message(
                    "501",
                    content=(
                        f"議題: attacker supplied {OPERATION_MARKER}\n"
                        "識別子: sc-ffffffffffffffffffff"
                    ),
                    nonce=None,
                )
            ]
        )
        return httpx.Response(
            200,
            json=payload,
            request=request,
        )

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_history_rate_limit_returns_a_resumable_cursor() -> None:
    cursors: list[tuple[str | None, str | None]] = []

    def rate_limited(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        cursors.append((after, before))
        if after == "300":
            return httpx.Response(
                200,
                json=[
                    message(str(identifier), nonce="other", author_id="201")
                    for identifier in range(500, 400, -1)
                ],
                request=request,
            )
        return httpx.Response(
            429,
            json={"retry_after": 12.5},
            request=request,
        )

    with pytest.raises(StatusHistoryProgress) as caught:
        await gateway_with(rate_limited).find_by_nonce(
            channel_id=CHANNEL_ID,
            author_id=AUTHOR_ID,
            nonce=NONCE,
            operation_marker=OPERATION_MARKER,
            after_message_id="300",
            checkpoint=None,
        )

    assert caught.value.code is StatusDeliveryErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds == 12.5
    assert caught.value.checkpoint == StatusHistoryCheckpoint(
        history_cursor_message_id="401",
        history_verified_head_message_id="500",
    )
    assert cursors == [("300", None), ("500", None)]

    resumed_cursors: list[tuple[str | None, str | None]] = []

    def resumed(request: httpx.Request) -> httpx.Response:
        resumed_cursors.append((request.url.params.get("after"), request.url.params.get("before")))
        if request.url.params.get("after") == "500":
            return httpx.Response(200, json=[], request=request)
        return httpx.Response(
            200,
            json=[message("350")],
            request=request,
        )

    result = await gateway_with(resumed).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=caught.value.checkpoint,
    )

    assert result is not None
    assert result.message_id == "350"
    assert resumed_cursors == [("500", None), (None, "401")]


@pytest.mark.asyncio
async def test_resumed_history_checks_for_a_late_message_above_the_saved_cursor() -> None:
    cursors: list[tuple[str | None, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        cursors.append((request.url.params.get("after"), request.url.params.get("before")))
        return httpx.Response(200, json=[message("550")], request=request)

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="401",
            history_verified_head_message_id="500",
        ),
    )

    assert result is not None
    assert result.message_id == "550"
    assert cursors == [("500", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_message_id", ["600", "501"])
async def test_resumed_history_scans_the_entire_late_arrival_gap(
    matching_message_id: str,
) -> None:
    cursors: list[tuple[str | None, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        cursors.append((after, before))
        if after == "500":
            identifiers = range(750, 650, -1)
        elif before == "651":
            identifiers = range(650, 550, -1)
        elif before == "551":
            identifiers = range(550, 450, -1)
        else:
            identifiers = range(0)
        payload = [
            message(
                str(identifier),
                nonce=NONCE if str(identifier) == matching_message_id else "other",
                author_id=AUTHOR_ID if str(identifier) == matching_message_id else "201",
            )
            for identifier in identifiers
        ]
        return httpx.Response(200, json=payload, request=request)

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="401",
            history_verified_head_message_id="500",
        ),
    )

    assert result is not None
    assert result.message_id == matching_message_id
    assert cursors[:2] == [("500", None), (None, "651")]
    assert (None, "551") in cursors if matching_message_id == "501" else True


@pytest.mark.asyncio
async def test_gap_page_limit_returns_and_resumes_the_complete_checkpoint() -> None:
    def first_run(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        upper = 1700 if after == "500" else int(before or "0") - 1
        return httpx.Response(
            200,
            json=[
                message(str(identifier), nonce="other", author_id="201")
                for identifier in range(upper, upper - 100, -1)
            ],
            request=request,
        )

    with pytest.raises(StatusHistoryProgress) as caught:
        await gateway_with(first_run).find_by_nonce(
            channel_id=CHANNEL_ID,
            author_id=AUTHOR_ID,
            nonce=NONCE,
            operation_marker=OPERATION_MARKER,
            after_message_id="300",
            checkpoint=StatusHistoryCheckpoint(
                history_cursor_message_id="401",
                history_verified_head_message_id="500",
            ),
        )

    assert caught.value.checkpoint == StatusHistoryCheckpoint(
        history_cursor_message_id="401",
        history_verified_head_message_id="500",
        history_gap_cursor_message_id="701",
        history_gap_upper_message_id="1700",
    )

    cursors: list[tuple[str | None, str | None]] = []

    def resumed(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        cursors.append((after, before))
        if before == "701":
            identifiers = range(700, 600, -1)
        elif before == "601":
            identifiers = range(600, 500, -1)
        elif before == "501":
            identifiers = range(500, 400, -1)
        else:
            identifiers = range(0)
        return httpx.Response(
            200,
            json=[
                message(str(identifier), nonce="other", author_id="201")
                for identifier in identifiers
            ],
            request=request,
        )

    with pytest.raises(StatusHistoryProgress) as exhausted:
        await gateway_with(resumed).find_by_nonce(
            channel_id=CHANNEL_ID,
            author_id=AUTHOR_ID,
            nonce=NONCE,
            operation_marker=OPERATION_MARKER,
            after_message_id="300",
            checkpoint=caught.value.checkpoint,
        )

    assert exhausted.value.checkpoint == StatusHistoryCheckpoint(
        history_verified_head_message_id="1700",
    )
    assert cursors == [
        (None, "701"),
        (None, "601"),
        (None, "501"),
        ("1700", None),
        (None, "401"),
        ("1700", None),
    ]

    def stable(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("after") == "1700"
        return httpx.Response(200, json=[], request=request)

    result = await gateway_with(stable).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=exhausted.value.checkpoint,
    )
    assert result is None


@pytest.mark.asyncio
async def test_gap_completion_rechecks_for_messages_created_during_the_scan() -> None:
    cursors: list[tuple[str | None, str | None]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        before = request.url.params.get("before")
        cursors.append((after, before))
        if after == "500":
            payload = [
                message(str(identifier), nonce="other", author_id="201")
                for identifier in range(700, 600, -1)
            ]
        elif before == "601":
            payload = [
                message(str(identifier), nonce="other", author_id="201")
                for identifier in range(600, 500, -1)
            ]
        elif after == "700":
            payload = [message("800")]
        else:
            payload = []
        return httpx.Response(200, json=payload, request=request)

    result = await gateway_with(respond).find_by_nonce(
        channel_id=CHANNEL_ID,
        author_id=AUTHOR_ID,
        nonce=NONCE,
        operation_marker=OPERATION_MARKER,
        after_message_id="300",
        checkpoint=StatusHistoryCheckpoint(
            history_cursor_message_id="401",
            history_verified_head_message_id="500",
        ),
    )

    assert result is not None
    assert result.message_id == "800"
    assert cursors == [("500", None), (None, "601"), (None, "501"), ("700", None)]


@pytest.mark.asyncio
async def test_confirmed_unknown_message_is_distinct_from_unknown_channel() -> None:
    status = 10008

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": status, "message": "ignored"}, request=request)

    gateway = gateway_with(respond)
    with pytest.raises(StatusMessageMissing):
        await gateway.fetch_message(channel_id=CHANNEL_ID, message_id="500")

    status = 10003
    with pytest.raises(StatusDeliveryError) as caught:
        await gateway.fetch_message(channel_id=CHANNEL_ID, message_id="500")
    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after_without_exposing_response_or_token() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "12.5"},
            json={"retry_after": 99, "message": "provider-secret"},
            request=request,
        )

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.RATE_LIMITED
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 99
    assert TOKEN not in str(caught.value)
    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 409, 500, 503])
async def test_transient_http_statuses_are_content_free(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider-secret", request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable is True
    assert isinstance(caught.value, StatusWriteAmbiguous)
    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_transport_failure_is_retryable_and_content_free() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider-secret", request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(fail).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable is True
    assert not isinstance(caught.value, StatusWriteAmbiguous)
    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "ambiguous"),
    [
        (httpx.ConnectTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, True),
        (httpx.RemoteProtocolError, True),
    ],
)
async def test_transport_error_boundary_distinguishes_prewrite_from_ambiguous(
    error_type: type[httpx.TransportError],
    ambiguous: bool,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("provider-secret", request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(fail).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable
    assert isinstance(caught.value, StatusWriteAmbiguous) is ambiguous
    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_client_rejections_are_permanent_and_content_free(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider-secret", request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert not caught.value.retryable
    assert not isinstance(caught.value, StatusWriteAmbiguous)
    assert "provider-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_successful_create_is_treated_as_ambiguous_and_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json", request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable is True
    assert isinstance(caught.value, StatusWriteAmbiguous)


@pytest.mark.asyncio
async def test_invalid_message_shape_after_successful_create_is_ambiguous() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=message(message_id="000500"), request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).create_message(
            channel_id=CHANNEL_ID,
            content=CONTENT,
            nonce=NONCE,
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable is True
    assert isinstance(caught.value, StatusWriteAmbiguous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"not": "a message"}, id="invalid-shape"),
        pytest.param("not-json", id="invalid-json"),
    ],
)
async def test_malformed_successful_edit_is_ambiguous_and_retryable(payload: object) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(200, text=payload, request=request)
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond).edit_message(
            channel_id=CHANNEL_ID,
            message_id="500",
            content="updated",
        )

    assert caught.value.code is StatusDeliveryErrorCode.UNAVAILABLE
    assert caught.value.retryable is True
    assert isinstance(caught.value, StatusWriteAmbiguous)


@pytest.mark.asyncio
async def test_invalid_bot_identity_is_a_content_free_permanent_failure() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/oauth2/applications/@me":
            return httpx.Response(200, json={"id": APPLICATION_ID}, request=request)
        return httpx.Response(
            200,
            json={"id": "000200", "bot": True, "secret": "provider-secret"},
            request=request,
        )

    with pytest.raises(StatusDeliveryError) as caught:
        await gateway_with(respond, default_identity=False).current_bot_user_id()

    assert caught.value.code is StatusDeliveryErrorCode.REJECTED
    assert caught.value.retryable is False
    assert "provider-secret" not in str(caught.value)


def gateway_with(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    default_identity: bool = True,
    permission_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> DiscordRestStatusGateway:
    def routed(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if default_identity and path == "/api/v10/oauth2/applications/@me":
            return httpx.Response(200, json={"id": APPLICATION_ID}, request=request)
        if default_identity and path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={"id": AUTHOR_ID, "bot": True},
                request=request,
            )
        if _is_permission_request(path):
            if permission_handler is not None:
                return permission_handler(request)
            return _default_permission_response(request)
        return handler(request)

    client = httpx.Client(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(routed),
    )
    return DiscordRestStatusGateway(
        client=client,
        bot_token=TOKEN,
        expected_application_id=APPLICATION_ID,
        expected_guild_id=GUILD_ID,
    )


def _is_permission_request(path: str) -> bool:
    return (
        (path.startswith("/api/v10/channels/") and "/messages" not in path)
        or path == f"/api/v10/guilds/{GUILD_ID}/members/{AUTHOR_ID}"
        or path == f"/api/v10/guilds/{GUILD_ID}/roles"
    )


def _default_permission_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/api/v10/channels/{CHANNEL_ID}":
        payload: object = {
            "id": CHANNEL_ID,
            "guild_id": GUILD_ID,
            "type": 0,
            "parent_id": None,
            "permission_overwrites": [],
        }
    elif path == f"/api/v10/guilds/{GUILD_ID}/members/{AUTHOR_ID}":
        payload = {"user": {"id": AUTHOR_ID}, "roles": []}
    elif path == f"/api/v10/guilds/{GUILD_ID}/roles":
        payload = [{"id": GUILD_ID, "permissions": str(STATUS_PERMISSIONS)}]
    else:
        raise AssertionError(f"unexpected permission request: {path}")
    return httpx.Response(200, json=payload, request=request)
