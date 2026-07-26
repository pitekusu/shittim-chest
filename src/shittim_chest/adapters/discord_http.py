"""Pure API Gateway v2 boundary for Discord's signed HTTP interactions."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from shittim_chest.application.discord import PanelAction, PanelCustomId
from shittim_chest.application.discord_http import (
    SHITTIM_COMMAND_NAME,
    DiscordHttpInput,
    DiscordHttpOperation,
    DiscordHttpPing,
)
from shittim_chest.application.ingress import IngressOutcome
from shittim_chest.application.scale_to_zero import IngressKind

DISCORD_SIGNATURE_REPLAY_TOLERANCE = timedelta(minutes=5)

_APPLICATION_COMMAND = 2
_MESSAGE_COMPONENT = 3
_PING = 1
_CHAT_INPUT_COMMAND = 1
_STRING_OPTION = 3
_BUTTON_COMPONENT = 2
_MANAGE_MESSAGES_PERMISSION = 1 << 13
_PUBLIC_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_SIGNATURE_PATTERN = re.compile(r"[0-9a-fA-F]{128}\Z")
_TIMESTAMP_PATTERN = re.compile(r"[0-9]{1,20}\Z")
_CONTENT_TYPE = "application/json; charset=utf-8"


class DiscordHttpBoundaryError(Exception):
    """Content-free base error safe to map at the public HTTP boundary."""

    code = "discord_http_invalid_request"
    status_code = 400

    def __init__(self) -> None:
        super().__init__(self.code)


class DiscordHttpAuthenticationError(DiscordHttpBoundaryError):
    """An interaction whose signature or timestamp cannot be trusted."""

    code = "discord_http_authentication_failed"
    status_code = 401


class DiscordHttpPayloadError(DiscordHttpBoundaryError):
    """A signed request that does not match the supported payload contract."""


class DiscordPublicKeyError(ValueError):
    """Fail-closed startup error for an invalid configured Discord public key."""

    def __init__(self) -> None:
        super().__init__("invalid Discord application public key")


@dataclass(frozen=True, slots=True)
class DiscordSignedHttpRequest:
    """Raw signed values restored from API Gateway without sensitive repr output."""

    raw_body: bytes = field(repr=False)
    signature_hex: str = field(repr=False)
    timestamp_header: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiGatewayV2Response:
    """A small typed response that can be returned directly by a Lambda handler."""

    status_code: int
    body: str

    def as_event(self) -> dict[str, object]:
        """Convert to the payload shape required by API Gateway HTTP API v2."""

        return {
            "statusCode": self.status_code,
            "headers": {"content-type": _CONTENT_TYPE},
            "body": self.body,
            "isBase64Encoded": False,
        }


@dataclass(frozen=True, slots=True)
class DiscordHttpReception:
    """Exactly one early HTTP response or one authenticated typed input."""

    response: ApiGatewayV2Response | None = None
    interaction: DiscordHttpInput | None = None

    def __post_init__(self) -> None:
        if (self.response is None) is (self.interaction is None):
            raise ValueError("HTTP reception requires exactly one result")


class DiscordRequestVerifier:
    """Verify Discord's Ed25519 signature and bounded timestamp before JSON parsing."""

    __slots__ = ("_replay_tolerance", "_verify_key")

    def __init__(
        self,
        public_key_hex: str,
        *,
        replay_tolerance: timedelta = DISCORD_SIGNATURE_REPLAY_TOLERANCE,
    ) -> None:
        if _PUBLIC_KEY_PATTERN.fullmatch(public_key_hex) is None:
            raise DiscordPublicKeyError
        if replay_tolerance <= timedelta(0):
            raise ValueError("Discord replay tolerance must be positive")
        try:
            self._verify_key = VerifyKey(bytes.fromhex(public_key_hex))
        except ValueError:
            raise DiscordPublicKeyError from None
        self._replay_tolerance = replay_tolerance

    def verify(self, request: DiscordSignedHttpRequest, *, now: datetime) -> None:
        """Authenticate the request while using the signed timestamp only for freshness."""

        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("verification clock must be timezone-aware UTC")
        if _TIMESTAMP_PATTERN.fullmatch(request.timestamp_header) is None:
            raise DiscordHttpAuthenticationError
        if _SIGNATURE_PATTERN.fullmatch(request.signature_hex) is None:
            raise DiscordHttpAuthenticationError
        try:
            timestamp_seconds = int(request.timestamp_header)
            signed_at = datetime.fromtimestamp(timestamp_seconds, tz=UTC)
            signature = bytes.fromhex(request.signature_hex)
        except OverflowError, OSError, ValueError:
            raise DiscordHttpAuthenticationError from None
        if abs((now - signed_at).total_seconds()) > self._replay_tolerance.total_seconds():
            raise DiscordHttpAuthenticationError
        signed_message = request.timestamp_header.encode("ascii") + request.raw_body
        try:
            self._verify_key.verify(signed_message, signature)
        except BadSignatureError, ValueError:
            raise DiscordHttpAuthenticationError from None


