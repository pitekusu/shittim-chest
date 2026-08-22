#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Synchronize the three current Discord Bot avatars into Records media storage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from tools.configure_records_auth_inputs import SetupError, require_target_account

AWS_REGION = "ap-northeast-1"
GITHUB_REPOSITORY = "pitekusu/shittim-chest"
PARAMETER_ROOT = "/shittim-chest/production"
MEDIA_BUCKET_PREFIX = "shittim-chest-production-records-media"
DISCORD_API_ROOT = "https://discord.com/api/v10"
DISCORD_CDN_ROOT = "https://cdn.discordapp.com"
MAX_AVATAR_BYTES = 512 * 1024
APPLY_CONFIRMATION = "y"

ParticipantSlot = Literal["participant-a", "participant-b", "participant-c"]
ImageDecoder = Callable[[bytes], tuple[int, int]]
PARTICIPANT_SLOTS: tuple[ParticipantSlot, ...] = (
    "participant-a",
    "participant-b",
    "participant-c",
)
TOKEN_PARAMETERS: dict[ParticipantSlot, str] = {
    slot: f"{PARAMETER_ROOT}/discord/{slot}/token" for slot in PARTICIPANT_SLOTS
}
AVATAR_OBJECT_KEYS: dict[ParticipantSlot, str] = {
    slot: f"participants/{slot}/avatar.webp" for slot in PARTICIPANT_SLOTS
}
DISCORD_ID_PATTERN = re.compile(r"[0-9]{17,20}\Z")
AVATAR_HASH_PATTERN = re.compile(r"(?:a_)?[0-9a-f]{32}\Z")
ACCOUNT_ID_PATTERN = re.compile(r"[0-9]{12}\Z")


