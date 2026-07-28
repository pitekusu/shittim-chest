"""Offline contracts for Discord's signed API Gateway HTTP interaction boundary."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from nacl.signing import SigningKey

from shittim_chest.adapters import discord_http as discord_http_module
from shittim_chest.adapters.discord_http import (
    DISCORD_HTTP_MAX_BASE64_BODY_CHARACTERS,
    DISCORD_HTTP_MAX_RAW_BODY_BYTES,
    DiscordHttpBoundary,
    DiscordHttpReception,
    DiscordPublicKeyError,
    DiscordRequestVerifier,
    extract_api_gateway_v2_request,
)
from shittim_chest.application import (
    DiscordHttpOperation,
    IngressKind,
    PanelAction,
    PanelCustomId,
)
from shittim_chest.domain import AttemptId, DebateId

NOW = datetime(2026, 7, 26, 5, 0, tzinfo=UTC)
TIMESTAMP = str(int(NOW.timestamp()))
INTERACTION_ID = "101"
APPLICATION_ID = "102"
GUILD_ID = "103"
CHANNEL_ID = "104"
REQUESTER_ID = "105"
MESSAGE_ID = "106"
PARENT_CHANNEL_ID = "107"


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


def boundary(signing_key: SigningKey) -> DiscordHttpBoundary:
    public_key = signing_key.verify_key.encode().hex()
    return DiscordHttpBoundary(DiscordRequestVerifier(public_key))


@pytest.mark.parametrize("public_key", ["", "a" * 63, "z" * 64])
def test_invalid_configured_public_key_fails_at_startup(public_key: str) -> None:
    with pytest.raises(DiscordPublicKeyError, match="invalid Discord application public key"):
        DiscordRequestVerifier(public_key)


def command_payload(
    *, question: str = "今日の朝ごはんは何がいい?甘いものが食べたい"
) -> dict[str, object]:
    return {
        "version": 1,
        "id": INTERACTION_ID,
        "application_id": APPLICATION_ID,
        "type": 2,
        "token": "handler-only-value",
        "guild_id": GUILD_ID,
        "channel_id": CHANNEL_ID,
        "channel": {"id": CHANNEL_ID, "type": 0, "parent_id": None},
        "member": {
            "nick": "サーバー表示名",
            "permissions": "0",
            "user": {
                "id": REQUESTER_ID,
                "username": "requester",
                "global_name": "グローバル表示名",
            },
        },
        "data": {
            "type": 1,
            "name": "shittim",
            "options": [{"name": "question", "type": 3, "value": question}],
        },
    }


def signed_event(
    signing_key: SigningKey,
    raw_body: bytes,
    *,
    timestamp: str = TIMESTAMP,
    base64_encoded: bool = False,
    header_names: tuple[str, str] = (
        "x-signature-ed25519",
        "x-signature-timestamp",
    ),
) -> dict[str, object]:
    signature = signing_key.sign(timestamp.encode("ascii") + raw_body).signature.hex()
    body = base64.b64encode(raw_body).decode("ascii") if base64_encoded else raw_body.decode()
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": "POST"}},
        "headers": {header_names[0]: signature, header_names[1]: timestamp},
        "body": body,
        "isBase64Encoded": base64_encoded,
    }


def receive(
    signing_key: SigningKey,
    payload: Mapping[str, object],
    *,
    now: datetime = NOW,
    base64_encoded: bool = False,
) -> DiscordHttpReception:
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return boundary(signing_key).receive(
        signed_event(signing_key, raw_body, base64_encoded=base64_encoded),
        now=now,
    )


def test_command_preserves_signed_unicode_body_and_returns_token_free_input(
    signing_key: SigningKey,
) -> None:
    question = " 今日の朝ごはんは何がいい?\n甘いものが食べたい "
    payload = command_payload(question=question)
    reception = receive(signing_key, payload)
    interaction = cast(DiscordHttpOperation, reception.interaction)

    assert reception.response is None
    assert interaction.kind is IngressKind.NEW_DEBATE
    assert interaction.question == question
    assert interaction.requester_display_name == "サーバー表示名"
    assert interaction.operation_id == INTERACTION_ID
    assert not hasattr(interaction, "token")


@pytest.mark.parametrize("base64_encoded", [False, True])
def test_plain_and_base64_api_gateway_bodies_verify_exactly(
    signing_key: SigningKey,
    base64_encoded: bool,
) -> None:
    reception = receive(signing_key, command_payload(), base64_encoded=base64_encoded)

    assert isinstance(reception.interaction, DiscordHttpOperation)


@pytest.mark.parametrize("base64_encoded", [False, True])
@pytest.mark.parametrize(
    ("body_size", "expected_status"),
    [
        (DISCORD_HTTP_MAX_RAW_BODY_BYTES, 400),
        (DISCORD_HTTP_MAX_RAW_BODY_BYTES + 1, 413),
    ],
)
def test_raw_body_limit_has_an_exact_plain_and_base64_boundary(
    signing_key: SigningKey,
    base64_encoded: bool,
    body_size: int,
    expected_status: int,
) -> None:
    raw_body = b" " * body_size
    event = signed_event(signing_key, raw_body, base64_encoded=base64_encoded)

    reception = boundary(signing_key).receive(event, now=NOW)

    assert reception.response is not None
    assert reception.response.status_code == expected_status
    if expected_status == 413:
        assert reception.response.body == '{"error":"request_too_large"}'


def test_plain_body_limit_counts_encoded_utf8_bytes(signing_key: SigningKey) -> None:
    exact_text = "é" * (DISCORD_HTTP_MAX_RAW_BODY_BYTES // 2)
    oversized_text = f"{exact_text}é"
    exact_raw = exact_text.encode("utf-8")
    oversized_raw = oversized_text.encode("utf-8")

    exact = boundary(signing_key).receive(signed_event(signing_key, exact_raw), now=NOW)
    oversized = boundary(signing_key).receive(
        signed_event(signing_key, oversized_raw),
        now=NOW,
    )

    assert exact.response is not None
    assert exact.response.status_code == 400
    assert oversized.response is not None
    assert oversized.response.status_code == 413


def test_oversized_base64_text_is_rejected_before_decode(
    signing_key: SigningKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = signed_event(signing_key, b"{}", base64_encoded=True)
    event["body"] = "A" * (DISCORD_HTTP_MAX_BASE64_BODY_CHARACTERS + 4)

    def forbidden_decode(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("oversized base64 must be rejected before decode")

    monkeypatch.setattr(discord_http_module.base64, "b64decode", forbidden_decode)

    reception = boundary(signing_key).receive(event, now=NOW)

    assert reception.response is not None
    assert reception.response.status_code == 413
    assert reception.response.body == '{"error":"request_too_large"}'


def test_oversized_plain_text_is_rejected_before_encoding_or_signature(
    signing_key: SigningKey,
) -> None:
    class EncodingForbidden(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("oversized plain text must be rejected before encoding")

    event = signed_event(signing_key, b"{}")
    event["body"] = EncodingForbidden("A" * (DISCORD_HTTP_MAX_RAW_BODY_BYTES + 1))
    headers = cast(dict[str, str], event["headers"])
    headers["x-signature-ed25519"] = "0" * 128

    reception = boundary(signing_key).receive(event, now=NOW)

    assert reception.response is not None
    assert reception.response.status_code == 413
    assert reception.response.body == '{"error":"request_too_large"}'


@pytest.mark.parametrize("base64_encoded", [False, True])
def test_huge_body_is_413_before_signature_verification(
    signing_key: SigningKey,
    base64_encoded: bool,
) -> None:
    event = signed_event(signing_key, b"{}", base64_encoded=base64_encoded)
    event["body"] = "A" * 1_000_000
    headers = cast(dict[str, str], event["headers"])
    headers["x-signature-ed25519"] = "0" * 128

    reception = boundary(signing_key).receive(event, now=NOW)

    assert reception.response is not None
    assert reception.response.status_code == 413
    assert reception.response.body == '{"error":"request_too_large"}'


def test_security_headers_are_case_insensitive(signing_key: SigningKey) -> None:
    raw_body = json.dumps(command_payload(), ensure_ascii=False).encode()
    event = signed_event(
        signing_key,
        raw_body,
        header_names=("X-Signature-Ed25519", "x-SIGNATURE-timestamp"),
    )

    reception = boundary(signing_key).receive(event, now=NOW)

    assert isinstance(reception.interaction, DiscordHttpOperation)


def test_authenticated_ping_returns_pong_without_an_operation(signing_key: SigningKey) -> None:
    reception = receive(signing_key, {"version": 1, "type": 1})

    assert reception.interaction is None
    assert reception.response is not None
    assert reception.response.status_code == 200
    assert reception.response.body == '{"type":1}'
    assert reception.response.as_event()["headers"] == {
        "content-type": "application/json; charset=utf-8"
    }


@pytest.mark.parametrize("action", [PanelAction.CANCEL, PanelAction.RETRY])
def test_component_parses_stable_operation_and_signed_member_permissions(
    signing_key: SigningKey,
    action: PanelAction,
) -> None:
    debate_id = DebateId.new()
    attempt_id = AttemptId.new()
    panel_id = PanelCustomId.for_attempt(
        debate_id=debate_id,
        attempt_id=attempt_id,
        action=action,
    )
    payload = command_payload()
    payload["type"] = 3
    payload["channel_id"] = CHANNEL_ID
    payload["channel"] = {
        "id": CHANNEL_ID,
        "type": 11,
        "parent_id": PARENT_CHANNEL_ID,
    }
    cast(dict[str, object], payload["member"])["permissions"] = str(1 << 13)
    payload["data"] = {"component_type": 2, "custom_id": panel_id.encode()}
    payload["message"] = {"id": MESSAGE_ID}

    reception = receive(signing_key, payload)
    interaction = cast(DiscordHttpOperation, reception.interaction)

    assert interaction.kind is IngressKind(action.value)
    assert interaction.operation_id == panel_id.operation_id
    assert interaction.debate_id == debate_id
    assert interaction.expected_attempt_id == attempt_id
    assert interaction.source_message_id == MESSAGE_ID
    assert interaction.source_thread_id == CHANNEL_ID
    assert interaction.parent_channel_id == PARENT_CHANNEL_ID
    assert interaction.can_manage_messages


def test_permissions_accept_future_bits_beyond_uint64(signing_key: SigningKey) -> None:
    payload = command_payload()
    cast(dict[str, object], payload["member"])["permissions"] = str((1 << 130) | (1 << 13))

    reception = receive(signing_key, payload)
    interaction = cast(DiscordHttpOperation, reception.interaction)

    assert interaction.can_manage_messages


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_signature",
        "missing_timestamp",
        "bad_signature_hex",
        "bad_signature_length",
        "bad_timestamp",
        "changed_body",
    ],
)
def test_invalid_authentication_is_always_401(signing_key: SigningKey, mutation: str) -> None:
    raw_body = json.dumps(command_payload(), ensure_ascii=False).encode()
    event = signed_event(signing_key, raw_body)
    headers = cast(dict[str, str], event["headers"])
    if mutation == "missing_signature":
        del headers["x-signature-ed25519"]
    elif mutation == "missing_timestamp":
        del headers["x-signature-timestamp"]
    elif mutation == "bad_signature_hex":
        headers["x-signature-ed25519"] = "z" * 128
    elif mutation == "bad_signature_length":
        headers["x-signature-ed25519"] = "a" * 126
    elif mutation == "bad_timestamp":
        headers["x-signature-timestamp"] = "not-a-timestamp"
    else:
        event["body"] = cast(str, event["body"]) + " "

    reception = boundary(signing_key).receive(event, now=NOW)

    assert reception.interaction is None
    assert reception.response is not None
    assert reception.response.status_code == 401
    assert reception.response.body == '{"error":"invalid_request_signature"}'


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(minutes=5, microseconds=1),
        -timedelta(minutes=5, microseconds=1),
    ],
)
def test_past_and_future_replay_outside_five_minutes_are_401(
    signing_key: SigningKey,
    offset: timedelta,
) -> None:
    reception = receive(signing_key, command_payload(), now=NOW + offset)

    assert reception.response is not None
    assert reception.response.status_code == 401


@pytest.mark.parametrize("offset", [timedelta(minutes=5), -timedelta(minutes=5)])
def test_replay_boundary_accepts_exactly_five_minutes(
    signing_key: SigningKey,
    offset: timedelta,
) -> None:
    reception = receive(signing_key, command_payload(), now=NOW + offset)

    interaction = cast(DiscordHttpOperation, reception.interaction)

    assert interaction.received_at == NOW + offset


def test_strict_base64_and_signed_invalid_json_return_400(signing_key: SigningKey) -> None:
    raw_body = b'{"type":1}'
    invalid_base64 = signed_event(signing_key, raw_body, base64_encoded=True)
    invalid_base64["body"] = "not+valid==="
    invalid_json = signed_event(signing_key, b"{")

    base64_reception = boundary(signing_key).receive(invalid_base64, now=NOW)
    json_reception = boundary(signing_key).receive(invalid_json, now=NOW)

    assert base64_reception.response is not None
    assert base64_reception.response.status_code == 400
    assert json_reception.response is not None
    assert json_reception.response.status_code == 400


@given(raw_body=st.binary(max_size=2048))
def test_arbitrary_signed_base64_body_never_escapes_the_safe_boundary(raw_body: bytes) -> None:
    signing_key = SigningKey(b"\x01" * 32)
    event = signed_event(signing_key, raw_body, base64_encoded=True)

    reception = boundary(signing_key).receive(event, now=NOW)

    assert (reception.response is None) is (reception.interaction is not None)
    if reception.response is not None:
        assert reception.response.status_code in {200, 400, 401}


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "type": 4},
        {"type": 1},
        {**command_payload(), "version": 0},
        {**command_payload(), "version": 2},
        {**command_payload(), "data": {"type": 1, "name": "other", "options": []}},
        {
            **command_payload(),
            "type": 3,
            "data": {"component_type": 2, "custom_id": "unsupported"},
            "message": {"id": MESSAGE_ID},
        },
    ],
)
def test_unsupported_signed_interactions_fail_closed(
    signing_key: SigningKey,
    payload: Mapping[str, object],
) -> None:
    reception = receive(signing_key, payload)

    assert reception.response is not None
    assert reception.response.status_code == 400


def test_excessively_long_signed_snowflake_fails_as_400(signing_key: SigningKey) -> None:
    payload = {**command_payload(), "id": "1" * 5000}

    reception = receive(signing_key, payload)

    assert reception.response is not None
    assert reception.response.status_code == 400


def test_sensitive_signed_values_are_excluded_from_repr_and_logs(
    signing_key: SigningKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_body = json.dumps(command_payload(), ensure_ascii=False).encode()
    event = signed_event(signing_key, raw_body)
    restored = extract_api_gateway_v2_request(event)

    reception = boundary(signing_key).receive(event, now=NOW)

    assert "handler-only-value" not in repr(reception)
    assert raw_body.decode() not in repr(restored)
    assert restored.signature_hex not in repr(restored)
    assert "handler-only-value" not in caplog.text
    assert restored.signature_hex not in caplog.text
