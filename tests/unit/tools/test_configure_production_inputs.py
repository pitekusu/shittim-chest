"""Tests for the one-command production private-input setup."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.stub import Stubber
from mypy_boto3_ssm.client import SSMClient
from tools import configure_production_inputs as setup

from shittim_chest.application import DiscordBotSlot

VALID_EMAIL = "operator" + "@" + "example.invalid"
SAVED_PERSONAS = """\
# Private operator configuration

## PersonaConfig v0002

| slot | display name | role | SSM path |
|---|---|---|---|
| `moderator` | Moderator | Coordinate without voting | `$ROOT/moderator` |
| `participant-a` | Alpha | Practical view | `$ROOT/participant-a` |
| `participant-b` | Bravo | Verification view | `$ROOT/participant-b` |
| `participant-c` | Charlie | Alternative view | `$ROOT/participant-c` |

### participant-a: Alpha

```text
Alpha prompt line 1.
Alpha prompt line 2.
```

### participant-b: Bravo

```text
Bravo prompt.
```

### participant-c: Charlie

```text
Charlie prompt.
```

## Next section
""".replace("$ROOT", f"{setup.PARAMETER_ROOT}/personas/v0002")


def client() -> SSMClient:
    return boto3.client(
        "ssm",
        region_name=setup.AWS_REGION,
        config=Config(signature_version=UNSIGNED),
    )


def secret_reader(values: list[str]) -> setup.SecretReader:
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


def complete_secret_input() -> list[str]:
    return [
        VALID_EMAIL,
        "101",
        "102,103",
        "104",
        "105",
        "106",
        "107",
        "ab" * 32,
        "moderator-token",
        "participant-a-token",
        "participant-b-token",
        "participant-c-token",
        "openai-key",
        "Moderator",
        "Moderator prompt",
        ".",
        "Participant A",
        "Participant A prompt",
        ".",
        "Participant B",
        "Participant B prompt",
        ".",
        "Participant C",
        "Participant C prompt",
        ".",
    ]


def test_parameter_names_are_the_exact_eleven_secure_string_paths() -> None:
    assert setup.parameter_names("v0001") == (
        "/shittim-chest/production/openai/api-key",
        "/shittim-chest/production/discord/moderator/public-key",
        "/shittim-chest/production/discord/moderator/token",
        "/shittim-chest/production/discord/participant-a/token",
        "/shittim-chest/production/discord/participant-b/token",
        "/shittim-chest/production/discord/participant-c/token",
        "/shittim-chest/production/runtime/v0001",
        "/shittim-chest/production/personas/v0001/moderator",
        "/shittim-chest/production/personas/v0001/participant-a",
        "/shittim-chest/production/personas/v0001/participant-b",
        "/shittim-chest/production/personas/v0001/participant-c",
    )


def test_collects_and_validates_every_missing_value_without_repr_leak() -> None:
    names = frozenset(setup.parameter_names("v0001"))
    pending = setup.collect_pending_setup(
        config_version="v0001",
        missing_parameters=names,
        github_email_missing=True,
        secret_reader=secret_reader(complete_secret_input()),
    )

    assert pending.github_email == VALID_EMAIL
    assert set(pending.parameters) == names
    assert repr(pending) == "PendingSetup()"

    runtime = json.loads(pending.parameters["/shittim-chest/production/runtime/v0001"])
    assert runtime == {
        "schema_version": "1",
        "config_version": "v0001",
        "guild_id": "101",
        "allowed_channel_ids": ["102", "103"],
        "identities": [
            {"slot": slot.value, "application_id": str(100 + index)}
            for index, slot in enumerate(DiscordBotSlot, start=4)
        ],
    }
    moderator = json.loads(pending.parameters["/shittim-chest/production/personas/v0001/moderator"])
    assert moderator["display_name"] == "Moderator"
    assert moderator["system_prompt"] == "Moderator prompt"


def test_collects_only_missing_values_and_does_not_require_existing_secrets() -> None:
    missing = frozenset({"/shittim-chest/production/openai/api-key"})

    pending = setup.collect_pending_setup(
        config_version="v0001",
        missing_parameters=missing,
        github_email_missing=False,
        secret_reader=secret_reader(["openai-key"]),
    )

    assert pending.github_email is None
    assert pending.parameters == {"/shittim-chest/production/openai/api-key": "openai-key"}


def test_loads_saved_v0002_personas_without_exposing_source_values(tmp_path: Path) -> None:
    source = tmp_path / "private.md"
    source.write_text(SAVED_PERSONAS, encoding="utf-8")

    personas = setup.load_saved_personas(source, "v0002")

    assert set(personas) == set(DiscordBotSlot)
    moderator = json.loads(personas[DiscordBotSlot.MODERATOR])
    assert moderator == {
        "schema_version": "1",
        "config_version": "v0002",
        "slot": "moderator",
        "display_name": "Moderator",
        "system_prompt": "Coordinate without voting",
    }
    participant = json.loads(personas[DiscordBotSlot.PARTICIPANT_A])
    assert participant["display_name"] == "Alpha"
    assert participant["system_prompt"] == "Alpha prompt line 1.\nAlpha prompt line 2."


def test_saved_personas_replace_all_persona_prompts() -> None:
    saved = setup._personas_from_operator_markdown(SAVED_PERSONAS, "v0002")
    missing = frozenset(
        f"{setup.PARAMETER_ROOT}/personas/v0002/{slot.value}" for slot in DiscordBotSlot
    )

    def unexpected_prompt(_prompt: str) -> str:
        raise AssertionError("saved personas must not be prompted again")

    pending = setup.collect_pending_setup(
        config_version="v0002",
        missing_parameters=missing,
        github_email_missing=False,
        secret_reader=unexpected_prompt,
        saved_personas=saved,
    )

    assert set(pending.parameters) == set(missing)


def test_private_source_pointer_is_local_only_and_absolute(tmp_path: Path) -> None:
    source = tmp_path / "private.md"
    source.write_text(SAVED_PERSONAS, encoding="utf-8")
    pointer = tmp_path / ".env.private-config"
    pointer.write_text(
        f"{setup.PRIVATE_SOURCE_ENV}={source}\n",
        encoding="utf-8",
    )

    assert setup.private_config_source({}, pointer) == source


def test_invalid_saved_persona_source_fails_with_content_free_code(tmp_path: Path) -> None:
    source = tmp_path / "private.md"
    source.write_text(
        SAVED_PERSONAS.replace("PersonaConfig v0002", "PersonaConfig v0003"), encoding="utf-8"
    )

    with pytest.raises(setup.SetupError) as caught:
        setup.load_saved_personas(source, "v0002")

    assert caught.value.code == "saved_persona_version_missing"
    assert "Alpha prompt" not in str(caught.value)


def test_parser_defaults_to_saved_persona_version() -> None:
    assert setup._parser().parse_args([]).config_version == "v0002"


@pytest.mark.parametrize(
    ("values", "code"),
    [
        (["not-an-email"], "operator_email_invalid"),
        ([VALID_EMAIL, "invalid-key"], "discord_public_key_invalid"),
    ],
)
def test_invalid_or_incomplete_input_fails_with_a_content_free_code(
    values: list[str],
    code: str,
) -> None:
    missing = frozenset()
    if len(values) > 1:
        missing = frozenset({"/shittim-chest/production/discord/moderator/public-key"})
    with pytest.raises(setup.SetupError) as caught:
        setup.collect_pending_setup(
            config_version="v0001",
            missing_parameters=missing,
            github_email_missing=True,
            secret_reader=secret_reader(values),
        )
    assert caught.value.code == code


def test_existing_parameter_scan_reads_metadata_only() -> None:
    sdk = client()
    target = "/shittim-chest/production/openai/api-key"
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "describe_parameters",
            {
                "Parameters": [
                    {
                        "Name": target,
                        "Type": "SecureString",
                        "Version": 1,
                        "Tier": "Standard",
                    },
                    {
                        "Name": "/unrelated/value",
                        "Type": "String",
                        "Version": 1,
                        "Tier": "Standard",
                    },
                ]
            },
            {
                "ParameterFilters": [
                    {
                        "Key": "Path",
                        "Option": "Recursive",
                        "Values": [setup.PARAMETER_ROOT],
                    }
                ]
            },
        )

        assert setup.existing_parameters(sdk, frozenset({target})) == frozenset({target})
        stubber.assert_no_pending_responses()


def test_put_parameters_creates_standard_secure_string_without_overwrite() -> None:
    sdk = client()
    name = "/shittim-chest/production/openai/api-key"
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "put_parameter",
            {"Version": 1, "Tier": "Standard"},
            {
                "Name": name,
                "Value": "private-value",
                "Description": "The Shittim Chest private production input",
                "Type": "SecureString",
                "Tier": "Standard",
                "Overwrite": False,
                "Tags": [
                    {"Key": "Project", "Value": "shittim-chest"},
                    {"Key": "Environment", "Value": "production"},
                ],
            },
        )

        setup.put_parameters(sdk, {name: "private-value"})
        stubber.assert_no_pending_responses()


def test_github_secret_is_sent_over_stdin_not_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(setup.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(setup.subprocess, "run", run)

    setup.set_github_secret(setup.GITHUB_REPOSITORY, VALID_EMAIL)

    command = captured["command"]
    assert isinstance(command, list)
    assert VALID_EMAIL not in command
    assert captured["input"] == VALID_EMAIL.encode()


def test_target_account_is_bound_to_the_configured_release_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = "1" * 12

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["variable", "get"]:
            value = f"arn:aws:iam::{account}:role/ShittimChest-Prod-GitHub-ReleasePlan\n"
        else:
            value = f"{account}\n"
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup.subprocess, "run", run)

    setup.require_target_account(setup.GITHUB_REPOSITORY)


def test_target_account_mismatch_fails_without_exposing_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = iter(("1" * 12, "2" * 12))

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        account = next(accounts)
        if command[1:3] == ["variable", "get"]:
            value = f"arn:aws:iam::{account}:role/ShittimChest-Prod-GitHub-ReleasePlan"
        else:
            value = account
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr="")

    monkeypatch.setattr(setup.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup.subprocess, "run", run)

    with pytest.raises(setup.SetupError) as caught:
        setup.require_target_account(setup.GITHUB_REPOSITORY)

    assert caught.value.code == "production_account_mismatch"
    assert "1" * 12 not in str(caught.value)
    assert "2" * 12 not in str(caught.value)