class AvatarSyncError(RuntimeError):
    """Stable, content-free synchronization failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParticipantAvatar:
    """One validated current avatar without private identity metadata."""

    slot: ParticipantSlot
    object_key: str
    body: bytes = field(repr=False)


def configured_token_parameters(client: Any) -> frozenset[str]:
    """Inspect exact token metadata without reading or decrypting token values."""

    expected = frozenset(TOKEN_PARAMETERS.values())
    found: set[str] = set()
    paginator = client.get_paginator("describe_parameters")
    for page in paginator.paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [PARAMETER_ROOT]}]
    ):
        for metadata in page.get("Parameters", []):
            name = metadata.get("Name")
            if name not in expected:
                continue
            if metadata.get("Type") != "SecureString":
                raise AvatarSyncError("participant_token_is_not_secure_string")
            found.add(name)
    return frozenset(found)


def load_participant_tokens(client: Any) -> dict[ParticipantSlot, str]:
    """Load the exact three Bot tokens for immediate in-memory use only."""

    names = [TOKEN_PARAMETERS[slot] for slot in PARTICIPANT_SLOTS]
    response = client.get_parameters(Names=names, WithDecryption=True)
    if response.get("InvalidParameters"):
        raise AvatarSyncError("participant_token_set_incomplete")
    raw_parameters = response.get("Parameters")
    if not isinstance(raw_parameters, list):
        raise AvatarSyncError("participant_token_response_invalid")
    values: dict[str, str] = {}
    for parameter in raw_parameters:
        if not isinstance(parameter, dict):
            raise AvatarSyncError("participant_token_response_invalid")
        name = parameter.get("Name")
        value = parameter.get("Value")
        if name not in names or name in values or not isinstance(value, str) or not value:
            raise AvatarSyncError("participant_token_response_invalid")
        values[name] = value
    if set(values) != set(names):
        raise AvatarSyncError("participant_token_set_incomplete")
    return {slot: values[TOKEN_PARAMETERS[slot]] for slot in PARTICIPANT_SLOTS}


def download_current_avatars(
    client: httpx.Client,
    tokens: Mapping[ParticipantSlot, str],
    *,
    image_decoder: ImageDecoder | None = None,
) -> tuple[ParticipantAvatar, ...]:
    """Resolve and validate all current Bot avatars before any S3 write occurs."""

    if set(tokens) != set(PARTICIPANT_SLOTS):
        raise AvatarSyncError("participant_token_set_incomplete")
    decoder = image_decoder or _decode_webp_dimensions
    avatars: list[ParticipantAvatar] = []
    resolved_user_ids: set[str] = set()
    for slot in PARTICIPANT_SLOTS:
        identity = client.get(
            f"{DISCORD_API_ROOT}/users/@me",
            headers={"Authorization": f"Bot {tokens[slot]}"},
        )
        if identity.status_code != 200:
            raise AvatarSyncError("discord_identity_request_failed")
        try:
            payload = identity.json()
        except ValueError:
            raise AvatarSyncError("discord_identity_response_invalid") from None
        if not isinstance(payload, dict):
            raise AvatarSyncError("discord_identity_response_invalid")
        user_id = payload.get("id")
        avatar_hash = payload.get("avatar")
        if (
            not isinstance(user_id, str)
            or DISCORD_ID_PATTERN.fullmatch(user_id) is None
            or not isinstance(avatar_hash, str)
            or AVATAR_HASH_PATTERN.fullmatch(avatar_hash) is None
            or payload.get("bot") is not True
        ):
            raise AvatarSyncError("discord_bot_avatar_unavailable")
        if user_id in resolved_user_ids:
            raise AvatarSyncError("discord_bot_identity_duplicated")
        resolved_user_ids.add(user_id)
        body = _download_webp(
            client,
            f"{DISCORD_CDN_ROOT}/avatars/{user_id}/{avatar_hash}.webp?size=256",
            image_decoder=decoder,
        )
        avatars.append(ParticipantAvatar(slot=slot, object_key=AVATAR_OBJECT_KEYS[slot], body=body))
    return tuple(avatars)


def upload_current_avatars(
    client: Any, bucket_name: str, avatars: Sequence[ParticipantAvatar]
) -> None:
    """Overwrite only the three stable current-profile object keys."""

    if tuple(avatar.slot for avatar in avatars) != PARTICIPANT_SLOTS:
        raise AvatarSyncError("participant_avatar_set_incomplete")
    for avatar in avatars:
        if avatar.object_key != AVATAR_OBJECT_KEYS[avatar.slot] or not _valid_webp(avatar.body):
            raise AvatarSyncError("participant_avatar_set_invalid")
    if client.get_bucket_versioning(Bucket=bucket_name).get("Status") != "Enabled":
        raise AvatarSyncError("media_bucket_versioning_required")
    uploaded: list[tuple[str, str]] = []
    try:
        for avatar in avatars:
            checksum = base64.b64encode(hashlib.sha256(avatar.body).digest()).decode("ascii")
            response = client.put_object(
                Bucket=bucket_name,
                Key=avatar.object_key,
                Body=avatar.body,
                ContentType="image/webp",
                CacheControl="private, max-age=300",
                ServerSideEncryption="AES256",
                ChecksumSHA256=checksum,
            )
            version_id = response.get("VersionId")
            if not isinstance(version_id, str) or not version_id:
                raise AvatarSyncError("participant_avatar_version_missing")
            uploaded.append((avatar.object_key, version_id))
    except BotoCoreError, ClientError, AvatarSyncError:
        try:
            for object_key, version_id in reversed(uploaded):
                client.delete_object(Bucket=bucket_name, Key=object_key, VersionId=version_id)
        except BotoCoreError, ClientError:
            raise AvatarSyncError("participant_avatar_rollback_failed") from None
        raise AvatarSyncError("participant_avatar_upload_rolled_back") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Check prerequisites or synchronize the three participant avatars."""

    args = _parser().parse_args(argv)
    try:
        require_target_account(GITHUB_REPOSITORY)
        session = boto3.Session(region_name=AWS_REGION)
        ssm = session.client("ssm", config=_aws_config())
        s3 = session.client("s3", config=_aws_config())
        sts = session.client("sts", config=_aws_config())
        account_id = _account_id(sts)
        bucket_name = f"{MEDIA_BUCKET_PREFIX}-{account_id}"
        configured = configured_token_parameters(ssm)
        s3.head_bucket(Bucket=bucket_name)
        if args.check:
            _print_status(len(configured))
            return 0 if configured == frozenset(TOKEN_PARAMETERS.values()) else 2
        if not sys.stdin.isatty():
            raise AvatarSyncError("interactive_terminal_required")
        if configured != frozenset(TOKEN_PARAMETERS.values()):
            raise AvatarSyncError("participant_token_set_incomplete")
        tokens = load_participant_tokens(ssm)
        with httpx.Client(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={"User-Agent": "ShittimChestRecordsAvatarSync/1.0.0"},
        ) as web:
            avatars = download_current_avatars(web, tokens)
        print("3体の現在のBotアイコンを検証しました。秘密値とDiscord IDは表示していません。")
        confirmation = input("Recordsの現在プロフィール用アイコン3件を上書きしますか [y/N]: ")
        if confirmation.strip().lower() != APPLY_CONFIRMATION:
            print("キャンセルしました。S3は変更していません。")
            return 2
        upload_current_avatars(s3, bucket_name, avatars)
        print("3体の現在プロフィール用アイコンを同期しました。")
        return 0
    except (
        BotoCoreError,
        ClientError,
        OSError,
        AvatarSyncError,
        SetupError,
        httpx.HTTPError,
    ) as error:
        code = (
            error.code
            if isinstance(error, (AvatarSyncError, SetupError))
            else "participant_avatar_sync_failed"
        )
        print(f"同期に失敗しました: {code}", file=sys.stderr)
        return 1


