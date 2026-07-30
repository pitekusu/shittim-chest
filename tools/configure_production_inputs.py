#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactively register STEP-10 private inputs without echoing their values."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from shittim_chest.adapters.discord_http import DiscordPublicKeyError, DiscordRequestVerifier
from shittim_chest.application import DiscordBotSlot
from shittim_chest.config.models import (
    PersonaConfigPayload,
    StartupConfigurationError,
    parse_discord_runtime_config,
)

if TYPE_CHECKING:
    from mypy_boto3_ssm.client import SSMClient

AWS_REGION = "ap-northeast-1"
GITHUB_REPOSITORY = "pitekusu/shittim-chest"
GITHUB_EMAIL_SECRET = "OPERATOR_NOTIFICATION_EMAIL"  # noqa: S105 - name, not value.
EXPECTED_ROLE_VARIABLE = "AWS_RELEASE_PLAN_ROLE_ARN"
PARAMETER_ROOT = "/shittim-chest/production"
CONFIG_VERSION_PATTERN = re.compile(r"v[0-9]{4}\Z")
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
PLAN_ROLE_PATTERN = re.compile(
    r"arn:aws:iam::(?P<account>[0-9]{12}):role/ShittimChest-Prod-GitHub-ReleasePlan\Z"
)
MAX_STANDARD_PARAMETER_BYTES = 4_096
MAX_PRIVATE_SOURCE_BYTES = 256 * 1_024
APPLY_CONFIRMATION = "y"
PRIVATE_SOURCE_ENV = "SHITTIM_PRIVATE_CONFIG_SOURCE"
PRIVATE_SOURCE_POINTER = Path(__file__).resolve().parents[1] / ".env.private-config"
DEFAULT_CONFIG_VERSION = "v0002"
_PERSONA_HEADING_PATTERN = re.compile(r"## PersonaConfig (?P<version>v[0-9]{4})\Z")
_PERSONA_SECTION_PATTERN = re.compile(
    r"### (?P<slot>participant-[abc]): (?P<display_name>[^\r\n]+)\Z"
)

SecretReader = Callable[[str], str]


