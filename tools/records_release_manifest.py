"""Create and validate the immutable Records Release plan manifest."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_CHANGE_SET_NAME = re.compile(
    r"records-release-[1-9][0-9]*-[1-9][0-9]*-(stateful|application|edge)"
)
_CHANGE_SET_ARN = re.compile(
    r"arn:[a-z0-9-]+:cloudformation:(ap-northeast-1|us-east-1):[0-9]{12}:"
    r"changeSet/[A-Za-z0-9][-A-Za-z0-9]*/[0-9a-f-]{36}"
)
_STACKS = {
    "stateful": ("ShittimChest-Prod-RecordsStateful", "stateful", "ap-northeast-1"),
    "application": ("ShittimChest-Prod-RecordsApplication", "application", "ap-northeast-1"),
    "edge": ("ShittimChest-Prod-RecordsEdge", "edge", "us-east-1"),
}
_EDGE_ALIAS_TYPES = {
    "Ipv4AliasF16765B0": "A",
    "Ipv6AliasBCE03BB2": "AAAA",
}
_EDGE_CERTIFICATE_LOGICAL_ID = "Certificate4E7ABB08"


def create_change_set_plan(
    value: object,
    *,
    change_set_type: str,
    expected_name: str,
    expected_region: str,
    expected_stack: str,
) -> dict[str, object]:
    """Normalize one described Change Set into its attested execution plan."""

    record = _object(value, "described Change Set")
    if change_set_type not in {"CREATE", "UPDATE"}:
        raise ValueError("Records Change Set type is invalid")
    if record.get("StackName") != expected_stack:
        raise ValueError("Records Change Set stack is invalid")
    if record.get("ChangeSetName") != expected_name:
        raise ValueError("Records Change Set name is invalid")
    arn = record.get("ChangeSetId")
    if not isinstance(arn, str) or _CHANGE_SET_ARN.fullmatch(arn) is None:
        raise ValueError("Records Change Set ARN is invalid")
    if f"changeSet/{expected_name}/" not in arn:
        raise ValueError("Records Change Set ARN does not bind its name")
    if f":cloudformation:{expected_region}:" not in arn:
        raise ValueError("Records Change Set ARN does not bind its region")

    status = record.get("Status")
    execution_status = record.get("ExecutionStatus")
    if status == "CREATE_COMPLETE" and execution_status == "AVAILABLE":
        executable = True
    elif status == "FAILED" and execution_status == "UNAVAILABLE":
        reason = record.get("StatusReason")
        if not isinstance(reason, str) or "didn't contain changes" not in reason:
            raise ValueError("Records Change Set failed for a reason other than no changes")
        if change_set_type != "UPDATE":
            raise ValueError("a CREATE Change Set cannot be a no-op")
        executable = False
    else:
        raise ValueError("Records Change Set is not in an attested terminal state")

    return {
        "stack": expected_stack,
        "name": expected_name,
        "arn": arn,
        "region": expected_region,
        "type": change_set_type,
        "executable": executable,
    }


def validate_change_set_safety(
    value: object,
    *,
    logical_name: str,
    expected_edge_hostname: str | None = None,
    expected_edge_zone_id: str | None = None,
    expected_edge_zone_name: str | None = None,
) -> None:
    """Reject removals and replacements outside explicitly permitted resources."""

    if logical_name not in _STACKS:
        raise ValueError("Records Change Set logical name is invalid")
    record = _object(value, "described Change Set")
    changes = record.get("Changes")
    if not isinstance(changes, list):
        raise ValueError("Records Change Set changes must be an array")
    invalid: list[dict[str, object]] = []
    for item_value in changes:
        item = _object(item_value, "Records Change Set change")
        change = _object(item.get("ResourceChange"), "Records resource change")
        resource_type = change.get("ResourceType")
        logical_id = change.get("LogicalResourceId")
        action = change.get("Action")
        replacement = change.get("Replacement", "False")
        if not all(
            isinstance(value, str) and value for value in (resource_type, logical_id, action)
        ):
            raise ValueError("Records resource change identity is invalid")
        if replacement not in {"False", "Conditional", "True"}:
            raise ValueError("Records resource change replacement is invalid")

        if resource_type == "AWS::CDK::Metadata":
            safe = (action == "Add" and replacement == "False") or (
                action == "Modify" and replacement in {"False", "Conditional"}
            )
        elif logical_name == "application" and resource_type == "AWS::Lambda::Version":
            safe = (action != "Remove" and replacement == "False") or (
                action == "Modify" and replacement == "True"
            )
        elif (
            logical_name == "edge"
            and resource_type == "AWS::CertificateManager::Certificate"
            and logical_id == _EDGE_CERTIFICATE_LOGICAL_ID
        ):
            safe = _is_expected_edge_certificate_migration(change)
        elif (
            logical_name == "edge"
            and resource_type == "AWS::Route53::RecordSet"
            and logical_id in _EDGE_ALIAS_TYPES
        ):
            safe = _is_expected_edge_alias_migration(
                change,
                expected_hostname=expected_edge_hostname,
                expected_record_type=_EDGE_ALIAS_TYPES[logical_id],
                expected_zone_id=expected_edge_zone_id,
                expected_zone_name=expected_edge_zone_name,
            )
        else:
            safe = action != "Remove" and replacement == "False"
        if not safe:
            invalid.append(
                {
                    "logical_resource_id": logical_id,
                    "resource_type": resource_type,
                    "action": action,
                    "replacement": replacement,
                }
            )
    if invalid:
        raise ValueError(
            "Records Change Set safety rejected: "
            + json.dumps(invalid, ensure_ascii=True, separators=(",", ":"))
        )


def _is_expected_edge_certificate_migration(change: Mapping[str, object]) -> bool:
    """Allow only the one-time RSA 2048 to ECDSA P-256 certificate replacement."""

    details = change.get("Details")
    if (
        change.get("Action") != "Modify"
        or change.get("Replacement") != "True"
        or not isinstance(details, list)
        or len(details) != 1
    ):
        return False
    detail = details[0]
    if not isinstance(detail, dict) or set(detail) != {
        "Target",
        "Evaluation",
        "ChangeSource",
    }:
        return False
    target = detail.get("Target")
    if not isinstance(target, dict):
        return False
    required_target = {
        "Attribute": "Properties",
        "Name": "KeyAlgorithm",
        "RequiresRecreation": "Always",
        "Path": "/Properties/KeyAlgorithm",
        "AfterValue": "EC_prime256v1",
        "AttributeChangeType": "Modify",
    }
    if any(target.get(key) != value for key, value in required_target.items()):
        return False
    if set(target) - (set(required_target) | {"BeforeValue"}):
        return False
    if target.get("BeforeValue") not in (None, "RSA_2048"):
        return False
    if detail["Evaluation"] != "Static" or detail["ChangeSource"] != "DirectModification":
        return False

    before = _json_object(change.get("BeforeContext"))
    after = _json_object(change.get("AfterContext"))
    if before is None or after is None or set(before) != {"Properties", "Metadata"}:
        return False
    if set(after) != {"Properties", "Metadata"} or before["Metadata"] != after["Metadata"]:
        return False
    before_properties = before.get("Properties")
    after_properties = after.get("Properties")
    if not isinstance(before_properties, dict) or not isinstance(after_properties, dict):
        return False
    if after_properties.get("KeyAlgorithm") != "EC_prime256v1":
        return False
    if before_properties.get("KeyAlgorithm", "RSA_2048") != "RSA_2048":
        return False
    before_without_algorithm = dict(before_properties)
    after_without_algorithm = dict(after_properties)
    before_without_algorithm.pop("KeyAlgorithm", None)
    after_without_algorithm.pop("KeyAlgorithm", None)
    return before_without_algorithm == after_without_algorithm


def _is_expected_edge_alias_migration(
    change: Mapping[str, object],
    *,
    expected_hostname: str | None,
    expected_record_type: str,
    expected_zone_id: str | None,
    expected_zone_name: str | None,
) -> bool:
    """Allow only the one-time duplicated-name to absolute-FQDN migration."""

    if not isinstance(expected_hostname, str) or not expected_hostname:
        return False
    if not isinstance(expected_zone_id, str) or not expected_zone_id:
        return False
    if not isinstance(expected_zone_name, str) or not expected_zone_name:
        return False
    if expected_hostname.endswith("."):
        return False
    zone_name = expected_zone_name.removesuffix(".")
    if not zone_name or expected_hostname == zone_name:
        return False
    before_name = f"{expected_hostname}.{zone_name}."
    after_name = f"{expected_hostname}."
    expected_detail = {
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
    if (
        change.get("Action") != "Modify"
        or change.get("Replacement") != "True"
        or change.get("Details") != [expected_detail]
    ):
        return False
    before = _json_object(change.get("BeforeContext"))
    after = _json_object(change.get("AfterContext"))
    if before is None or after is None or set(before) != {"Properties", "Metadata"}:
        return False
    if set(after) != {"Properties", "Metadata"} or before["Metadata"] != after["Metadata"]:
        return False
    before_properties = before.get("Properties")
    after_properties = after.get("Properties")
    if not isinstance(before_properties, dict) or not isinstance(after_properties, dict):
        return False
    required_properties = {"AliasTarget", "Type", "HostedZoneId", "Name"}
    if (
        set(before_properties) != required_properties
        or set(after_properties) != required_properties
    ):
        return False
    if (
        before_properties.get("Type") != expected_record_type
        or after_properties.get("Type") != expected_record_type
        or before_properties.get("HostedZoneId") != expected_zone_id
        or after_properties.get("HostedZoneId") != expected_zone_id
        or before_properties.get("Name") != before_name
        or after_properties.get("Name") != after_name
    ):
        return False
    before_without_name = dict(before_properties)
    after_without_name = dict(after_properties)
    before_without_name.pop("Name")
    after_without_name.pop("Name")
    return before_without_name == after_without_name


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except TypeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def create_manifest(
    *,
    application_plan: object,
    bundle_sha256: str,
    commit_sha: str,
    edge_plan: object,
    stateful_plan: object,
    web_artifact_sha256: str,
    web_sbom_sha256: str,
) -> dict[str, object]:
    """Bind the fixed commit, Lambda/Web evidence, and three normalized plans."""

    manifest = {
        "schema_version": 3,
        "commit_sha": commit_sha,
        "bundle_sha256": bundle_sha256,
        "web_artifact_sha256": web_artifact_sha256,
        "web_sbom_sha256": web_sbom_sha256,
        "change_sets": {
            "stateful": stateful_plan,
            "application": application_plan,
            "edge": edge_plan,
        },
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: object, *, expected_commit_sha: str | None = None) -> None:
    """Reject extra fields or an altered stack, identity, type, or execution decision."""

    root = _object(value, "Records Release manifest")
    if set(root) != {
        "schema_version",
        "commit_sha",
        "bundle_sha256",
        "web_artifact_sha256",
        "web_sbom_sha256",
        "change_sets",
    }:
        raise ValueError("Records Release manifest fields are invalid")
    if root.get("schema_version") != 3:
        raise ValueError("Records Release manifest schema is invalid")
    commit_sha = root.get("commit_sha")
    if not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None:
        raise ValueError("Records Release manifest commit is invalid")
    if expected_commit_sha is not None and commit_sha != expected_commit_sha:
        raise ValueError("Records Release manifest commit does not match the workflow")
    bundle_sha256 = root.get("bundle_sha256")
    if not isinstance(bundle_sha256, str) or _SHA256.fullmatch(bundle_sha256) is None:
        raise ValueError("Records Release bundle hash is invalid")
    web_artifact_sha256 = root.get("web_artifact_sha256")
    if not isinstance(web_artifact_sha256, str) or _SHA256.fullmatch(web_artifact_sha256) is None:
        raise ValueError("Records Release web artifact hash is invalid")
    web_sbom_sha256 = root.get("web_sbom_sha256")
    if not isinstance(web_sbom_sha256, str) or _SHA256.fullmatch(web_sbom_sha256) is None:
        raise ValueError("Records Release web SBOM hash is invalid")
    change_sets = _object(root.get("change_sets"), "Records Change Set plans")
    if tuple(change_sets) != tuple(_STACKS):
        raise ValueError("Records Change Set plan order is invalid")
    for logical_name, (expected_stack, expected_suffix, expected_region) in _STACKS.items():
        plan = _object(change_sets.get(logical_name), f"Records {logical_name} plan")
        if set(plan) != {"stack", "name", "arn", "region", "type", "executable"}:
            raise ValueError("Records Change Set plan fields are invalid")
        name = plan.get("name")
        if (
            not isinstance(name, str)
            or _CHANGE_SET_NAME.fullmatch(name) is None
            or not name.endswith(f"-{expected_suffix}")
        ):
            raise ValueError("Records Change Set plan name is invalid")
        arn = plan.get("arn")
        if (
            not isinstance(arn, str)
            or _CHANGE_SET_ARN.fullmatch(arn) is None
            or f"changeSet/{name}/" not in arn
        ):
            raise ValueError("Records Change Set plan ARN is invalid")
        if plan.get("stack") != expected_stack:
            raise ValueError("Records Change Set plan stack is invalid")
        if plan.get(
            "region"
        ) != expected_region or f":cloudformation:{expected_region}:" not in str(arn):
            raise ValueError("Records Change Set plan region is invalid")
        change_set_type = plan.get("type")
        executable = plan.get("executable")
        if change_set_type not in {"CREATE", "UPDATE"} or not isinstance(executable, bool):
            raise ValueError("Records Change Set plan execution fields are invalid")
        if change_set_type == "CREATE" and not executable:
            raise ValueError("Records CREATE plan cannot be a no-op")


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    entry = commands.add_parser("create-entry")
    entry.add_argument("described_change_set", type=Path)
    entry.add_argument("--type", choices=("CREATE", "UPDATE"), required=True)
    entry.add_argument("--expected-name", required=True)
    entry.add_argument("--expected-region", choices=("ap-northeast-1", "us-east-1"), required=True)
    entry.add_argument("--expected-stack", required=True)
    entry.add_argument("--output", type=Path, required=True)
    create = commands.add_parser("create-manifest")
    create.add_argument("--stateful-plan", type=Path, required=True)
    create.add_argument("--application-plan", type=Path, required=True)
    create.add_argument("--edge-plan", type=Path, required=True)
    create.add_argument("--commit-sha", required=True)
    create.add_argument("--bundle-sha256", required=True)
    create.add_argument("--web-artifact-sha256", required=True)
    create.add_argument("--web-sbom-sha256", required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--expected-commit-sha", required=True)
    safety = commands.add_parser("validate-change-set-safety")
    safety.add_argument("described_change_set", type=Path)
    safety.add_argument("--logical-name", choices=tuple(_STACKS), required=True)
    safety.add_argument("--expected-edge-hostname")
    safety.add_argument("--expected-edge-zone-id")
    safety.add_argument("--expected-edge-zone-name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create-entry":
        _write(
            args.output,
            create_change_set_plan(
                _read(args.described_change_set),
                change_set_type=args.type,
                expected_name=args.expected_name,
                expected_region=args.expected_region,
                expected_stack=args.expected_stack,
            ),
        )
    elif args.command == "create-manifest":
        _write(
            args.output,
            create_manifest(
                stateful_plan=_read(args.stateful_plan),
                application_plan=_read(args.application_plan),
                edge_plan=_read(args.edge_plan),
                commit_sha=args.commit_sha,
                bundle_sha256=args.bundle_sha256,
                web_artifact_sha256=args.web_artifact_sha256,
                web_sbom_sha256=args.web_sbom_sha256,
            ),
        )
    elif args.command == "validate-manifest":
        validate_manifest(
            _read(args.manifest),
            expected_commit_sha=args.expected_commit_sha,
        )
    else:
        validate_change_set_safety(
            _read(args.described_change_set),
            logical_name=args.logical_name,
            expected_edge_hostname=args.expected_edge_hostname,
            expected_edge_zone_id=args.expected_edge_zone_id,
            expected_edge_zone_name=args.expected_edge_zone_name,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