class DiscordHttpBoundary:
    """Authenticate and parse one event without invoking application or AWS services."""

    __slots__ = ("_verifier",)

    def __init__(self, verifier: DiscordRequestVerifier) -> None:
        self._verifier = verifier

    def receive(self, event: Mapping[str, object], *, now: datetime) -> DiscordHttpReception:
        """Return PONG/error immediately, otherwise one token-free typed operation."""

        try:
            request = extract_api_gateway_v2_request(event)
            self._verifier.verify(request, now=now)
            interaction = parse_verified_interaction(request.raw_body, received_at=now)
        except DiscordHttpBoundaryError as error:
            return DiscordHttpReception(response=error_response(error))
        if isinstance(interaction, DiscordHttpPing):
            return DiscordHttpReception(response=pong_response())
        return DiscordHttpReception(interaction=interaction)


def extract_api_gateway_v2_request(
    event: Mapping[str, object],
) -> DiscordSignedHttpRequest:
    """Restore raw body bytes and security headers from an HTTP API payload v2 event."""

    if event.get("version") != "2.0":
        raise DiscordHttpPayloadError
    request_context = _mapping(event.get("requestContext"))
    http_context = _mapping(request_context.get("http"))
    if http_context.get("method") != "POST":
        raise DiscordHttpPayloadError

    raw_headers = _mapping(event.get("headers"), authentication=True)
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise DiscordHttpAuthenticationError
        name = raw_name.casefold()
        if name in headers:
            raise DiscordHttpAuthenticationError
        headers[name] = raw_value
    try:
        signature = headers["x-signature-ed25519"]
        timestamp = headers["x-signature-timestamp"]
    except KeyError:
        raise DiscordHttpAuthenticationError from None

    body = event.get("body")
    if not isinstance(body, str):
        raise DiscordHttpPayloadError
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(encoded, bool):
        raise DiscordHttpPayloadError
    if encoded:
        try:
            raw_body = base64.b64decode(body.encode("ascii"), validate=True)
        except UnicodeEncodeError, binascii.Error, ValueError:
            raise DiscordHttpPayloadError from None
    else:
        try:
            raw_body = body.encode("utf-8")
        except UnicodeEncodeError:
            raise DiscordHttpPayloadError from None
    return DiscordSignedHttpRequest(
        raw_body=raw_body,
        signature_hex=signature,
        timestamp_header=timestamp,
    )


def parse_verified_interaction(
    raw_body: bytes,
    *,
    received_at: datetime,
) -> DiscordHttpInput:
    """Parse only a body that the caller has already authenticated."""

    try:
        decoded = raw_body.decode("utf-8")
        parsed = cast(
            object,
            json.loads(
                decoded,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            ),
        )
    except UnicodeDecodeError, ValueError, RecursionError, DiscordHttpPayloadError:
        raise DiscordHttpPayloadError from None
    payload = _mapping(parsed)
    if _integer(payload.get("version")) != 1:
        raise DiscordHttpPayloadError
    interaction_type = _integer(payload.get("type"))
    if interaction_type == _PING:
        return DiscordHttpPing(received_at=received_at)
    if interaction_type not in {_APPLICATION_COMMAND, _MESSAGE_COMPONENT}:
        raise DiscordHttpPayloadError

    interaction_id = _snowflake(payload.get("id"))
    application_id = _snowflake(payload.get("application_id"))
    guild_id = _snowflake(payload.get("guild_id"))
    channel_id = _snowflake(payload.get("channel_id"))
    requester_id, username, display_name, permissions = _requester(payload)
    channel_type, parent_channel_id = _channel_context(payload, channel_id=channel_id)
    data = _mapping(payload.get("data"))

    if interaction_type == _APPLICATION_COMMAND:
        return _parse_command(
            data,
            interaction_id=interaction_id,
            application_id=application_id,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_type=channel_type,
            parent_channel_id=parent_channel_id,
            requester_id=requester_id,
            username=username,
            display_name=display_name,
            permissions=permissions,
            received_at=received_at,
        )
    return _parse_component(
        payload,
        data,
        interaction_id=interaction_id,
        application_id=application_id,
        guild_id=guild_id,
        channel_id=channel_id,
        channel_type=channel_type,
        parent_channel_id=parent_channel_id,
        requester_id=requester_id,
        username=username,
        display_name=display_name,
        permissions=permissions,
        received_at=received_at,
    )