class SetupError(RuntimeError):
    """Stable, content-free setup failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PendingSetup:
    """Validated values that must never appear in repr or normal output."""

    github_email: str | None = field(repr=False)
    parameters: Mapping[str, str] = field(repr=False)


def parameter_names(config_version: str) -> tuple[str, ...]:
    """Return the complete public-safe set of production parameter names."""

    _validate_config_version(config_version)
    persona_root = f"{PARAMETER_ROOT}/personas/{config_version}"
    return (
        f"{PARAMETER_ROOT}/openai/api-key",
        f"{PARAMETER_ROOT}/discord/moderator/public-key",
        f"{PARAMETER_ROOT}/discord/moderator/token",
        f"{PARAMETER_ROOT}/discord/participant-a/token",
        f"{PARAMETER_ROOT}/discord/participant-b/token",
        f"{PARAMETER_ROOT}/discord/participant-c/token",
        f"{PARAMETER_ROOT}/runtime/{config_version}",
        f"{persona_root}/moderator",
        f"{persona_root}/participant-a",
        f"{persona_root}/participant-b",
        f"{persona_root}/participant-c",
    )


def existing_parameters(client: SSMClient, targets: frozenset[str]) -> frozenset[str]:
    """Read parameter metadata only; never request or decrypt values."""

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
    config_version: str,
    missing_parameters: frozenset[str],
    github_email_missing: bool,
    secret_reader: SecretReader = getpass.getpass,
    saved_personas: Mapping[DiscordBotSlot, str] | None = None,
) -> PendingSetup:
    """Collect and validate only missing values through hidden terminal prompts."""

    _validate_config_version(config_version)
    values: dict[str, str] = {}
    email = None
    if github_email_missing:
        email = _required_secret(secret_reader, "通知先メール")
        if EMAIL_PATTERN.fullmatch(email) is None:
            raise SetupError("operator_email_invalid")

    runtime_name = f"{PARAMETER_ROOT}/runtime/{config_version}"
    if runtime_name in missing_parameters:
        values[runtime_name] = _runtime_json(config_version, secret_reader)

    public_key_name = f"{PARAMETER_ROOT}/discord/moderator/public-key"
    if public_key_name in missing_parameters:
        public_key = _required_secret(secret_reader, "moderator Application Public Key")
        try:
            DiscordRequestVerifier(public_key)
        except DiscordPublicKeyError:
            raise SetupError("discord_public_key_invalid") from None
        values[public_key_name] = public_key

    credential_prompts = (
        (f"{PARAMETER_ROOT}/discord/moderator/token", "moderator Bot token"),
        (f"{PARAMETER_ROOT}/discord/participant-a/token", "participant-a Bot token"),
        (f"{PARAMETER_ROOT}/discord/participant-b/token", "participant-b Bot token"),
        (f"{PARAMETER_ROOT}/discord/participant-c/token", "participant-c Bot token"),
        (f"{PARAMETER_ROOT}/openai/api-key", "OpenAI API key"),
    )
    new_tokens: list[str] = []
    for name, prompt in credential_prompts:
        if name not in missing_parameters:
            continue
        value = _required_secret(secret_reader, prompt)
        values[name] = value
        if name.endswith("/token"):
            new_tokens.append(value)
    if len(new_tokens) != len(set(new_tokens)):
        raise SetupError("discord_tokens_must_be_distinct")

    validated_saved_personas = _validated_saved_personas(
        config_version,
        saved_personas or {},
    )
    for slot in DiscordBotSlot:
        name = f"{PARAMETER_ROOT}/personas/{config_version}/{slot.value}"
        if name in missing_parameters:
            values[name] = validated_saved_personas.get(slot) or _persona_json(
                config_version,
                slot,
                secret_reader,
            )

    unexpected = set(values) - set(missing_parameters)
    if unexpected or set(missing_parameters) - set(values):
        raise SetupError("private_input_collection_incomplete")
    for value in values.values():
        if len(value.encode("utf-8")) > MAX_STANDARD_PARAMETER_BYTES:
            raise SetupError("parameter_exceeds_standard_tier_limit")
    return PendingSetup(github_email=email, parameters=values)


def put_parameters(client: SSMClient, parameters: Mapping[str, str]) -> None:
    """Create validated SecureStrings without overwriting any existing version."""

    for name, value in parameters.items():
        client.put_parameter(
            Name=name,
            Value=value,
            Description="The Shittim Chest private production input",
            Type="SecureString",
            Tier="Standard",
            Overwrite=False,
            Tags=[
                {"Key": "Project", "Value": "shittim-chest"},
                {"Key": "Environment", "Value": "production"},
            ],
        )


def github_secret_names(repository: str) -> frozenset[str]:
    """List GitHub secret names only; values are unavailable by design."""

    gh = _required_executable("gh")
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [gh, "secret", "list", "--app", "actions", "--repo", repository, "--json", "name"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SetupError("github_secret_list_failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SetupError("github_secret_list_invalid") from None
    if not isinstance(payload, list):
        raise SetupError("github_secret_list_invalid")
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SetupError("github_secret_list_invalid")
        names.add(item["name"])
    return frozenset(names)


def set_github_secret(repository: str, email: str) -> None:
    """Send the private email to gh over stdin, never a process argument."""

    gh = _required_executable("gh")
    result = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [gh, "secret", "set", GITHUB_EMAIL_SECRET, "--repo", repository],
        input=email.encode("utf-8"),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SetupError("github_secret_write_failed")


def require_target_account(repository: str) -> None:
    """Bind the active AWS identity to the account configured on GitHub."""

    gh = _required_executable("gh")
    aws = _required_executable("aws")
    role = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [gh, "variable", "get", EXPECTED_ROLE_VARIABLE, "--repo", repository],
        check=False,
        capture_output=True,
        text=True,
    )
    identity = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [aws, "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        check=False,
        capture_output=True,
        text=True,
    )
    if role.returncode != 0 or identity.returncode != 0:
        raise SetupError("production_account_preflight_failed")
    match = PLAN_ROLE_PATTERN.fullmatch(role.stdout.strip())
    account = identity.stdout.strip()
    if match is None or match.group("account") != account:
        raise SetupError("production_account_mismatch")


def private_config_source(
    environ: Mapping[str, str] = os.environ,
    pointer: Path = PRIVATE_SOURCE_POINTER,
) -> Path | None:
    """Resolve an optional local-only source pointer without displaying its path."""

    configured = environ.get(PRIVATE_SOURCE_ENV, "").strip()
    if not configured and pointer.exists():
        try:
            lines = [
                line.strip()
                for line in pointer.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except OSError:
            raise SetupError("private_config_pointer_invalid") from None
        prefix = f"{PRIVATE_SOURCE_ENV}="
        if len(lines) != 1 or not lines[0].startswith(prefix):
            raise SetupError("private_config_pointer_invalid")
        configured = lines[0][len(prefix) :].strip()
    if not configured:
        return None
    source = Path(configured).expanduser()
    if not source.is_absolute():
        raise SetupError("private_config_source_invalid")
    try:
        size = source.stat().st_size
    except OSError:
        raise SetupError("private_config_source_unavailable") from None
    if not source.is_file() or size <= 0 or size > MAX_PRIVATE_SOURCE_BYTES:
        raise SetupError("private_config_source_invalid")
    return source


def load_saved_personas(source: Path, config_version: str) -> Mapping[DiscordBotSlot, str]:
    """Load and validate private persona text without emitting its contents."""

    _validate_config_version(config_version)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        raise SetupError("private_config_source_invalid") from None
    try:
        return _personas_from_operator_markdown(raw, config_version)
    except SetupError:
        raise
    except TypeError, ValueError:
        raise SetupError("saved_persona_config_invalid") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Check readiness or run the one-command private-input setup."""

    args = _parser().parse_args(argv)
    try:
        names = frozenset(parameter_names(args.config_version))
        require_target_account(GITHUB_REPOSITORY)
        client = _ssm_client()
        existing = existing_parameters(client, names)
        configured_github_secrets = github_secret_names(GITHUB_REPOSITORY)
        email_missing = GITHUB_EMAIL_SECRET not in configured_github_secrets
        if args.check:
            _print_status(len(existing), len(names), email_missing)
            return 0 if len(existing) == len(names) and not email_missing else 2
        if not sys.stdin.isatty():
            raise SetupError("interactive_terminal_required")

        missing = names - existing
        saved_personas: Mapping[DiscordBotSlot, str] = {}
        persona_names = {
            f"{PARAMETER_ROOT}/personas/{args.config_version}/{slot.value}"
            for slot in DiscordBotSlot
        }
        if missing & persona_names:
            source = private_config_source()
            if source is not None:
                saved_personas = load_saved_personas(source, args.config_version)
                print("保存済みのPersonaConfigを検証して再利用します。")
        print("不足している値だけを対話入力します。入力内容は画面・ログへ表示しません。")
        pending = collect_pending_setup(
            config_version=args.config_version,
            missing_parameters=missing,
            github_email_missing=email_missing,
            saved_personas=saved_personas,
        )
        if pending.github_email is None and not pending.parameters:
            print("設定済みです。追加作業はありません。")
            return 0
        confirmation = input("検証済みの値をGitHub/AWSへ登録しますか [y/N]: ").strip().lower()
        if confirmation != APPLY_CONFIRMATION:
            print("キャンセルしました。AWS/GitHubは変更していません。")
            return 2
        if pending.github_email is not None:
            set_github_secret(GITHUB_REPOSITORY, pending.github_email)
        put_parameters(client, pending.parameters)
        print("登録が完了しました。秘密値は表示・ローカル保存していません。")
        _print_status(len(existing) + len(pending.parameters), len(names), False)
        return 0
    except (
        BotoCoreError,
        ClientError,
        EOFError,
        OSError,
        SetupError,
        StartupConfigurationError,
    ) as error:
        code = error.code if isinstance(error, SetupError) else "private_input_setup_failed"
        print(f"設定に失敗しました: {code}", file=sys.stderr)
        return 1


