"""Strict Records Release manifest tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from tools.records_release_manifest import (
    create_change_set_plan,
    create_manifest,
    validate_manifest,
)

COMMIT_SHA = "a" * 40
BUNDLE_SHA = "b" * 64
WEB_SHA = "c" * 64
WEB_SBOM_SHA = "d" * 64
ACCOUNT = "000000000000"


def described(
    logical_name: str,
    *,
    status: str = "CREATE_COMPLETE",
    execution_status: str = "AVAILABLE",
    reason: str | None = None,
) -> dict[str, object]:
    name = f"records-release-123-1-{logical_name}"
    stacks = {
        "stateful": "ShittimChest-Prod-RecordsStateful",
        "application": "ShittimChest-Prod-RecordsApplication",
        "edge": "ShittimChest-Prod-RecordsEdge",
    }
    stack = stacks[logical_name]
    region = "us-east-1" if logical_name == "edge" else "ap-northeast-1"
    result: dict[str, object] = {
        "StackName": stack,
        "ChangeSetName": name,
        "ChangeSetId": (
            f"arn:aws:cloudformation:{region}:{ACCOUNT}:"
            f"changeSet/{name}/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ),
        "Status": status,
        "ExecutionStatus": execution_status,
    }
    if reason is not None:
        result["StatusReason"] = reason
    return result


def plan(logical_name: str, change_set_type: str = "UPDATE") -> dict[str, object]:
    record = described(logical_name)
    return create_change_set_plan(
        record,
        change_set_type=change_set_type,
        expected_name=str(record["ChangeSetName"]),
        expected_region="us-east-1" if logical_name == "edge" else "ap-northeast-1",
        expected_stack=str(record["StackName"]),
    )


def manifest() -> dict[str, object]:
    return create_manifest(
        stateful_plan=plan("stateful"),
        application_plan=plan("application", "CREATE"),
        edge_plan=plan("edge", "CREATE"),
        commit_sha=COMMIT_SHA,
        bundle_sha256=BUNDLE_SHA,
        web_artifact_sha256=WEB_SHA,
        web_sbom_sha256=WEB_SBOM_SHA,
    )


def test_change_set_plan_attests_create_or_update_and_execution_decision() -> None:
    stateful = plan("stateful")
    application = plan("application", "CREATE")
    edge = plan("edge", "CREATE")

    assert stateful["type"] == "UPDATE"
    assert stateful["executable"] is True
    assert application["type"] == "CREATE"
    assert application["executable"] is True
    assert edge["region"] == "us-east-1"


def test_unchanged_update_is_a_normal_non_executable_plan() -> None:
    record = described(
        "stateful",
        status="FAILED",
        execution_status="UNAVAILABLE",
        reason="The submitted information didn't contain changes.",
    )

    result = create_change_set_plan(
        record,
        change_set_type="UPDATE",
        expected_name=str(record["ChangeSetName"]),
        expected_region="ap-northeast-1",
        expected_stack=str(record["StackName"]),
    )

    assert result["executable"] is False


@pytest.mark.parametrize(
    ("status", "execution_status", "reason"),
    (
        ("FAILED", "UNAVAILABLE", "Access denied"),
        ("CREATE_COMPLETE", "UNAVAILABLE", None),
        ("CREATE_PENDING", "UNAVAILABLE", None),
    ),
)
def test_change_set_plan_rejects_failures_and_incomplete_states(
    status: str,
    execution_status: str,
    reason: str | None,
) -> None:
    record = described(
        "stateful",
        status=status,
        execution_status=execution_status,
        reason=reason,
    )

    with pytest.raises(ValueError):
        create_change_set_plan(
            record,
            change_set_type="UPDATE",
            expected_name=str(record["ChangeSetName"]),
            expected_region="ap-northeast-1",
            expected_stack=str(record["StackName"]),
        )


def test_manifest_binds_fixed_sha_stack_name_type_and_execution() -> None:
    value = manifest()

    validate_manifest(value, expected_commit_sha=COMMIT_SHA)

    assert value["schema_version"] == 3
    assert value["web_artifact_sha256"] == WEB_SHA
    assert value["web_sbom_sha256"] == WEB_SBOM_SHA
    assert value["change_sets"] == {
        "stateful": plan("stateful"),
        "application": plan("application", "CREATE"),
        "edge": plan("edge", "CREATE"),
    }


@pytest.mark.parametrize(
    ("path", "replacement"),
    (
        (("commit_sha",), "c" * 40),
        (("web_sbom_sha256",), "not-a-hash"),
        (("change_sets", "stateful", "stack"), "WrongStack"),
        (("change_sets", "stateful", "name"), "records-release-123-1-application"),
        (("change_sets", "stateful", "type"), "REPLACE"),
        (("change_sets", "edge", "region"), "ap-northeast-1"),
        (("change_sets", "application", "executable"), False),
    ),
)
def test_manifest_rejects_tampered_execution_contract(
    path: tuple[str, ...],
    replacement: object,
) -> None:
    value = deepcopy(manifest())
    target: Any = value
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = replacement

    with pytest.raises(ValueError):
        validate_manifest(value, expected_commit_sha=COMMIT_SHA)
