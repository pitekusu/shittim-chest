#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactively register private Records cost inputs without echoing values."""

from __future__ import annotations

import argparse
import getpass
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
ADMIN_KEY_PARAMETER = f"{PARAMETER_ROOT}/openai/admin-key"
PROJECT_ID_PARAMETER = f"{PARAMETER_ROOT}/openai/project-id"
MAX_STANDARD_PARAMETER_BYTES = 4_096
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
    """Validated values that must never appear in repr or normal output."""

    parameters: Mapping[str, str] = field(repr=False)


def parameter_names() -> tuple[str, ...]:
    """Return the exact Records cost parameter set."""

    return ADMIN_KEY_PARAMETER, PROJECT_ID_PARAMETER


def parameter_metadata(client: SSMClient) -> Mapping[str, ParameterMetadata]:
    """Read metadata only; never request or decrypt a parameter value."""

    targets = frozenset(parameter_names())
    found: dict[str, ParameterMetadata] = {}
    paginator = client.get_paginator("describe_parameters")
    for page in paginator.paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [PARAMETER_ROOT]}]
    ):
        for item in page.get("Parameters", []):
            name = item.get("Name")
            if name not in targets:
                continue
            type_ = item.get("Type")
            tier = item.get("Tier")
            version = item.get("Version")
            if type_ != "SecureString":
                raise SetupError("existing_parameter_is_not_secure_string")
            if tier != "Standard" or not isinstance(version, int) or version < 1:
                raise SetupError("existing_parameter_metadata_invalid")
            found[name] = ParameterMetadata(
                name=name,
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
    """Prompt only for missing values and retain them only in memory."""

    expected = frozenset(parameter_names())
    if not missing_parameters <= expected:
        raise SetupError("unexpected_parameter_requested")
    values: dict[str, str] = {}
    if ADMIN_KEY_PARAMETER in missing_parameters:
        values[ADMIN_KEY_PARAMETER] = _required_secret(secret_reader, "OpenAI Admin API key")
    if PROJECT_ID_PARAMETER in missing_parameters:
        values[PROJECT_ID_PARAMETER] = _required_secret(secret_reader, "OpenAI project ID")
    if set(values) != set(missing_parameters):
        raise SetupError("private_input_collection_incomplete")
    if any(len(value.encode("utf-8")) > MAX_STANDARD_PARAMETER_BYTES for value in values.values()):
        raise SetupError("parameter_exceeds_standard_tier_limit")
    return PendingSetup(parameters=MappingProxyType(values))


def put_parameters(
    client: SSMClient, parameters: Mapping[str, str]
) -> Mapping[str, ParameterMetadata]:
    """Create validated SecureStrings and return only safe response metadata."""

    created: dict[str, ParameterMetadata] = {}
    for name, value in parameters.items():
        response = client.put_parameter(
            Name=name,
            Value=value,
            Description="Private cost input for Shittim Chest Records",
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
        created[name] = ParameterMetadata(
            name=name,
            type="SecureString",
            tier=tier,
            version=version,
        )
    return MappingProxyType(created)


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
    """Check metadata or interactively register missing Records cost inputs."""

    args = _parser().parse_args(argv)
    try:
        require_target_account(GITHUB_REPOSITORY)
        client = _ssm_client()
        metadata = parameter_metadata(client)
        names = frozenset(parameter_names())
        if args.check:
            _print_metadata(metadata)
            return 0 if frozenset(metadata) == names else 2
        if not sys.stdin.isatty():
            raise SetupError("interactive_terminal_required")
        missing = names - metadata.keys()
        if not missing:
            print("設定済みです。追加作業はありません。")
            _print_metadata(metadata)
            return 0
        print("不足している値だけを対話入力します。秘密値は画面・ログへ表示しません。")
        pending = collect_pending_setup(missing_parameters=frozenset(missing))
        confirmation = input("検証済みの値をSSMへ登録しますか [y/N]: ")
        if confirmation.strip().lower() != "y":
            print("キャンセルしました。AWSは変更していません。")
            return 2
        created_metadata = put_parameters(client, pending.parameters)
        complete_metadata = MappingProxyType({**metadata, **created_metadata})
        if frozenset(complete_metadata) != names:
            raise SetupError("created_parameter_metadata_incomplete")
        print("登録が完了しました。秘密値は表示・ローカル保存していません。")
        _print_metadata(complete_metadata)
        return 0
    except (BotoCoreError, ClientError, EOFError, OSError, SetupError) as error:
        code = error.code if isinstance(error, SetupError) else "records_cost_input_setup_failed"
        print(f"設定に失敗しました: {code}", file=sys.stderr)
        return 1


def _required_secret(secret_reader: SecretReader, label: str) -> str:
    value = secret_reader(f"{label}: ").strip()
    if not value:
        raise SetupError("private_value_missing")
    return value


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
    for name in parameter_names():
        item = metadata.get(name)
        if item is None:
            print(f"{name}: 未設定")
        else:
            print(f"{item.name}: {item.type} / {item.tier} / version {item.version}")
    print(f"Records cost SSM SecureString: {len(metadata)}/{len(parameter_names())} 設定済み")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Records OpenAI費用入力を対話入力しSSMへ安全に登録する",
    )
    parser.add_argument("--check", action="store_true", help="秘密値を読まずmetadataだけ確認")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
