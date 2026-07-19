"""Tests for the disposable DynamoDB Local test runner."""

from __future__ import annotations

import pytest
from tools.run_dynamodb_local import (
    DEFAULT_TEST_COMMAND,
    DynamoDbLocalError,
    command_from_arguments,
    endpoint_from_port_output,
)


def test_endpoint_from_loopback_port_output() -> None:
    assert endpoint_from_port_output("127.0.0.1:49152\n") == "http://127.0.0.1:49152"


@pytest.mark.parametrize("output", ["0.0.0.0:8000", "[::1]:8000", "127.0.0.1:70000"])
def test_endpoint_rejects_non_loopback_or_invalid_port(output: str) -> None:
    with pytest.raises(DynamoDbLocalError):
        endpoint_from_port_output(output)


def test_command_defaults_to_the_full_locked_suite() -> None:
    assert command_from_arguments(()) == DEFAULT_TEST_COMMAND
    assert command_from_arguments(("--",)) == DEFAULT_TEST_COMMAND


def test_command_removes_the_argument_delimiter() -> None:
    assert command_from_arguments(("--", "uv", "run", "pytest", "-q")) == (
        "uv",
        "run",
        "pytest",
        "-q",
    )
