"""Tests for current-only Records participant avatar synchronization."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx
import pytest
from tools import sync_records_participant_avatars as sync


def webp(width: int = 256, height: int = 256) -> bytes:
    payload = (
        b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
        + b"encoded"
    )
    chunk = b"VP8 " + len(payload).to_bytes(4, "little") + payload
    if len(payload) % 2:
        chunk += b"\x00"
    riff_payload = b"WEBP" + chunk
    return b"RIFF" + len(riff_payload).to_bytes(4, "little") + riff_payload


WEBP = webp()


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
    def __init__(
        self,
        *,
        fail_at: int | None = None,
        delete_fail_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.fail_at = fail_at
        self.delete_fail_keys = delete_fail_keys

    def get_bucket_versioning(self, **kwargs: Any) -> dict[str, str]:
        assert kwargs == {"Bucket": "private-media"}
        return {"Status": "Enabled"}

    def put_object(self, **kwargs: Any) -> dict[str, str]:
        if self.fail_at == len(self.calls):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "failed"}},
                "PutObject",
            )
        self.calls.append(kwargs)
        return {"VersionId": f"version-{len(self.calls)}"}

    def delete_object(self, **kwargs: Any) -> None:
        self.delete_calls.append(kwargs)
        if kwargs["Key"] in self.delete_fail_keys:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "failed"}},
                "DeleteObject",
            )


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
        avatars = sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (256, 256),
        )

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
        sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (256, 256),
        )

    assert caught.value.code == expected_code


def test_rejects_duplicate_resolved_bot_identity_before_upload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={"id": "1" * 18, "avatar": "a" * 32, "bot": True},
            )
        return httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp"})

    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(sync.AvatarSyncError) as caught,
    ):
        sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (256, 256),
        )

    assert caught.value.code == "discord_bot_identity_duplicated"


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
        sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (256, 256),
        )

    assert caught.value.code == "discord_avatar_media_type_invalid"


@pytest.mark.parametrize("body", (webp(128, 128), WEBP[:-1], b"RIFFinvalidWEBP"))
def test_rejects_malformed_or_incorrectly_sized_webp(body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={"id": "1" * 18, "avatar": "a" * 32, "bot": True},
            )
        return httpx.Response(200, content=body, headers={"Content-Type": "image/webp"})

    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(sync.AvatarSyncError) as caught,
    ):
        sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (256, 256),
        )

    assert caught.value.code == "discord_avatar_content_invalid"


def test_rejects_webp_that_the_decoder_cannot_confirm() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v10/users/@me":
            return httpx.Response(
                200,
                json={"id": "1" * 18, "avatar": "a" * 32, "bot": True},
            )
        return httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp"})

    tokens = {slot: f"token-{slot}" for slot in sync.PARTICIPANT_SLOTS}
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(sync.AvatarSyncError) as caught,
    ):
        sync.download_current_avatars(
            client,
            tokens,
            image_decoder=lambda _body: (128, 128),
        )

    assert caught.value.code == "discord_avatar_content_invalid"


def test_decoder_dependency_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync._decode_webp_dimensions(WEBP)

    assert caught.value.code == "webp_decoder_missing"


def test_decoder_uses_stdin_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return sync.subprocess.CompletedProcess(command, 0, stdout=b"256 256", stderr=b"")

    monkeypatch.setattr(sync.shutil, "which", lambda _name: "/usr/bin/magick")
    monkeypatch.setattr(sync.subprocess, "run", run)

    assert sync._decode_webp_dimensions(WEBP) == (256, 256)
    assert captured["command"][0] == "/usr/bin/magick"
    assert captured["kwargs"]["input"] == WEBP
    assert captured["kwargs"]["timeout"] == 10
    assert "shell" not in captured["kwargs"]


def test_account_preflight_failure_returns_stable_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_preflight(_repository: str) -> None:
        raise sync.SetupError("production_account_preflight_failed")

    monkeypatch.setattr(sync, "require_target_account", fail_preflight)

    assert sync.main(["--check"]) == 1
    assert capsys.readouterr().err == ("同期に失敗しました: production_account_preflight_failed\n")


def test_confirmation_eof_returns_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync._read_confirmation()

    assert caught.value.code == "confirmation_input_unavailable"


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
    assert client.delete_calls == []


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


def test_partial_upload_is_rolled_back_to_previous_versions() -> None:
    client = FakeS3(fail_at=1)
    avatars = tuple(
        sync.ParticipantAvatar(
            slot=slot,
            object_key=sync.AVATAR_OBJECT_KEYS[slot],
            body=WEBP,
        )
        for slot in sync.PARTICIPANT_SLOTS
    )

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync.upload_current_avatars(client, "private-media", avatars)

    assert caught.value.code == "participant_avatar_upload_rolled_back"
    assert client.delete_calls == [
        {
            "Bucket": "private-media",
            "Key": "participants/participant-a/avatar.webp",
            "VersionId": "version-1",
        }
    ]


def test_rollback_attempts_every_uploaded_version_after_one_delete_fails() -> None:
    client = FakeS3(
        fail_at=2,
        delete_fail_keys=frozenset({"participants/participant-b/avatar.webp"}),
    )
    avatars = tuple(
        sync.ParticipantAvatar(
            slot=slot,
            object_key=sync.AVATAR_OBJECT_KEYS[slot],
            body=WEBP,
        )
        for slot in sync.PARTICIPANT_SLOTS
    )

    with pytest.raises(sync.AvatarSyncError) as caught:
        sync.upload_current_avatars(client, "private-media", avatars)

    assert caught.value.code == "participant_avatar_rollback_failed"
    assert caught.value.rollback_outcomes == (
        ("participant-b", "failed"),
        ("participant-a", "deleted"),
    )
    assert [call["Key"] for call in client.delete_calls] == [
        "participants/participant-b/avatar.webp",
        "participants/participant-a/avatar.webp",
    ]
    assert sync._failure_message(caught.value) == (
        "participant_avatar_rollback_failed (participant-b=failed,participant-a=deleted)"
    )
