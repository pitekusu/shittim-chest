"""Strict Records Release manifest tests."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from tools.records_release_manifest import (
    create_change_set_plan,
    create_manifest,
    validate_change_set_safety,
    validate_manifest,
)

COMMIT_SHA = "a" * 40
BUNDLE_SHA = "b" * 64
WEB_SHA = "c" * 64
WEB_SBOM_SHA = "d" * 64
ACCOUNT = "000000000000"
RECORDS_PUBLIC_HOSTNAME = "shittim.pitekusu.dev"
EDGE_HOSTNAME = "shittim.example.com"
EDGE_ZONE_ID = "Z0123456789EXAMPLE"
EDGE_ZONE_NAME = "example.com"


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
        records_public_hostname=RECORDS_PUBLIC_HOSTNAME,
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


def resource_change(
    resource_type: str,
    *,
    action: str = "Modify",
    logical_id: str = "ExpectedResource1234",
    replacement: str | None = "False",
) -> dict[str, object]:
    change: dict[str, object] = {
        "Action": action,
        "LogicalResourceId": logical_id,
        "ResourceType": resource_type,
    }
    if replacement is not None:
        change["Replacement"] = replacement
    return {"ResourceChange": change}


def edge_alias_migration(logical_id: str, record_type: str) -> dict[str, object]:
    before_name = f"{EDGE_HOSTNAME}.{EDGE_ZONE_NAME}."
    after_name = f"{EDGE_HOSTNAME}."
    alias_target = {
        "HostedZoneId": "Z2FDTNDATAQYW2",
        "DNSName": "example.cloudfront.net",
    }
    construct_id = "Ipv4Alias" if record_type == "A" else "Ipv6Alias"
    return {
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": logical_id,
            "ResourceType": "AWS::Route53::RecordSet",
            "Replacement": "True",
            "Details": [
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "Name",
                        "RequiresRecreation": "Always",
                        "Path": "/Properties/Name",
                        "BeforeValue": before_name,
                        "AfterValue": after_name,
                        "AttributeChangeType": "Modify",
                    },
                    "Evaluation": "Static",
                    "ChangeSource": "DirectModification",
                }
            ],
            "BeforeContext": json.dumps(
                {
                    "Properties": {
                        "AliasTarget": alias_target,
                        "Type": record_type,
                        "HostedZoneId": EDGE_ZONE_ID,
                        "Name": before_name,
                    },
                    "Metadata": {"aws:cdk:path": f"RecordsEdge/{construct_id}/Resource"},
                }
            ),
            "AfterContext": json.dumps(
                {
                    "Properties": {
                        "AliasTarget": alias_target,
                        "Type": record_type,
                        "HostedZoneId": EDGE_ZONE_ID,
                        "Name": after_name,
                    },
                    "Metadata": {"aws:cdk:path": f"RecordsEdge/{construct_id}/Resource"},
                }
            ),
        }
    }


def edge_certificate_migration(*, explicit_before_algorithm: bool = False) -> dict[str, object]:
    properties = {
        "DomainName": {"Ref": "RecordsPublicHostname"},
        "ValidationMethod": "DNS",
        "Tags": [{"Key": "Project", "Value": "shittim-chest"}],
    }
    before_properties = dict(properties)
    if explicit_before_algorithm:
        before_properties["KeyAlgorithm"] = "RSA_2048"
    metadata = {"aws:cdk:path": "RecordsEdge/Certificate/Resource"}
    target = {
        "Attribute": "Properties",
        "Name": "KeyAlgorithm",
        "RequiresRecreation": "Always",
        "Path": "/Properties/KeyAlgorithm",
        "AfterValue": "EC_prime256v1",
        "AttributeChangeType": "Modify" if explicit_before_algorithm else "Add",
    }
    if explicit_before_algorithm:
        target["BeforeValue"] = "RSA_2048"
    return {
        "ResourceChange": {
            "Action": "Modify",
            "LogicalResourceId": "Certificate4E7ABB08",
            "ResourceType": "AWS::CertificateManager::Certificate",
            "Replacement": "True",
            "Details": [
                {
                    "Target": target,
                    "Evaluation": "Static",
                    "ChangeSource": "DirectModification",
                }
            ],
            "BeforeContext": json.dumps({"Properties": before_properties, "Metadata": metadata}),
            "AfterContext": json.dumps(
                {
                    "Properties": {**properties, "KeyAlgorithm": "EC_prime256v1"},
                    "Metadata": metadata,
                }
            ),
        }
    }


def test_change_set_safety_allows_only_expected_immutable_replacements() -> None:
    validate_change_set_safety(
        {
            "Changes": [
                resource_change("AWS::DynamoDB::Table", action="Add", replacement=None),
                resource_change("AWS::Lambda::Function"),
                resource_change("AWS::CDK::Metadata", replacement="Conditional"),
                resource_change("AWS::Lambda::Version", replacement="True"),
            ]
        },
        logical_name="application",
    )

    validate_change_set_safety(
        {
            "Changes": [
                edge_certificate_migration(),
                edge_alias_migration("Ipv4AliasF16765B0", "A"),
                edge_alias_migration("Ipv6AliasBCE03BB2", "AAAA"),
            ]
        },
        logical_name="edge",
        expected_edge_hostname=EDGE_HOSTNAME,
        expected_edge_zone_id=EDGE_ZONE_ID,
        expected_edge_zone_name=EDGE_ZONE_NAME,
    )

    validate_change_set_safety(
        {"Changes": [edge_certificate_migration(explicit_before_algorithm=True)]},
        logical_name="edge",
    )


def test_change_set_safety_rejects_widened_certificate_replacement() -> None:
    migration = edge_certificate_migration()
    change = migration["ResourceChange"]
    assert isinstance(change, dict)
    after = json.loads(str(change["AfterContext"]))
    after["Properties"]["DomainName"] = "other.example.com"
    change["AfterContext"] = json.dumps(after)

    with pytest.raises(ValueError, match="safety rejected"):
        validate_change_set_safety({"Changes": [migration]}, logical_name="edge")


@pytest.mark.parametrize(
    ("explicit_before_algorithm", "attribute_change_type"),
    ((False, "Modify"), (True, "Add")),
)
def test_change_set_safety_rejects_certificate_change_type_context_mismatch(
    explicit_before_algorithm: bool,
    attribute_change_type: str,
) -> None:
    migration = edge_certificate_migration(explicit_before_algorithm=explicit_before_algorithm)
    change = migration["ResourceChange"]
    assert isinstance(change, dict)
    details = change["Details"]
    assert isinstance(details, list)
    target = details[0]["Target"]
    assert isinstance(target, dict)
    target["AttributeChangeType"] = attribute_change_type

    with pytest.raises(ValueError, match="safety rejected"):
        validate_change_set_safety({"Changes": [migration]}, logical_name="edge")


def test_change_set_safety_rejects_future_or_widened_alias_replacements() -> None:
    future = edge_alias_migration("Ipv4AliasF16765B0", "A")
    future_change = future["ResourceChange"]
    assert isinstance(future_change, dict)
    future_detail = future_change["Details"]
    assert isinstance(future_detail, list)
    target = future_detail[0]["Target"]
    assert isinstance(target, dict)
    target["BeforeValue"] = f"{EDGE_HOSTNAME}."
    before = json.loads(str(future_change["BeforeContext"]))
    before["Properties"]["Name"] = f"{EDGE_HOSTNAME}."
    future_change["BeforeContext"] = json.dumps(before)

    widened = edge_alias_migration("Ipv6AliasBCE03BB2", "AAAA")
    widened_change = widened["ResourceChange"]
    assert isinstance(widened_change, dict)
    after = json.loads(str(widened_change["AfterContext"]))
    after["Properties"]["AliasTarget"]["DNSName"] = "other.cloudfront.net"
    widened_change["AfterContext"] = json.dumps(after)

    for change in (future, widened):
        with pytest.raises(ValueError, match="safety rejected"):
            validate_change_set_safety(
                {"Changes": [change]},
                logical_name="edge",
                expected_edge_hostname=EDGE_HOSTNAME,
                expected_edge_zone_id=EDGE_ZONE_ID,
                expected_edge_zone_name=EDGE_ZONE_NAME,
            )


@pytest.mark.parametrize(
    ("logical_name", "change"),
    (
        ("stateful", resource_change("AWS::DynamoDB::Table", action="Remove")),
        ("application", resource_change("AWS::Lambda::Function", replacement="True")),
        ("application", resource_change("AWS::Lambda::Version", action="Remove")),
        ("edge", resource_change("AWS::Lambda::Version", replacement="True")),
        (
            "edge",
            resource_change(
                "AWS::Route53::RecordSet",
                logical_id="UnexpectedAlias1234",
                replacement="True",
            ),
        ),
        (
            "edge",
            resource_change(
                "AWS::Route53::RecordSet",
                logical_id="Ipv4AliasF16765B0",
                action="Remove",
                replacement="False",
            ),
        ),
        ("application", resource_change("AWS::CDK::Metadata", replacement="True")),
    ),
)
def test_change_set_safety_rejects_other_removals_and_replacements(
    logical_name: str,
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="safety rejected"):
        validate_change_set_safety({"Changes": [change]}, logical_name=logical_name)


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

    assert value["schema_version"] == 4
    assert value["records_public_hostname"] == RECORDS_PUBLIC_HOSTNAME
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
        (("schema_version",), 3),
        (("commit_sha",), "c" * 40),
        (("records_public_hostname",), "HTTPS://shittim.example.com"),
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


@pytest.mark.parametrize(
    "hostname",
    (
        "localhost",
        ".example.com",
        "example..com",
        f"{'a' * 64}.example.com",
        f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}",
    ),
)
def test_manifest_rejects_invalid_public_hostname(hostname: str) -> None:
    value = manifest()
    value["records_public_hostname"] = hostname

    with pytest.raises(ValueError, match="public hostname"):
        validate_manifest(value, expected_commit_sha=COMMIT_SHA)


def test_manifest_rejects_valid_hostname_that_does_not_match_upload_cors() -> None:
    value = manifest()
    value["records_public_hostname"] = "records.example.com"

    with pytest.raises(ValueError, match="Memorial upload CORS origin"):
        validate_manifest(value, expected_commit_sha=COMMIT_SHA)

    with pytest.raises(ValueError, match="Memorial upload CORS origin"):
        create_manifest(
            stateful_plan=plan("stateful"),
            application_plan=plan("application", "CREATE"),
            edge_plan=plan("edge", "CREATE"),
            commit_sha=COMMIT_SHA,
            records_public_hostname="records.example.com",
            bundle_sha256=BUNDLE_SHA,
            web_artifact_sha256=WEB_SHA,
            web_sbom_sha256=WEB_SBOM_SHA,
        )
