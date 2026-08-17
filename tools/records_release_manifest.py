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
    else:
        validate_manifest(
            _read(args.manifest),
            expected_commit_sha=args.expected_commit_sha,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
