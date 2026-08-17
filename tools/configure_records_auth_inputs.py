#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactively register private Records authentication inputs without echoing values."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient

AWS_REGION = "ap-northeast-1"
GITHUB_REPOSITORY = "pitekusu/shittim-chest"
EXPECTED_ROLE_VARIABLE = "AWS_RECORDS_PLAN_ROLE_ARN"
PARAMETER_ROOT = "/shittim-chest/production/records"
OAUTH_CONFIG_PARAMETER = f"{PARAMETER_ROOT}/discord/oauth/v0001"
CLIENT_SECRET_PARAMETER = f"{PARAMETER_ROOT}/discord/client-secret"
SESSION_KEY_PARAMETER = f"{PARAMETER_ROOT}/session-key"
OAUTH_CALLBACK_PATH = "/api/v1/auth/discord/callback"
APPLY_CONFIRMATION = "y"
MAX_STANDARD_PARAMETER_BYTES = 4_096
DISCORD_ID_PATTERN = re.compile(r"[0-9]{17,20}\Z")
HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
PLAN_ROLE_PATTERN = re.compile(
    r"arn:aws:iam::(?P<account>[0-9]{12}):role/ShittimChest-Prod-GitHub-RecordsPlan\Z"
)

SecretReader = Callable[[str], str]
SessionKeyFactory = Callable[[], str]


