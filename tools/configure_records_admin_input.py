#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Register the private Records administrator Discord ID without echoing it."""

from __future__ import annotations

import argparse
import getpass
import hmac
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient

AWS_REGION = "ap-northeast-1"
GITHUB_REPOSITORY = "pitekusu/shittim-chest"
EXPECTED_ROLE_VARIABLE = "AWS_RECORDS_PLAN_ROLE_ARN"
PARAMETER_ROOT = "/shittim-chest/production/records"
ADMIN_DISCORD_ID_PARAMETER = f"{PARAMETER_ROOT}/admin/discord-user-id"
MAX_STANDARD_PARAMETER_BYTES = 4_096
DISCORD_ID_PATTERN = re.compile(r"[0-9]{17,20}\Z")
PLAN_ROLE_PATTERN = re.compile(
    r"arn:aws:iam::(?P<account>[0-9]{12}):role/ShittimChest-Prod-GitHub-RecordsPlan\Z"
)

SecretReader = Callable[[str], str]


class SetupError(RuntimeError):
    """Stable, content-free setup failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParameterMetadata:
    """Public SSM metadata safe to display."""

    name: str
    type: str
    tier: str
    version: int


@dataclass(frozen=True, slots=True)
class PendingSetup:
    """Validated private value retained only in memory."""

    parameters: Mapping[str, str] = field(repr=False)


def parameter_names() -> tuple[str, ...]:
    """Return the exact Records administrator parameter set."""

    return (ADMIN_DISCORD_ID_PARAMETER,)


def parameter_metadata(client: SSMClient) -> Mapping[str, ParameterMetadata]:
    """Read metadata only; never request or decrypt the administrator ID."""

    found: dict[str, ParameterMetadata] = {}
    paginator = client.get_paginator("describe_parameters")
    for page in paginator.paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [PARAMETER_ROOT]}]
    ):
        for item in page.get("Parameters", []):
            if item.get("Name") != ADMIN_DISCORD_ID_PARAMETER:
                continue
            type_ = item.get("Type")
            tier = item.get("Tier")
            version = item.get("Version")
            if type_ != "SecureString":
                raise SetupError("existing_parameter_is_not_secure_string")
            if tier != "Standard" or not isinstance(version, int) or version < 1:
                raise SetupError("existing_parameter_metadata_invalid")
            found[ADMIN_DISCORD_ID_PARAMETER] = ParameterMetadata(
                name=ADMIN_DISCORD_ID_PARAMETER,
                type=type_,
                tier=tier,
                version=version,
            )
    return MappingProxyType(found)


def collect_pending_setup(
    *,
    missing_parameters: frozenset[str],
    secret_reader: SecretReader = getpass.getpass,
) -> PendingSetup:
    """Prompt only when the exact administrator parameter is missing."""

    if not missing_parameters <= frozenset(parameter_names()):
        raise SetupError("unexpected_parameter_requested")
    values: dict[str, str] = {}
    if ADMIN_DISCORD_ID_PARAMETER in missing_parameters:
        value = secret_reader("管理者Discord user ID: ").strip()
        if DISCORD_ID_PATTERN.fullmatch(value) is None:
            raise SetupError("discord_identifier_invalid")
        confirmed_value = secret_reader("管理者Discord user ID (確認): ").strip()
        if not hmac.compare_digest(value, confirmed_value):
            raise SetupError("discord_identifier_confirmation_mismatch")
        values[ADMIN_DISCORD_ID_PARAMETER] = value
    if set(values) != set(missing_parameters):
        raise SetupError("private_input_collection_incomplete")
    if any(len(value.encode("utf-8")) > MAX_STANDARD_PARAMETER_BYTES for value in values.values()):
        raise SetupError("parameter_exceeds_standard_tier_limit")
    return PendingSetup(parameters=MappingProxyType(values))


def put_parameters(
    client: SSMClient,
    parameters: Mapping[str, str],
) -> Mapping[str, ParameterMetadata]:
    """Create the SecureString once and return only safe response metadata."""

    if set(parameters) != {ADMIN_DISCORD_ID_PARAMETER}:
        raise SetupError("unexpected_parameter_requested")
    response = client.put_parameter(
        Name=ADMIN_DISCORD_ID_PARAMETER,
        Value=parameters[ADMIN_DISCORD_ID_PARAMETER],
        Description="Private administrator identity for Shittim Chest Records",
        Type="SecureString",
        Tier="Standard",
        Overwrite=False,
        Tags=[
            {"Key": "Project", "Value": "shittim-chest"},
            {"Key": "Environment", "Value": "production"},
            {"Key": "Component", "Value": "records"},
        ],
    )
    version = response.get("Version")
    tier = response.get("Tier")
    if not isinstance(version, int) or version < 1 or tier != "Standard":
        raise SetupError("created_parameter_metadata_invalid")
    return MappingProxyType(
        {
            ADMIN_DISCORD_ID_PARAMETER: ParameterMetadata(
                name=ADMIN_DISCORD_ID_PARAMETER,
                type="SecureString",
                tier=tier,
                version=version,
            )
        }
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
    """Check metadata or interactively register the Records administrator ID."""

    args = _parser().parse_args(argv)
    try:
        require_target_account(GITHUB_REPOSITORY)
        client = _ssm_client()
        metadata = parameter_metadata(client)
        if args.check:
            _print_metadata(metadata)
            return 0 if frozenset(metadata) == frozenset(parameter_names()) else 2
        if not sys.stdin.isatty():
            raise SetupError("interactive_terminal_required")
        if ADMIN_DISCORD_ID_PARAMETER in metadata:
            print("設定済みです。追加作業はありません。")
            _print_metadata(metadata)
            return 0
        print("管理者IDを対話入力します。値は画面・ログへ表示しません。")
        pending = collect_pending_setup(missing_parameters=frozenset({ADMIN_DISCORD_ID_PARAMETER}))
        confirmation = input("検証済みの値をSSMへ登録しますか [y/N]: ")
        if confirmation.strip().lower() != "y":
            print("キャンセルしました。AWSは変更していません。")
            return 2
        complete = put_parameters(client, pending.parameters)
        print("登録が完了しました。秘密値は表示・ローカル保存していません。")
        _print_metadata(complete)
        return 0
    except (BotoCoreError, ClientError, EOFError, OSError, SetupError) as error:
        code = error.code if isinstance(error, SetupError) else "records_admin_input_setup_failed"
        print(f"設定に失敗しました: {code}", file=sys.stderr)
        return 1


def _ssm_client() -> SSMClient:
    return boto3.Session(region_name=AWS_REGION).client(
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


def _print_metadata(metadata: Mapping[str, ParameterMetadata]) -> None:
    item = metadata.get(ADMIN_DISCORD_ID_PARAMETER)
    if item is None:
        print(f"{ADMIN_DISCORD_ID_PARAMETER}: 未設定")
    else:
        print(f"{item.name}: {item.type} / {item.tier} / version {item.version}")
    print(f"Records admin SSM SecureString: {len(metadata)}/1 設定済み")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Records管理者Discord IDをSSMへ安全に登録する",
    )
    parser.add_argument("--check", action="store_true", help="秘密値を読まずmetadataだけ確認")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