def pong_response() -> ApiGatewayV2Response:
    """Return Discord's required PONG callback with an explicit JSON content type."""

    return _json_response(200, {"type": _PING})


def error_response(error: DiscordHttpBoundaryError) -> ApiGatewayV2Response:
    """Map a content-free boundary error to its stable public status code."""

    public_code = (
        "invalid_request_signature"
        if isinstance(error, DiscordHttpAuthenticationError)
        else "invalid_request"
    )
    return _json_response(error.status_code, {"error": public_code})


def ingress_response(outcome: IngressOutcome) -> ApiGatewayV2Response:
    """Map a durable application result to one token-free ephemeral callback."""

    content = {
        IngressOutcome.STARTING: (
            "\u23f3 シッテムの箱を起動しています。\nチャンネルへ起動状況を表示します。"
        ),
        IngressOutcome.ACCEPTED: (
            "\u2705 議論依頼を受け付けました。\nチャンネルへ進行状況を表示します。"
        ),
        IngressOutcome.RETRY_STARTING: (
            "\u23f3 再試行を受け付け、シッテムの箱を起動しています。"
            "\nチャンネルへ操作状況を表示します。"
        ),
        IngressOutcome.RETRY_ACCEPTED: (
            "\u2705 再試行を受け付けました。\nチャンネルへ操作状況を表示します。"
        ),
        IngressOutcome.CANCEL_STARTING: (
            "\u23f3 取り消しを受け付け、シッテムの箱を起動しています。"
            "\nチャンネルへ操作状況を表示します。"
        ),
        IngressOutcome.CANCEL_ACCEPTED: (
            "\u2705 取り消しを受け付けました。\nチャンネルへ操作状況を表示します。"
        ),
        IngressOutcome.COMPLETED: "この依頼はすでに処理を完了しています。",
        IngressOutcome.REJECTED: "この依頼は受け付けられませんでした。",
        IngressOutcome.TERMINAL_FAILED: "この依頼は処理期限内に開始できませんでした。",
        IngressOutcome.QUEUE_FULL: (
            "\u274c 現在20件の依頼が待機しています。\nしばらくしてから再実行してください。"
        ),
        IngressOutcome.NOT_ALLOWED: "この場所または操作では利用できません。",
    }[outcome]
    return _json_response(
        200,
        {
            "data": {
                "allowed_mentions": {"parse": []},
                "content": content,
                "flags": 64,
            },
            "type": 4,
        },
    )


def ingress_unavailable_response() -> ApiGatewayV2Response:
    """Fail without claiming Discord acceptance when durable persistence is unknown."""

    return _json_response(503, {"error": "ingress_unavailable"})


def _parse_command(
    data: Mapping[str, object],
    *,
    interaction_id: str,
    application_id: str,
    guild_id: str,
    channel_id: str,
    channel_type: int | None,
    parent_channel_id: str | None,
    requester_id: str,
    username: str,
    display_name: str,
    permissions: int,
    received_at: datetime,
) -> DiscordHttpOperation:
    if _integer(data.get("type")) != _CHAT_INPUT_COMMAND:
        raise DiscordHttpPayloadError
    command_name = _text(data.get("name"))
    if command_name != SHITTIM_COMMAND_NAME:
        raise DiscordHttpPayloadError
    options = _sequence(data.get("options"))
    if len(options) != 1:
        raise DiscordHttpPayloadError
    option = _mapping(options[0])
    if _text(option.get("name")) != "question" or _integer(option.get("type")) != _STRING_OPTION:
        raise DiscordHttpPayloadError
    question = _text(option.get("value"))
    try:
        return DiscordHttpOperation(
            interaction_id=interaction_id,
            operation_id=interaction_id,
            kind=IngressKind.NEW_DEBATE,
            application_id=application_id,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_type=channel_type,
            parent_channel_id=parent_channel_id,
            requester_id=requester_id,
            requester_username=username,
            requester_display_name=display_name,
            can_manage_messages=bool(permissions & _MANAGE_MESSAGES_PERMISSION),
            received_at=received_at,
            command_name=command_name,
            question=question,
        )
    except TypeError, ValueError:
        raise DiscordHttpPayloadError from None


