#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run a command against an isolated, digest-pinned DynamoDB Local container."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from mypy_boto3_dynamodb.client import DynamoDBClient

DYNAMODB_LOCAL_IMAGE: Final = (
    "amazon/dynamodb-local@sha256:d89f8fcc6b1a39cb35976c248ed42a28c66ae00dc043099210f5571e42648ab4"
)
DEFAULT_TEST_COMMAND: Final = ("uv", "run", "--frozen", "pytest")
PORT_PATTERN: Final = re.compile(r"^127\.0\.0\.1:(?P<port>[1-9][0-9]{0,4})$")


class DynamoDbLocalError(RuntimeError):
    """Raised when the local DynamoDB test service cannot be used safely."""


@dataclass(frozen=True, slots=True)
class StartedDynamoDbLocal:
    """The container identity and loopback-only endpoint for one test run."""

    container_cli: str
    container_name: str
    endpoint_url: str


def parse_args() -> argparse.Namespace:
    """Parse a container runtime selection and an optional test command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--container-cli",
        help="container CLI to use; defaults to podman, then docker",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=30,
        help="maximum DynamoDB Local readiness wait (default: 30)",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to run after --; defaults to `uv run --frozen pytest`",
    )
    return parser.parse_args()


def resolve_container_cli(explicit_cli: str | None) -> str:
    """Return an available container CLI without assuming Docker compatibility."""

    candidates = (explicit_cli,) if explicit_cli is not None else ("podman", "docker")
    for candidate in candidates:
        if candidate is not None and shutil.which(candidate) is not None:
            return candidate
    if explicit_cli is not None:
        raise DynamoDbLocalError(f"requested container CLI is unavailable: {explicit_cli}")
    raise DynamoDbLocalError("Podman or Docker is required to run DynamoDB Local")


def _run(
    arguments: Sequence[str],
    *,
    check: bool = True,
    timeout_seconds: int = 30,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - no shell; caller selects the local test command.
            tuple(arguments),
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        command = arguments[0] if arguments else "container command"
        raise DynamoDbLocalError(f"failed to run {command}") from error


def _container_name() -> str:
    return f"shittim-dynamodb-local-{os.getpid()}"


def endpoint_from_port_output(output: str) -> str:
    """Turn a loopback-only container port mapping into an endpoint URL."""

    match = PORT_PATTERN.fullmatch(output.strip())
    if match is None:
        raise DynamoDbLocalError("DynamoDB Local did not expose a loopback-only port")
    port = int(match.group("port"))
    if port > 65535:
        raise DynamoDbLocalError("DynamoDB Local exposed an invalid port")
    return f"http://127.0.0.1:{port}"


def start_dynamodb_local(container_cli: str) -> StartedDynamoDbLocal:
    """Start a disposable in-memory local service on an automatically chosen port."""

    name = _container_name()
    result = _run(
        (
            container_cli,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            "127.0.0.1::8000",
            DYNAMODB_LOCAL_IMAGE,
            "-jar",
            "DynamoDBLocal.jar",
            "-inMemory",
            "-sharedDb",
            "-disableTelemetry",
        )
    )
    if not result.stdout.strip():
        raise DynamoDbLocalError("DynamoDB Local did not return a container ID")
    try:
        endpoint_url = endpoint_from_port_output(
            _run((container_cli, "port", name, "8000/tcp")).stdout
        )
    except DynamoDbLocalError:
        _run((container_cli, "stop", "--time", "10", name), check=False)
        raise
    return StartedDynamoDbLocal(
        container_cli=container_cli,
        container_name=name,
        endpoint_url=endpoint_url,
    )


def wait_until_ready(endpoint_url: str, timeout_seconds: int) -> None:
    """Wait for a successful signed DynamoDB API call, not merely an open TCP port."""

    if timeout_seconds <= 0:
        raise DynamoDbLocalError("timeout must be positive")
    client: DynamoDBClient = boto3.client(
        "dynamodb",
        region_name="ap-northeast-1",
        endpoint_url=endpoint_url,
        aws_access_key_id="local",
        aws_secret_access_key="local",  # noqa: S106 - Local accepts dummy credentials.
        config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0}),
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            client.list_tables(Limit=1)
            return
        except (BotoCoreError, ClientError, OSError) as error:
            last_error = error
            time.sleep(0.1)
    raise DynamoDbLocalError(f"DynamoDB Local did not become ready: {last_error}")


def _container_logs(service: StartedDynamoDbLocal) -> str:
    result = _run(
        (service.container_cli, "logs", service.container_name),
        check=False,
    )
    return result.stdout.strip() or result.stderr.strip()


def stop_dynamodb_local(service: StartedDynamoDbLocal) -> None:
    """Stop the unique disposable container; --rm removes it after shutdown."""

    _run(
        (service.container_cli, "stop", "--time", "10", service.container_name),
        check=False,
    )


def command_from_arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    """Use the full suite by default and reject an empty explicit delimiter."""

    command = tuple(argument for argument in arguments if argument != "--")
    if not command:
        return DEFAULT_TEST_COMMAND
    return command


def run_with_dynamodb_local(
    *,
    container_cli: str,
    command: Sequence[str],
    timeout_seconds: int,
) -> int:
    """Start the service, run one child command with its endpoint, then clean up."""

    service: StartedDynamoDbLocal | None = None
    try:
        service = start_dynamodb_local(container_cli)
        try:
            wait_until_ready(service.endpoint_url, timeout_seconds)
        except DynamoDbLocalError as error:
            logs = _container_logs(service)
            detail = f"\nDynamoDB Local logs:\n{logs}" if logs else ""
            raise DynamoDbLocalError(f"{error}{detail}") from error
        environment = os.environ.copy()
        environment["DYNAMODB_ENDPOINT_URL"] = service.endpoint_url
        result = _run(tuple(command), check=False, environment=environment, timeout_seconds=600)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    finally:
        if service is not None:
            stop_dynamodb_local(service)


def main() -> int:
    """Run the selected command with one isolated DynamoDB Local instance."""

    arguments = parse_args()
    try:
        container_cli = resolve_container_cli(arguments.container_cli)
        command = command_from_arguments(arguments.command)
        return run_with_dynamodb_local(
            container_cli=container_cli,
            command=command,
            timeout_seconds=arguments.timeout_seconds,
        )
    except DynamoDbLocalError as error:
        print(f"DynamoDB Local test runner failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