class SetupError(RuntimeError):
    """Stable, content-free setup failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PendingSetup:
    """Validated values that must never appear in repr or normal output."""

    parameters: Mapping[str, str] = field(repr=False)
    callback_url: str = field(repr=False)


def parameter_names() -> tuple[str, ...]:
    """Return the exact Records authentication parameter set."""

    return OAUTH_CONFIG_PARAMETER, CLIENT_SECRET_PARAMETER, SESSION_KEY_PARAMETER


def existing_parameters(client: SSMClient, targets: frozenset[str]) -> frozenset[str]:
    """Read parameter metadata only; never request or decrypt a value."""

    found: set[str] = set()
    paginator = client.get_paginator("describe_parameters")
    for page in paginator.paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [PARAMETER_ROOT]}]
    ):
        for metadata in page.get("Parameters", []):
            name = metadata.get("Name")
            if name not in targets:
                continue
            if metadata.get("Type") != "SecureString":
                raise SetupError("existing_parameter_is_not_secure_string")
            found.add(name)
    return frozenset(found)


def collect_pending_setup(
    *,
    missing_parameters: frozenset[str],
    secret_reader: SecretReader = getpass.getpass,
    session_key_factory: SessionKeyFactory = lambda: secrets.token_urlsafe(32),
) -> PendingSetup:
    """Collect only missing values and validate the complete public contract."""

    expected = frozenset(parameter_names())
    if not missing_parameters <= expected:
        raise SetupError("unexpected_parameter_requested")

    values: dict[str, str] = {}
    callback_url = ""
    if OAUTH_CONFIG_PARAMETER in missing_parameters:
        client_id = _discord_id(secret_reader, "Moderator Application ID")
        guild_id = _discord_id(secret_reader, "閲覧を許可するDiscord Guild ID")
        origin = _records_origin(_required_secret(secret_reader, "Records公開origin"))
        callback_url = f"{origin}{OAUTH_CALLBACK_PATH}"
        values[OAUTH_CONFIG_PARAMETER] = json.dumps(
            {
                "schema_version": 1,
                "client_id": client_id,
                "guild_id": guild_id,
                "allowed_origin": origin,
                "oauth_callback_url": callback_url,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    if CLIENT_SECRET_PARAMETER in missing_parameters:
        values[CLIENT_SECRET_PARAMETER] = _required_secret(
            secret_reader,
            "Moderator Application OAuth2 Client Secret",
        )

    if SESSION_KEY_PARAMETER in missing_parameters:
        session_key = session_key_factory()
        if not isinstance(session_key, str) or len(session_key) < 43:
            raise SetupError("session_key_generation_failed")
        values[SESSION_KEY_PARAMETER] = session_key

    if set(values) != set(missing_parameters):
        raise SetupError("private_input_collection_incomplete")
    if any(len(value.encode("utf-8")) > MAX_STANDARD_PARAMETER_BYTES for value in values.values()):
        raise SetupError("parameter_exceeds_standard_tier_limit")
    return PendingSetup(parameters=MappingProxyType(values), callback_url=callback_url)


def put_parameters(client: SSMClient, parameters: Mapping[str, str]) -> None:
    """Create validated SecureStrings without overwriting existing values."""

    for name, value in parameters.items():
        client.put_parameter(
            Name=name,
            Value=value,
            Description="Private authentication input for Shittim Chest Records",
            Type="SecureString",
            Tier="Standard",
            Overwrite=False,
            Tags=[
                {"Key": "Project", "Value": "shittim-chest"},
                {"Key": "Environment", "Value": "production"},
                {"Key": "Component", "Value": "records"},
            ],
        )


def require_target_account(repository: str) -> None:
    """Bind the active AWS identity to the account configured for Records Release."""

    gh = _required_executable("gh")
    aws = _required_executable("aws")
    role = subprocess.run(  # noqa: S603 - fixed command and resolved executable.
        [gh, "variable", "get", EXPECTED_ROLE_VARIABLE, "--repo", repository],
        check=False,
        capture_output=True,
        text=True,
    )
    identity = subprocess.run(  # noqa: S603 - fixed command and resolved executable.
        [aws, "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        check=False,
        capture_output=True,
        text=True,
    )
    if role.returncode != 0 or identity.returncode != 0:
        raise SetupError("production_account_preflight_failed")
    match = PLAN_ROLE_PATTERN.fullmatch(role.stdout.strip())
    if match is None or match.group("account") != identity.stdout.strip():
        raise SetupError("production_account_mismatch")


def main(argv: Sequence[str] | None = None) -> int:
    """Check metadata or interactively register missing Records auth inputs."""

    args = _parser().parse_args(argv)
    try:
        require_target_account(GITHUB_REPOSITORY)
        client = _ssm_client()
        names = frozenset(parameter_names())
        existing = existing_parameters(client, names)
        if args.check:
            _print_status(len(existing), len(names))
            return 0 if existing == names else 2
        if not sys.stdin.isatty():
            raise SetupError("interactive_terminal_required")

        missing = names - existing
        if not missing:
            print("設定済みです。追加作業はありません。")
            _print_status(len(existing), len(names))
            return 0
        print("不足している値だけを対話入力します。秘密値は画面・ログへ表示しません。")
        pending = collect_pending_setup(missing_parameters=missing)
        if pending.callback_url:
            print(f"Discord Portalへ登録するOAuth2 callback URL: {pending.callback_url}")
        confirmation = input("Portal設定を確認し、検証済みの値をSSMへ登録しますか [y/N]: ")
        if confirmation.strip().lower() != APPLY_CONFIRMATION:
            print("キャンセルしました。AWSは変更していません。")
            return 2
        put_parameters(client, pending.parameters)
        print("登録が完了しました。秘密値は表示・ローカル保存していません。")
        _print_status(len(existing) + len(pending.parameters), len(names))
        return 0
    except (BotoCoreError, ClientError, EOFError, OSError, SetupError) as error:
        code = error.code if isinstance(error, SetupError) else "records_auth_input_setup_failed"
        print(f"設定に失敗しました: {code}", file=sys.stderr)
        return 1


def _discord_id(secret_reader: SecretReader, label: str) -> str:
    value = _required_secret(secret_reader, label)
    if DISCORD_ID_PATTERN.fullmatch(value) is None:
        raise SetupError("discord_identifier_invalid")
    return value


def _records_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise SetupError("records_origin_invalid") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or hostname is None
        or HOSTNAME_PATTERN.fullmatch(hostname.lower()) is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SetupError("records_origin_invalid")
    return f"https://{hostname.lower()}"


def _required_secret(secret_reader: SecretReader, label: str) -> str:
    value = secret_reader(f"{label}: ").strip()
    if not value:
        raise SetupError("private_value_missing")
    return value


def _ssm_client() -> SSMClient:
    session = boto3.Session(region_name=AWS_REGION)
    return session.client(
        "ssm",
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
    )


def _required_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SetupError(f"{name}_executable_missing")
    return executable


def _print_status(configured: int, total: int) -> None:
    print(f"Records SSM SecureString: {configured}/{total} 設定済み")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Records OAuth/Session入力を対話入力しSSMへ安全に登録する",
    )
    parser.add_argument("--check", action="store_true", help="秘密値を読まず設定数だけ確認")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