def _parse_component(
    payload: Mapping[str, object],
    data: Mapping[str, object],
    *,
    interaction_id: str,
    application_id: str,
    guild_id: str,
    channel_id: str,
    channel_type: int | None,
    parent_channel_id: str | None,
    requester_id: str,
    username: str,
    display_name: str,
    permissions: int,
    received_at: datetime,
) -> DiscordHttpOperation:
    if _integer(data.get("component_type")) != _BUTTON_COMPONENT:
        raise DiscordHttpPayloadError
    custom_id = _text(data.get("custom_id"))
    try:
        panel_id = PanelCustomId.parse(custom_id)
        expected_attempt_id = panel_id.expected_attempt_id()
    except ValueError:
        raise DiscordHttpPayloadError from None
    kind = IngressKind.CANCEL if panel_id.action is PanelAction.CANCEL else IngressKind.RETRY
    message = _mapping(payload.get("message"))
    source_message_id = _snowflake(message.get("id"))
    try:
        return DiscordHttpOperation(
            interaction_id=interaction_id,
            operation_id=panel_id.operation_id,
            kind=kind,
            application_id=application_id,
            guild_id=guild_id,
            channel_id=channel_id,
            channel_type=channel_type,
            parent_channel_id=parent_channel_id,
            requester_id=requester_id,
            requester_username=username,
            requester_display_name=display_name,
            can_manage_messages=bool(permissions & _MANAGE_MESSAGES_PERMISSION),
            received_at=received_at,
            debate_id=panel_id.debate_id,
            expected_attempt_id=expected_attempt_id,
            custom_id=custom_id,
            source_message_id=source_message_id,
            source_thread_id=channel_id,
        )
    except TypeError, ValueError:
        raise DiscordHttpPayloadError from None


def _requester(payload: Mapping[str, object]) -> tuple[str, str, str, int]:
    member = _mapping(payload.get("member"))
    user = _mapping(member.get("user"))
    requester_id = _snowflake(user.get("id"))
    username = _text(user.get("username"))
    global_name = _optional_text(user.get("global_name"))
    nickname = _optional_text(member.get("nick"))
    raw_permissions = _text(member.get("permissions"))
    if not raw_permissions.isascii() or not raw_permissions.isdecimal():
        raise DiscordHttpPayloadError
    try:
        permissions = int(raw_permissions)
    except ValueError:
        raise DiscordHttpPayloadError from None
    return requester_id, username, nickname or global_name or username, permissions


def _channel_context(
    payload: Mapping[str, object],
    *,
    channel_id: str,
) -> tuple[int | None, str | None]:
    raw_channel = payload.get("channel")
    if raw_channel is None:
        return None, None
    channel = _mapping(raw_channel)
    if _snowflake(channel.get("id")) != channel_id:
        raise DiscordHttpPayloadError
    channel_type = _integer(channel.get("type"))
    if not 0 <= channel_type <= 255:
        raise DiscordHttpPayloadError
    parent_channel_id = _optional_snowflake(channel.get("parent_id"))
    return channel_type, parent_channel_id


def _mapping(value: object, *, authentication: bool = False) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        if authentication:
            raise DiscordHttpAuthenticationError
        raise DiscordHttpPayloadError
    return cast(Mapping[str, object], value)


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise DiscordHttpPayloadError
    return tuple(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DiscordHttpPayloadError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscordHttpPayloadError
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value)


def _snowflake(value: object) -> str:
    text = _text(value)
    if not text.isascii() or not text.isdecimal():
        raise DiscordHttpPayloadError
    try:
        numeric = int(text)
    except ValueError:
        raise DiscordHttpPayloadError from None
    if not 0 < numeric < 2**64:
        raise DiscordHttpPayloadError
    return text


def _optional_snowflake(value: object) -> str | None:
    if value is None:
        return None
    return _snowflake(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise DiscordHttpPayloadError
        result[name] = value
    return result


def _reject_json_constant(value: str) -> object:
    del value
    raise DiscordHttpPayloadError


def _json_response(status_code: int, payload: Mapping[str, object]) -> ApiGatewayV2Response:
    return ApiGatewayV2Response(
        status_code=status_code,
        body=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
    )


__all__ = (
    "DISCORD_SIGNATURE_REPLAY_TOLERANCE",
    "ApiGatewayV2Response",
    "DiscordHttpAuthenticationError",
    "DiscordHttpBoundary",
    "DiscordHttpBoundaryError",
    "DiscordHttpPayloadError",
    "DiscordHttpReception",
    "DiscordPublicKeyError",
    "DiscordRequestVerifier",
    "DiscordSignedHttpRequest",
    "error_response",
    "extract_api_gateway_v2_request",
    "ingress_response",
    "ingress_unavailable_response",
    "parse_verified_interaction",
    "pong_response",
)