def _download_webp(
    client: httpx.Client,
    url: str,
    *,
    image_decoder: ImageDecoder,
) -> bytes:
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise AvatarSyncError("discord_avatar_download_failed")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if media_type != "image/webp":
                raise AvatarSyncError("discord_avatar_media_type_invalid")
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_AVATAR_BYTES:
                        raise AvatarSyncError("discord_avatar_too_large")
                except ValueError:
                    raise AvatarSyncError("discord_avatar_length_invalid") from None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_AVATAR_BYTES:
                    raise AvatarSyncError("discord_avatar_too_large")
                chunks.append(chunk)
    except httpx.HTTPError:
        raise AvatarSyncError("discord_avatar_download_failed") from None
    body = b"".join(chunks)
    if not _valid_webp(body) or image_decoder(body) != (256, 256):
        raise AvatarSyncError("discord_avatar_content_invalid")
    return body


def _valid_webp(body: bytes) -> bool:
    return _webp_dimensions(body) == (256, 256)


def _webp_dimensions(body: bytes) -> tuple[int, int] | None:
    if (
        len(body) < 20
        or body[:4] != b"RIFF"
        or body[8:12] != b"WEBP"
        or int.from_bytes(body[4:8], "little") + 8 != len(body)
    ):
        return None
    offset = 12
    dimensions: tuple[int, int] | None = None
    image_payload_found = False
    while offset < len(body):
        if offset + 8 > len(body):
            return None
        chunk_type = body[offset : offset + 4]
        chunk_size = int.from_bytes(body[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        padded_end = payload_end + (chunk_size % 2)
        if payload_end > len(body) or padded_end > len(body):
            return None
        payload = body[payload_start:payload_end]
        parsed: tuple[int, int] | None = None
        if chunk_type == b"VP8 ":
            parsed = _vp8_dimensions(payload)
            image_payload_found = parsed is not None
        elif chunk_type == b"VP8L":
            parsed = _vp8l_dimensions(payload)
            image_payload_found = parsed is not None
        elif chunk_type == b"VP8X":
            if len(payload) != 10 or payload[1:4] != b"\x00\x00\x00":
                return None
            parsed = (
                int.from_bytes(payload[4:7], "little") + 1,
                int.from_bytes(payload[7:10], "little") + 1,
            )
        elif chunk_type == b"ANMF":
            if len(payload) < 16:
                return None
            image_payload_found = True
        if parsed is not None:
            if dimensions is not None and dimensions != parsed:
                return None
            dimensions = parsed
        offset = padded_end
    if offset != len(body) or not image_payload_found:
        return None
    return dimensions


def _vp8_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
        return None
    width = int.from_bytes(payload[6:8], "little") & 0x3FFF
    height = int.from_bytes(payload[8:10], "little") & 0x3FFF
    return (width, height) if width and height else None


def _vp8l_dimensions(payload: bytes) -> tuple[int, int] | None:
    if len(payload) < 5 or payload[0] != 0x2F:
        return None
    bits = int.from_bytes(payload[1:5], "little")
    if bits >> 29:
        return None
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def _decode_webp_dimensions(body: bytes) -> tuple[int, int]:
    executable = shutil.which("magick")
    if executable is None:
        raise AvatarSyncError("webp_decoder_missing")
    try:
        result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
            [
                executable,
                "-limit",
                "memory",
                "32MiB",
                "-limit",
                "map",
                "64MiB",
                "-limit",
                "disk",
                "0",
                "webp:-",
                "-format",
                "%w %h",
                "info:",
            ],
            input=body,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except OSError, subprocess.TimeoutExpired:
        raise AvatarSyncError("webp_decode_failed") from None
    match = re.fullmatch(rb"([1-9][0-9]{0,4}) ([1-9][0-9]{0,4})", result.stdout)
    if result.returncode != 0 or match is None:
        raise AvatarSyncError("webp_decode_failed")
    return int(match.group(1)), int(match.group(2))


def _account_id(client: Any) -> str:
    response = client.get_caller_identity()
    account_id = response.get("Account")
    if not isinstance(account_id, str) or ACCOUNT_ID_PATTERN.fullmatch(account_id) is None:
        raise AvatarSyncError("production_account_identity_invalid")
    return account_id


def _aws_config() -> Config:
    return Config(
        connect_timeout=5,
        read_timeout=15,
        retries={"total_max_attempts": 3, "mode": "standard"},
    )


def _print_status(configured: int) -> None:
    print(f"Participant Bot token metadata: {configured}/{len(PARTICIPANT_SLOTS)} 設定済み")
    print("Records media bucket: 到達可能")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discord Botの現在のアイコンをRecordsへ安全に同期する",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="token本文を読まず、SSM metadataとMedia bucketだけを確認",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