def _runtime_json(config_version: str, secret_reader: SecretReader) -> str:
    guild_id = _required_secret(secret_reader, "Discord Guild ID")
    channel_ids = tuple(
        value.strip()
        for value in _required_secret(
            secret_reader,
            "許可するChannel ID (複数はカンマ区切り)",
        ).split(",")
        if value.strip()
    )
    identities = [
        {
            "slot": slot.value,
            "application_id": _required_secret(
                secret_reader,
                f"{slot.value} Application ID",
            ),
        }
        for slot in DiscordBotSlot
    ]
    value = json.dumps(
        {
            "schema_version": "1",
            "config_version": config_version,
            "guild_id": guild_id,
            "allowed_channel_ids": channel_ids,
            "identities": identities,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    parse_discord_runtime_config(value)
    return value


def _persona_json(
    config_version: str,
    slot: DiscordBotSlot,
    secret_reader: SecretReader,
) -> str:
    display_name = _required_secret(secret_reader, f"{slot.value} display name")
    system_prompt = _multiline_secret(secret_reader, f"{slot.value} system prompt")
    value = json.dumps(
        {
            "schema_version": "1",
            "config_version": config_version,
            "slot": slot.value,
            "display_name": display_name,
            "system_prompt": system_prompt,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    PersonaConfigPayload.model_validate_json(value)
    return value


def _personas_from_operator_markdown(
    raw: str,
    config_version: str,
) -> Mapping[DiscordBotSlot, str]:
    lines = raw.splitlines()
    heading_indexes = [
        index
        for index, line in enumerate(lines)
        if (match := _PERSONA_HEADING_PATTERN.fullmatch(line)) is not None
        and match.group("version") == config_version
    ]
    if len(heading_indexes) != 1:
        raise SetupError("saved_persona_version_missing")
    start = heading_indexes[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = lines[start:end]
    table = _persona_table(section, config_version)
    prompts = _participant_prompts(section)
    if set(table) != set(DiscordBotSlot) or set(prompts) != {
        DiscordBotSlot.PARTICIPANT_A,
        DiscordBotSlot.PARTICIPANT_B,
        DiscordBotSlot.PARTICIPANT_C,
    }:
        raise SetupError("saved_persona_set_invalid")

    values: dict[DiscordBotSlot, str] = {}
    for slot in DiscordBotSlot:
        display_name, summary = table[slot]
        participant = prompts.get(slot)
        if participant is not None and participant[0] != display_name:
            raise SetupError("saved_persona_display_name_mismatch")
        system_prompt = summary if participant is None else participant[1]
        value = json.dumps(
            {
                "schema_version": "1",
                "config_version": config_version,
                "slot": slot.value,
                "display_name": display_name,
                "system_prompt": system_prompt,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        PersonaConfigPayload.model_validate_json(value)
        values[slot] = value
    return MappingProxyType(values)


def _persona_table(
    lines: Sequence[str],
    config_version: str,
) -> Mapping[DiscordBotSlot, tuple[str, str]]:
    rows: dict[DiscordBotSlot, tuple[str, str]] = {}
    for line in lines:
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line[1:-1].split("|")]
        if len(cells) != 4:
            continue
        try:
            slot = DiscordBotSlot(cells[0])
        except ValueError:
            continue
        expected_path = f"{PARAMETER_ROOT}/personas/{config_version}/{slot.value}"
        if slot in rows or not cells[1] or not cells[2] or cells[3] != expected_path:
            raise SetupError("saved_persona_table_invalid")
        rows[slot] = (cells[1], cells[2])
    return MappingProxyType(rows)


def _participant_prompts(
    lines: Sequence[str],
) -> Mapping[DiscordBotSlot, tuple[str, str]]:
    prompts: dict[DiscordBotSlot, tuple[str, str]] = {}
    index = 0
    while index < len(lines):
        match = _PERSONA_SECTION_PATTERN.fullmatch(lines[index])
        if match is None:
            index += 1
            continue
        slot = DiscordBotSlot(match.group("slot"))
        if slot in prompts:
            raise SetupError("saved_persona_section_invalid")
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or lines[index] != "```text":
            raise SetupError("saved_persona_section_invalid")
        index += 1
        prompt_lines: list[str] = []
        while index < len(lines) and lines[index] != "```":
            prompt_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise SetupError("saved_persona_section_invalid")
        prompt = "\n".join(prompt_lines).strip()
        table_display_name = match.group("display_name").strip()
        if not prompt or not table_display_name:
            raise SetupError("saved_persona_section_invalid")
        prompts[slot] = (table_display_name, prompt)
        index += 1
    return MappingProxyType(prompts)


def _validated_saved_personas(
    config_version: str,
    personas: Mapping[DiscordBotSlot, str],
) -> Mapping[DiscordBotSlot, str]:
    validated: dict[DiscordBotSlot, str] = {}
    try:
        for slot, value in personas.items():
            payload = PersonaConfigPayload.model_validate_json(value)
            if payload.slot is not slot or payload.config_version != config_version:
                raise SetupError("saved_persona_config_mismatch")
            validated[slot] = value
    except TypeError, ValueError:
        raise SetupError("saved_persona_config_invalid") from None
    return MappingProxyType(validated)


def _multiline_secret(secret_reader: SecretReader, label: str) -> str:
    print(f"{label}を貼り付け、最後に単独の . を入力してください。")
    lines: list[str] = []
    while True:
        line = secret_reader("  > ")
        if line == ".":
            break
        lines.append("." if line == ".." else line)
    value = "\n".join(lines)
    if not value.strip():
        raise SetupError("private_value_missing")
    return value


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


def _validate_config_version(value: str) -> None:
    if CONFIG_VERSION_PATTERN.fullmatch(value) is None:
        raise SetupError("config_version_invalid")


def _print_status(configured: int, total: int, email_missing: bool) -> None:
    email_status = "未設定" if email_missing else "設定済み"
    print(f"GitHub通知メール: {email_status}")
    print(f"SSM SecureString: {configured}/{total} 設定済み")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="STEP-10の非公開設定を対話入力し、GitHubとSSMへ安全に登録する",
    )
    parser.add_argument("--check", action="store_true", help="秘密値を読まず設定数だけ確認")
    parser.add_argument("--config-version", default=DEFAULT_CONFIG_VERSION)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
