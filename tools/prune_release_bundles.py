#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Keep three distinct deployment ZIPs per family, protecting deployed references.

Dry-run by default. Apply requires the exact plan digest from a prior dry-run.
Run under the release workflows' shared production-release concurrency group;
operators must ensure no release is running before applying manually.
"""

import argparse
import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from zipfile import BadZipFile, ZipFile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = "ap-northeast-1"
KEEP_GENERATIONS = 3
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
STACKS = {
    "core": "ShittimChest-Prod-Runtime",
    "records": "ShittimChest-Prod-RecordsApplication",
}
ZIP_PATTERNS = {
    "records": re.compile(r"[0-9a-f]{64}\.zip"),
    "core": re.compile(r"lambda/shittim-chest/[0-9a-f]{64}/shittim-chest-lambda-arm64\.zip"),
}
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class CleanupError(RuntimeError):
    """Only content-free, stable error codes reach the CLI output."""


@dataclass(frozen=True)
class Version:
    key: str
    version_id: str
    size: int
    modified: datetime
    latest: bool


def resolve(value: object, parameters: dict[str, str]) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and set(value) == {"Ref"}:
        result = parameters.get(value["Ref"])
        if result:
            return result
    raise CleanupError("unresolved_deployed_asset")


def deployed_references(cloudformation: Any, bucket: str) -> dict[str, set[tuple[str, str | None]]]:
    protected: dict[str, set[tuple[str, str | None]]] = {}
    for family, stack_name in STACKS.items():
        stacks = cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
        if len(stacks) != 1 or stacks[0]["StackStatus"] not in {
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
        }:
            raise CleanupError("stack_not_stable")
        for page in cloudformation.get_paginator("list_change_sets").paginate(StackName=stack_name):
            if any(
                item["ExecutionStatus"] in {"AVAILABLE", "EXECUTE_IN_PROGRESS"}
                for item in page.get("Summaries", [])
            ):
                raise CleanupError("pending_release_change_set")
        parameters = {
            item["ParameterKey"]: item["ParameterValue"] for item in stacks[0].get("Parameters", [])
        }
        template = cloudformation.get_template(
            StackName=stack_name,
            TemplateStage="Processed",
        )["TemplateBody"]
        if isinstance(template, str):
            template = json.loads(template)
        references: set[tuple[str, str | None]] = set()
        for resource in template["Resources"].values():
            if resource["Type"] not in {"AWS::Lambda::Function", "AWS::Lambda::LayerVersion"}:
                continue
            properties = resource["Properties"]
            code = properties.get("Code", properties.get("Content", {}))
            if "S3Bucket" not in code:
                continue
            if resolve(code["S3Bucket"], parameters) != bucket:
                raise CleanupError("unexpected_deployed_bucket")
            key = resolve(code["S3Key"], parameters)
            version = (
                resolve(code["S3ObjectVersion"], parameters) if "S3ObjectVersion" in code else None
            )
            references.add((key, version))
        if not references:
            raise CleanupError("missing_deployed_references")
        protected[family] = references
    return protected


def inventory(s3: Any, bucket: str, account: str) -> list[Version]:
    versions: list[Version] = []
    for page in s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket,
        ExpectedBucketOwner=account,
    ):
        if any(
            any(pattern.fullmatch(item["Key"]) for pattern in ZIP_PATTERNS.values())
            for item in page.get("DeleteMarkers", [])
        ):
            raise CleanupError("bundle_delete_marker_present")
        for item in page.get("Versions", []):
            if not any(pattern.fullmatch(item["Key"]) for pattern in ZIP_PATTERNS.values()):
                continue
            versions.append(
                Version(
                    item["Key"],
                    item["VersionId"],
                    item["Size"],
                    item["LastModified"],
                    item["IsLatest"],
                )
            )
    return versions


def records_bundle_keys(s3: Any, bucket: str, account: str, versions: list[Version]) -> set[str]:
    """Identify Records ZIPs, including historical keys, without extracting any files."""
    keys: set[str] = set()
    for version in versions:
        if not version.latest or not ZIP_PATTERNS["records"].fullmatch(version.key):
            continue
        if not 0 < version.size <= MAX_BUNDLE_BYTES:
            raise CleanupError("bundle_size_out_of_bounds")
        with s3.get_object(
            Bucket=bucket,
            Key=version.key,
            VersionId=version.version_id,
            ExpectedBucketOwner=account,
        )["Body"] as body:
            payload = body.read(MAX_BUNDLE_BYTES + 1)
        if len(payload) != version.size:
            raise CleanupError("bundle_size_mismatch")
        try:
            with ZipFile(BytesIO(payload)) as archive:
                if "shittim_records/__init__.py" in archive.namelist():
                    keys.add(version.key)
        except BadZipFile as error:
            raise CleanupError("invalid_bundle_zip") from error
    return keys


def plan_deletions(
    versions: list[Version],
    references: dict[str, set[tuple[str, str | None]]],
    family: str,
    records_keys: set[str],
) -> list[Version]:
    by_key: dict[str, list[Version]] = defaultdict(list)
    for version in versions:
        by_key[version.key].append(version)
    for refs in references.values():
        for key, version_id in refs:
            if key not in by_key or not any(
                item.version_id == version_id if version_id is not None else item.latest
                for item in by_key[key]
            ):
                raise CleanupError("deployed_asset_missing")
    # A Core auxiliary Lambda uses a root ZIP. It is not a Records generation.
    other_keys = {key for other, refs in references.items() if other != family for key, _ in refs}
    keys = {key for key in by_key if ZIP_PATTERNS[family].fullmatch(key) and key not in other_keys}
    if family == "records":
        keys &= records_keys
    current_keys = {key for key, _ in references[family] if key in keys}
    if not current_keys or len(current_keys) > KEEP_GENERATIONS:
        raise CleanupError("unexpected_current_generation_count")
    newest = sorted(
        keys - current_keys,
        key=lambda key: (max(item.modified for item in by_key[key]), key),
        reverse=True,
    )
    retained = current_keys | set(newest[: KEEP_GENERATIONS - len(current_keys)])
    pinned = {ref for refs in references.values() for ref in refs}
    deleted = [
        item
        for key in keys
        for item in by_key[key]
        if (key not in retained or not item.latest) and (key, item.version_id) not in pinned
    ]
    # The caller finishes all noncurrent deletions before deleting current versions.
    return sorted(deleted, key=lambda item: (item.latest, item.modified, item.key, item.version_id))


def plan_digest(
    versions: list[Version], references: dict[str, set[tuple[str, str | None]]], family: str
) -> str:
    payload = {
        "family": family,
        "versions": sorted(
            (item.key, item.version_id, item.size, item.modified.isoformat(), item.latest)
            for item in versions
        ),
        "references": {
            name: sorted((key, version or "") for key, version in refs)
            for name, refs in references.items()
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prune(
    s3: Any,
    cloudformation: Any,
    *,
    account: str,
    family: str,
    apply: bool,
    expected_plan: str | None,
) -> dict[str, object]:
    bucket = f"cdk-hnb659fds-assets-{account}-{REGION}"
    if (
        s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=account).get("Status")
        != "Enabled"
    ):
        raise CleanupError("bucket_versioning_not_enabled")
    references = deployed_references(cloudformation, bucket)
    versions = inventory(s3, bucket, account)
    records_keys = (
        records_bundle_keys(s3, bucket, account, versions) if family == "records" else set()
    )
    deletions = plan_deletions(versions, references, family, records_keys)
    digest = plan_digest(versions, references, family)
    result: dict[str, object] = {
        "state": "planned",
        "family": family,
        "keep_generations": KEEP_GENERATIONS,
        "plan_sha256": digest,
        "delete_versions": len(deletions),
        "delete_bytes": sum(item.size for item in deletions),
    }
    if not apply:
        return result
    if expected_plan != digest:
        raise CleanupError("cleanup_plan_changed")
    # Re-read references immediately before irreversible version deletion.
    if deployed_references(cloudformation, bucket) != references:
        raise CleanupError("deployed_references_changed")
    # DeleteObjects has no ordering guarantee within a batch. Finish older versions
    # first so a failed batch cannot expose an older object as the current version.
    for latest in (False, True):
        phase = [item for item in deletions if item.latest is latest]
        for offset in range(0, len(phase), 1000):
            response = s3.delete_objects(
                Bucket=bucket,
                ExpectedBucketOwner=account,
                Delete={
                    "Objects": [
                        {"Key": item.key, "VersionId": item.version_id}
                        for item in phase[offset : offset + 1000]
                    ],
                    "Quiet": True,
                },
            )
            if response.get("Errors"):
                raise CleanupError("partial_version_deletion_failed")
    remaining = inventory(s3, bucket, account)
    if {(item.key, item.version_id) for item in remaining} & {
        (item.key, item.version_id) for item in deletions
    }:
        raise CleanupError("deletion_verification_failed")
    if plan_deletions(remaining, deployed_references(cloudformation, bucket), family, records_keys):
        raise CleanupError("retention_verification_failed")
    return result | {"state": "deleted_and_verified"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--family", choices=tuple(STACKS), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    args = parser.parse_args()
    # SDK wire logs can contain signed headers; never emit them in this operator tool.
    logging.disable(logging.CRITICAL)
    try:
        if not re.fullmatch(r"\d{12}", args.account_id):
            raise CleanupError("invalid_account_id")
        session = boto3.Session(region_name=REGION)
        if (
            session.client("sts", config=SDK_CONFIG).get_caller_identity()["Account"]
            != args.account_id
        ):
            raise CleanupError("unexpected_aws_account")
        result = prune(
            session.client("s3", config=SDK_CONFIG),
            session.client("cloudformation", config=SDK_CONFIG),
            account=args.account_id,
            family=args.family,
            apply=args.apply,
            expected_plan=args.expected_plan_sha256,
        )
        print(json.dumps(result))
        return 0
    except CleanupError as error:
        category = str(error)
    except ClientError, BotoCoreError:
        category = "aws_request_failed"
    except KeyError, TypeError, ValueError:
        category = "invalid_aws_response"
    print(json.dumps({"state": "stopped", "category": category}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
