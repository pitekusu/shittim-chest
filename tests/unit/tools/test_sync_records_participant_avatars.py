"""Tests for current-only Records participant avatar synchronization."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
from tools import sync_records_participant_avatars as sync

WEBP = b"RIFF\x04\x00\x00\x00WEBPtest"


class FakePaginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {
            "ParameterFilters": [
                {
                    "Key": "Path",
                    "Option": "Recursive",
                    "Values": [sync.PARAMETER_ROOT],
                }
            ]
        }
        return self.pages


class FakeSsm:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    def get_paginator(self, operation: str) -> FakePaginator:
        assert operation == "describe_parameters"
        return FakePaginator([self.response])

    def get_parameters(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {
            "Names": [sync.TOKEN_PARAMETERS[slot] for slot in sync.PARTICIPANT_SLOTS],
            "WithDecryption": True,
        }
        return self.response


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_current_avatar_contract_uses_three_stable_slot_keys() -> None:
    assert sync.AVATAR_OBJECT_KEYS == {
        "participant-a": "participants/participant-a/avatar.webp",
        "participant-b": "participants/participant-b/avatar.webp",
        "participant-c": "participants/participant-c/avatar.webp",
    }


def test_token_metadata_check_does_not_read_values() -> None:
    parameters = [
        {"Name": sync.TOKEN_PARAMETERS[slot], "Type": "SecureString"}
        for slot in sync.PARTICIPANT_SLOTS
    ]

    assert sync.configured_token_parameters(FakeSsm({"Parameters": parameters})) == frozenset(
        sync.TOKEN_PARAMETERS.values()
    )


def test_token_metadata_requires_secure_string() -> None:
    parameters = [
        {"Name": sync.TOKEN_PARAMETERS["participant-a"], "Type": "String"},
    ]

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync.configured_token_parameters(FakeSsm({"Parameters": parameters}))

    assert caught.value.code == "participant_token_is_not_secure_string"


def test_loads_only_the_exact_three_tokens() -> None:
    response = {
        "Parameters": [
            {"Name": sync.TOKEN_PARAMETERS[slot], "Value": f"token-{slot}"}
            for slot in reversed(sync.PARTICIPANT_SLOTS)
        ]
    }

    assert sync.load_participant_tokens(FakeSsm(response)) == {
        slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS
    }

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync.load_participant_tokens(FakeSsm({"Parameters": response["Parameters"][:2]}))
    assert caught.value.code == "participant_token_set_incomplete"


def test_downloads_and_validates_all_current_bot_avatars() -> None:
    ids = {slot: str(index) * 18 for index, slot in enumerate(sync.PARTICIPANT_SLOTS, start=1)}
    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    hashes = {slot: f"{index:x}" * 32 for index, slot in enumerate(sync.PARTICIPANT_SLOTS, start=1)}
    requested_cdn_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            token = request.headers["Authorization"].removeprefix("Bot ")
            slot = next(slot for slot, expected in tokens.items() if token == expected)
            return httpx.Response(
                200,
                json={"id": ids[slot], "avatar": hashes[slot], "bot": True},
            )
        requested_cdn_urls.append(str(request.url))
        return httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        avatars = sync.download_current_avatars(client, tokens)

    assert tuple(avatar.slot for avatar in avatars) == sync.PARTICIPANT_SLOTS
    assert tuple(avatar.object_key for avatar in avatars) == tuple(sync.AVATAR_OBJECT_KEYS.values())
    assert all(avatar.body == WEBP for avatar in avatars)
    assert requested_cdn_urls == [
        f"{sync.DISCORD_CDN_ROOT}/avatars/{ids[slot]}/{hashes[slot]}.webp?size=256"
        for slot in sync.PARTICIPANT_SLOTS
    ]


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    (
        ({"id": "1" * 18, "avatar": None, "bot": True}, "discord_bot_avatar_unavailable"),
        (
            {"id": "1" * 18, "avatar": "a" * 32, "bot": False},
            "discord_bot_avatar_unavailable",
        ),
    ),
)
def test_rejects_identity_without_a_current_bot_avatar(
    payload: dict[str, Any],
    expected_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v10/users/@me"
        return httpx.Response(200, json=payload)

    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(sync.AvatarSyncError) as caught,
    ):
        sync.download_current_avatars(client, tokens)

    assert caught.value.code == expected_code


def test_rejects_non_webp_avatar_before_any_upload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={"id": "1" * 18, "avatar": "a" * 32, "bot": True},
            )
        return httpx.Response(200, content=b"not-webp", headers={"Content-Type": "image/png"})

    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(sync.AvatarSyncError) as caught,
    ):
        sync.download_current_avatars(client, tokens)

    assert caught.value.code == "discord_avatar_media_type_invalid"


def test_uploads_only_stable_current_keys_with_integrity_metadata() -> None:
    client = FakeS3()
    avatars = tuple(
        sync.ParticipantAvatar(
            slot=slot,
            object_key=sync.AVATAR_OBJECT_KEYS[slot],
            body=WEBP,
        )
        for slot in sync.PARTICIPANT_SLOTS
    )

    sync.upload_current_avatars(client, "private-media", avatars)

    assert [call["Key"] for call in client.calls] == list(sync.AVATAR_OBJECT_KEYS.values())
    assert all(call["Bucket"] == "private-media" for call in client.calls)
    assert all(call["ContentType"] == "image/webp" for call in client.calls)
    assert all(call["ServerSideEncryption"] == "AES256" for call in client.calls)
    expected_checksum = base64.b64encode(hashlib.sha256(WEBP).digest()).decode("ascii")
    assert all(call["ChecksumSHA256"] == expected_checksum for call in client.calls)


def test_incomplete_upload_set_fails_before_any_s3_write() -> None:
    client = FakeS3()
    avatar = sync.ParticipantAvatar(
        slot="participant-a",
        object_key=sync.AVATAR_OBJECT_KEYS["participant-a"],
        body=WEBP,
    )

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync.upload_current_avatars(client, "private-media", (avatar,))

    assert caught.value.code == "participant_avatar_set_incomplete"
    assert client.calls == []
