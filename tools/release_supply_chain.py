#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and revalidate content-addressed STEP-10 release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PROFILE_ARN = re.compile(
    r"arn:aws:signer:ap-northeast-1:[0-9]{12}:/signing-profiles/"
    r"shittim_chest_ecr\Z"
)
_REPOSITORY_URI = re.compile(
    r"([0-9]{12})\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/shittim-chest\Z"
)
_CHANGE_SET_ARN = re.compile(
    r"arn:aws:cloudformation:(?:ap-northeast-1|us-east-1):[0-9]{12}:"
    r"changeSet/[A-Za-z0-9_.-]+/[0-9a-f-]+\Z"
)
_NOTATION_SIGNATURE = "application/vnd.cncf.notary.signature"
_GITHUB_BUNDLE = "application/vnd.dev.sigstore.bundle.v0.3+json"
_PREDICATE_KEY = "dev.sigstore.bundle.predicateType"
_PREDICATES = {
    "provenance": "https://slsa.dev/provenance/v1",
    "sbom": "https://spdx.dev/Document/v2.3",
    "vulnerability": (
        "https://github.com/pitekusu/shittim-chest/attestations/vulnerability-assessment/v1"
    ),
}
_IMAGE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
_RISK_EVIDENCE_KEYS = ("grype_raw", "grype_vex", "vendor_vex")
_STACKS = (
    "ShittimChest-Prod-Stateful",
    "ShittimChest-Prod-Runtime",
    "ShittimChest-Prod-Operations",
    "ShittimChest-Prod-CostGovernance",
)


def verify_image_evidence(
    *,
    coverage: object | None = None,
    digest: str,
    image_details: object,
    profile_arn: str,
    signing_status: object,
    referrers: object,
    risk_gate_passed: bool = False,
    scan: object,
) -> dict[str, object]:
    """Validate AWS evidence and return a content-free release predicate."""

    _require_digest(digest)
    details = _object(image_details, "image details")
    matching_images = [
        _object(item, "image detail")
        for item in _array(details.get("imageDetails"), "image details")
        if isinstance(item, Mapping) and item.get("imageDigest") == digest
    ]
    if len(matching_images) != 1:
        raise ValueError("image digest does not resolve exactly once")
    media_type = matching_images[0].get("imageManifestMediaType")
    if media_type not in _IMAGE_MEDIA_TYPES:
        raise ValueError("image manifest media type is not deployable")
    if _PROFILE_ARN.fullmatch(profile_arn) is None:
        raise ValueError("signing profile ARN is invalid")
    signing = _object(signing_status, "signing status")
    statuses = _array(signing.get("signingStatuses"), "signing statuses")
    matching_statuses = [
        _object(item, "signing status")
        for item in statuses
        if isinstance(item, Mapping) and item.get("signingProfileArn") == profile_arn
    ]
    if len(matching_statuses) != 1 or matching_statuses[0].get("status") != "COMPLETE":
        raise ValueError("managed signing is not complete for the expected profile")

    listed = _object(referrers, "image referrers")
    if listed.get("nextToken") not in (None, ""):
        raise ValueError("image referrer input is not fully paginated")
    active = [
        _object(item, "image referrer")
        for item in _array(listed.get("referrers"), "image referrers")
        if isinstance(item, Mapping) and item.get("artifactStatus") == "ACTIVE"
    ]
    artifacts: dict[str, str] = {}
    signatures = [item for item in active if item.get("artifactType") == _NOTATION_SIGNATURE]
    artifacts["signature"] = _one_artifact_digest(signatures, "Notation signature")
    for name, predicate in _PREDICATES.items():
        matches = [
            item
            for item in active
            if item.get("artifactType") == _GITHUB_BUNDLE
            and isinstance(item.get("annotations"), Mapping)
            and cast(Mapping[str, object], item["annotations"]).get(_PREDICATE_KEY) == predicate
        ]
        artifacts[name] = _one_artifact_digest(matches, name)

    scan_result = create_vulnerability_predicate(
        coverage=coverage,
        digest=digest,
        risk_gate_passed=risk_gate_passed,
        scan=scan,
    )
    return {
        "schema_version": 1,
        "image_digest": digest,
        "media_type": media_type,
        "referrers": artifacts,
        "scan": scan_result,
        "signing_profile_arn": profile_arn,
    }


def _inspector_scan_timestamp(*, coverage: object, digest: str) -> str:
    payload = _object(coverage, "Inspector coverage")
    if payload.get("nextToken") not in (None, ""):
        raise ValueError("Inspector coverage input is not fully paginated")
    matches = [
        _object(item, "covered resource")
        for item in _array(payload.get("coveredResources"), "covered resources")
        if isinstance(item, Mapping)
        and item.get("resourceType") == "AWS_ECR_CONTAINER_IMAGE"
        and item.get("scanType") == "PACKAGE"
        and isinstance(resource_id := item.get("resourceId"), str)
        and resource_id.endswith(digest)
    ]
    if len(matches) != 1:
        raise ValueError("image digest does not resolve to exactly one Inspector coverage record")
    status = _object(matches[0].get("scanStatus"), "Inspector scan status")
    if status.get("statusCode") != "ACTIVE" or status.get("reason") != "SUCCESSFUL":
        raise ValueError("Inspector package scan is not successful")
    scanned_at = matches[0].get("lastScannedAt")
    if not isinstance(scanned_at, str) or not scanned_at:
        raise ValueError("Inspector scan timestamp is missing")
    return scanned_at


def create_vulnerability_predicate(
    *,
    coverage: object | None = None,
    digest: str,
    risk_gate_passed: bool = False,
    scan: object,
) -> dict[str, object]:
    """Normalize enhanced ECR counts without publishing vulnerability identifiers."""

    _require_digest(digest)
    scan_payload = _object(scan, "image scan")
    status = _object(scan_payload.get("imageScanStatus"), "image scan status")
    if status.get("status") not in {"ACTIVE", "COMPLETE"}:
        raise ValueError("enhanced image scan is not active or complete")
    findings = _object(scan_payload.get("imageScanFindings"), "image scan findings")
    raw_counts = _object(findings.get("findingSeverityCounts", {}), "severity counts")
    counts: dict[str, int] = {}
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL", "UNDEFINED"):
        value = raw_counts.get(severity, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("severity count is invalid")
        counts[severity.lower()] = value
    if (counts["critical"] or counts["high"]) and not risk_gate_passed:
        raise ValueError("release image has unaccepted high or critical findings")
    coverage_scanned_at = (
        _inspector_scan_timestamp(coverage=coverage, digest=digest)
        if coverage is not None
        else None
    )
    scanned_at = (
        findings.get("imageScanCompletedAt")
        or findings.get("vulnerabilitySourceUpdatedAt")
        or coverage_scanned_at
    )
    if not isinstance(scanned_at, str) or not scanned_at:
        raise ValueError("image scan timestamp is missing")
    return {
        "schema_version": 1,
        "image_digest": digest,
        "result": "passed",
        "risk_gate": "passed",
        "scanned_at": scanned_at,
        "scanner": "ECR_ENHANCED",
        "severity_counts": counts,
    }


def create_manifest(
    *,
    break_glass_risk_evidence: Mapping[str, Path],
    break_glass_sbom_path: Path,
    break_glass_verification: object,
    change_sets: object,
    commit_sha: str,
    lambda_bundle: object,
    normal_sbom_path: Path,
    normal_risk_evidence: Mapping[str, Path],
    normal_verification: object,
    repository_uri: str,
    runtime_config_parameter: str,
    templates: object,
) -> dict[str, object]:
    """Bind immutable plan outputs into one canonical release manifest."""

    if _SHA.fullmatch(commit_sha) is None:
        raise ValueError("commit SHA is invalid")
    repository_match = _REPOSITORY_URI.fullmatch(repository_uri)
    if repository_match is None:
        raise ValueError("repository URI is invalid")
    account = repository_match.group(1)
    images = {
        "normal": _manifest_image(
            repository_uri=repository_uri,
            risk_evidence=normal_risk_evidence,
            sbom_path=normal_sbom_path,
            verification=normal_verification,
        ),
        "break_glass": _manifest_image(
            repository_uri=repository_uri,
            risk_evidence=break_glass_risk_evidence,
            sbom_path=break_glass_sbom_path,
            verification=break_glass_verification,
        ),
    }
    if images["normal"]["digest"] == images["break_glass"]["digest"]:
        raise ValueError("normal and break-glass images must use different digests")
    changes = _object(change_sets, "change sets")
    if tuple(changes) != _STACKS:
        raise ValueError("change set order is invalid")
    for stack, arn in changes.items():
        if (
            not isinstance(arn, str)
            or _CHANGE_SET_ARN.fullmatch(arn) is None
            or f":{account}:changeSet/" not in arn
        ):
            raise ValueError(f"change set ARN is invalid for {stack}")
    template_hashes = _object(templates, "template hashes")
    if set(template_hashes) != set(_STACKS):
        raise ValueError("template hashes are incomplete")
    for value in template_hashes.values():
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("template hash is invalid")
    bundle = _object(lambda_bundle, "Lambda bundle")
    if set(bundle) != {"bucket", "key", "sha256"}:
        raise ValueError("Lambda bundle fields are invalid")
    if bundle.get("bucket") != f"cdk-hnb659fds-assets-{account}-ap-northeast-1":
        raise ValueError("Lambda bundle bucket is invalid")
    if not re.fullmatch(
        r"lambda/shittim-chest/[0-9a-f]{64}/shittim-chest-lambda-arm64\.zip",
        _string(bundle, "key", "Lambda bundle"),
    ):
        raise ValueError("Lambda bundle key is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", _string(bundle, "sha256", "Lambda bundle")):
        raise ValueError("Lambda bundle hash is invalid")
    if not runtime_config_parameter.startswith("/shittim-chest/production/runtime/v"):
        raise ValueError("runtime config parameter is not versioned")
    manifest = {
        "schema_version": 1,
        "repository": "pitekusu/shittim-chest",
        "workflow": ".github/workflows/release.yml",
        "commit_sha": commit_sha,
        "images": images,
        "templates": dict(template_hashes),
        "change_sets": dict(changes),
        "lambda_bundle": dict(bundle),
        "runtime_config_parameter": runtime_config_parameter,
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: object) -> None:
    """Reject manifest tampering and non-canonical fields before deployment."""

    root = _object(value, "release manifest")
    expected = {
        "schema_version",
        "repository",
        "workflow",
        "commit_sha",
        "images",
        "templates",
        "change_sets",
        "lambda_bundle",
        "runtime_config_parameter",
    }
    if set(root) != expected or root.get("schema_version") != 1:
        raise ValueError("release manifest schema is invalid")
    if root.get("repository") != "pitekusu/shittim-chest":
        raise ValueError("release manifest repository is invalid")
    if root.get("workflow") != ".github/workflows/release.yml":
        raise ValueError("release manifest workflow is invalid")
    commit_sha = root.get("commit_sha")
    if not isinstance(commit_sha, str) or _SHA.fullmatch(commit_sha) is None:
        raise ValueError("release manifest commit is invalid")
    images = _object(root.get("images"), "release images")
    if tuple(images) != ("normal", "break_glass"):
        raise ValueError("release image order is invalid")
    validated_digests: list[str] = []
    release_account: str | None = None
    for name, raw_image in images.items():
        image = _object(raw_image, f"release image {name}")
        expected_image_fields = {
            "digest",
            "media_type",
            "reference",
            "repository_uri",
            "referrers",
            "risk_evidence",
            "sbom",
            "scan",
            "signing_profile_arn",
        }
        if set(image) != expected_image_fields:
            raise ValueError("release image fields are invalid")
        digest = _string(image, "digest", "release image")
        _require_digest(digest)
        validated_digests.append(digest)
        repository_uri = _string(image, "repository_uri", "release image")
        repository_match = _REPOSITORY_URI.fullmatch(repository_uri)
        if repository_match is None or image.get("reference") != f"{repository_uri}@{digest}":
            raise ValueError("release image reference is invalid")
        account = repository_match.group(1)
        if release_account is None:
            release_account = account
        elif release_account != account:
            raise ValueError("release image accounts do not match")
        if image.get("media_type") not in _IMAGE_MEDIA_TYPES:
            raise ValueError("release image media type is invalid")
        profile = image.get("signing_profile_arn")
        if not isinstance(profile, str) or _PROFILE_ARN.fullmatch(profile) is None:
            raise ValueError("release signing profile is invalid")
        if f":{account}:/signing-profiles/" not in profile:
            raise ValueError("release signing profile account is invalid")
        referrers = _object(image.get("referrers"), "release referrers")
        if set(referrers) != {"signature", "sbom", "provenance", "vulnerability"}:
            raise ValueError("release referrers are incomplete")
        for artifact in referrers.values():
            _require_digest(artifact)
        risk_evidence = _object(image.get("risk_evidence"), "release risk evidence")
        if tuple(risk_evidence) != _RISK_EVIDENCE_KEYS:
            raise ValueError("release risk evidence is incomplete")
        for evidence_hash in risk_evidence.values():
            if (
                not isinstance(evidence_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", evidence_hash) is None
            ):
                raise ValueError("release risk evidence hash is invalid")
        sbom = _object(image.get("sbom"), "release SBOM")
        if set(sbom) != {"format", "sha256"} or sbom.get("format") != "spdx-json-2.3":
            raise ValueError("release SBOM metadata is invalid")
        sbom_sha = sbom.get("sha256")
        if not isinstance(sbom_sha, str) or re.fullmatch(r"[0-9a-f]{64}", sbom_sha) is None:
            raise ValueError("release SBOM hash is invalid")
        _validate_scan_predicate(image.get("scan"), digest=digest)
    if len(set(validated_digests)) != 2:
        raise ValueError("release image digests must be distinct")
    changes = _object(root.get("change_sets"), "release change sets")
    if tuple(changes) != _STACKS:
        raise ValueError("release change set order is invalid")
    for arn in changes.values():
        if (
            not isinstance(arn, str)
            or _CHANGE_SET_ARN.fullmatch(arn) is None
            or release_account is None
            or f":{release_account}:changeSet/" not in arn
        ):
            raise ValueError("release change set ARN is invalid")
    templates = _object(root.get("templates"), "release templates")
    if set(templates) != set(_STACKS):
        raise ValueError("release template hashes are invalid")
    for template_hash in templates.values():
        if (
            not isinstance(template_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", template_hash) is None
        ):
            raise ValueError("release template hash is invalid")
    bundle = _object(root.get("lambda_bundle"), "release Lambda bundle")
    if set(bundle) != {"bucket", "key", "sha256"}:
        raise ValueError("release Lambda bundle is invalid")
    if bundle.get("bucket") != (f"cdk-hnb659fds-assets-{release_account}-ap-northeast-1"):
        raise ValueError("release Lambda bundle bucket is invalid")
    if not re.fullmatch(
        r"lambda/shittim-chest/[0-9a-f]{64}/shittim-chest-lambda-arm64\.zip",
        _string(bundle, "key", "release Lambda bundle"),
    ):
        raise ValueError("release Lambda bundle key is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", _string(bundle, "sha256", "release Lambda bundle")):
        raise ValueError("release Lambda bundle hash is invalid")
    runtime_config = root.get("runtime_config_parameter")
    if (
        not isinstance(runtime_config, str)
        or re.fullmatch(r"/shittim-chest/production/runtime/v[0-9]{4}", runtime_config) is None
    ):
        raise ValueError("release runtime config parameter is invalid")


def validate_runtime_template(
    value: object,
    *,
    break_glass_digest: str,
    normal_digest: str,
    repository_uri: str,
) -> None:
    """Require every application task image in the runtime template to use one digest."""

    _require_digest(normal_digest)
    _require_digest(break_glass_digest)
    template = _object(value, "runtime template")
    resources = _object(template.get("Resources"), "runtime resources")
    images: dict[str, object] = {}
    for resource in resources.values():
        record = _object(resource, "runtime resource")
        if record.get("Type") != "AWS::ECS::TaskDefinition":
            continue
        properties = _object(record.get("Properties"), "task definition")
        for container in _array(properties.get("ContainerDefinitions"), "containers"):
            container_record = _object(container, "container")
            if container_record.get("Name") in {"application", "break-glass-application"}:
                name = _string(container_record, "Name", "container")
                if name in images:
                    raise ValueError("runtime task image container is duplicated")
                images[name] = container_record.get("Image")
    expected = {
        "application": f"{repository_uri}@{normal_digest}",
        "break-glass-application": f"{repository_uri}@{break_glass_digest}",
    }
    if images != expected:
        raise ValueError("runtime task images are not the exact release digest")


def _manifest_image(
    *,
    repository_uri: str,
    risk_evidence: Mapping[str, Path],
    sbom_path: Path,
    verification: object,
) -> dict[str, object]:
    record = _object(verification, "verification")
    digest = _string(record, "image_digest", "verification")
    _require_digest(digest)
    profile = _string(record, "signing_profile_arn", "verification")
    if _PROFILE_ARN.fullmatch(profile) is None:
        raise ValueError("signing profile ARN is invalid")
    artifacts = _object(record.get("referrers"), "referrers")
    if set(artifacts) != {"signature", "sbom", "provenance", "vulnerability"}:
        raise ValueError("release verification referrers are incomplete")
    for value in artifacts.values():
        _require_digest(value)
    _validate_scan_predicate(record.get("scan"), digest=digest)
    if not sbom_path.is_file():
        raise ValueError("SBOM does not exist")
    return {
        "digest": digest,
        "media_type": _string(record, "media_type", "verification"),
        "reference": f"{repository_uri}@{digest}",
        "repository_uri": repository_uri,
        "referrers": dict(artifacts),
        "risk_evidence": _hash_evidence(risk_evidence),
        "sbom": {
            "format": "spdx-json-2.3",
            "sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
        },
        "scan": record["scan"],
        "signing_profile_arn": profile,
    }


def validate_change_set(
    value: object,
    *,
    expected_arn: str,
    expected_noecho_parameters: Sequence[str] = (),
    expected_parameters: Mapping[str, str],
    expected_stack: str,
) -> None:
    """Bind a described immutable change set to its manifest ARN and inputs.

    DescribeChangeSet does not return the ChangeSetType supplied to CreateChangeSet. The trusted
    workflow determines create versus update from the stack status before creation and execution.
    """

    record = _object(value, "change set")
    if record.get("ChangeSetId") != expected_arn or record.get("StackName") != expected_stack:
        raise ValueError("change set identity is invalid")
    status = record.get("Status")
    execution = record.get("ExecutionStatus")
    if status == "CREATE_COMPLETE":
        if execution != "AVAILABLE":
            raise ValueError("change set is not executable")
    elif status == "FAILED":
        reason = record.get("StatusReason")
        if (
            execution != "UNAVAILABLE"
            or not isinstance(reason, str)
            or "didn't contain changes" not in reason
        ):
            raise ValueError("change set failed for a reason other than no changes")
    else:
        raise ValueError("change set creation is incomplete")
    parameters: dict[str, str] = {}
    for item in _array(record.get("Parameters", []), "change set parameters"):
        parameter = _object(item, "change set parameter")
        key = parameter.get("ParameterKey")
        if not isinstance(key, str) or not key or key in parameters:
            raise ValueError("change set parameter key is invalid")
        value = parameter.get("ParameterValue")
        if isinstance(value, str):
            parameters[key] = value
    for key, expected in expected_parameters.items():
        if parameters.get(key) != expected:
            raise ValueError(f"change set parameter is invalid: {key}")
    if set(expected_noecho_parameters) & set(expected_parameters):
        raise ValueError("change set parameter cannot be both exact and NoEcho")
    if len(set(expected_noecho_parameters)) != len(expected_noecho_parameters):
        raise ValueError("expected NoEcho parameter is duplicated")
    for key in expected_noecho_parameters:
        value = parameters.get(key)
        if not isinstance(value, str) or re.fullmatch(r"\*+", value) is None:
            raise ValueError(f"change set NoEcho parameter is invalid: {key}")


def _hash_evidence(paths: Mapping[str, Path]) -> dict[str, str]:
    if tuple(paths) != _RISK_EVIDENCE_KEYS:
        raise ValueError("risk evidence paths are incomplete")
    hashes: dict[str, str] = {}
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError("risk evidence file does not exist")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _validate_scan_predicate(value: object, *, digest: str) -> None:
    scan = _object(value, "release scan")
    if set(scan) != {
        "schema_version",
        "image_digest",
        "result",
        "risk_gate",
        "scanned_at",
        "scanner",
        "severity_counts",
    }:
        raise ValueError("release scan fields are invalid")
    if (
        scan.get("schema_version") != 1
        or scan.get("image_digest") != digest
        or scan.get("result") != "passed"
        or scan.get("risk_gate") != "passed"
        or scan.get("scanner") != "ECR_ENHANCED"
        or not isinstance(scan.get("scanned_at"), str)
        or not scan.get("scanned_at")
    ):
        raise ValueError("release scan is invalid")
    counts = _object(scan.get("severity_counts"), "release severity counts")
    if set(counts) != {"critical", "high", "medium", "low", "informational", "undefined"}:
        raise ValueError("release severity counts are invalid")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts.values()
    ):
        raise ValueError("release severity count is invalid")


def _one_artifact_digest(items: Sequence[Mapping[str, object]], name: str) -> str:
    if len(items) != 1:
        raise ValueError(f"expected exactly one active {name} referrer")
    digest = items[0].get("digest")
    _require_digest(digest)
    return cast(str, digest)


def _require_digest(value: object) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("OCI digest is invalid")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{name} must be an array")
    return value


def _string(record: Mapping[str, object], key: str, name: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}.{key} must be a non-empty string")
    return value


def _read(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-image")
    verify.add_argument("--digest", required=True)
    verify.add_argument("--coverage", type=Path, required=True)
    verify.add_argument("--image-details", type=Path, required=True)
    verify.add_argument("--profile-arn", required=True)
    verify.add_argument("--signing-status", type=Path, required=True)
    verify.add_argument("--referrers", type=Path, required=True)
    verify.add_argument("--scan", type=Path, required=True)
    verify.add_argument("--risk-gate-passed", action="store_true")
    verify.add_argument("--output", type=Path, required=True)
    predicate = commands.add_parser("create-vulnerability-predicate")
    predicate.add_argument("--digest", required=True)
    predicate.add_argument("--coverage", type=Path, required=True)
    predicate.add_argument("--scan", type=Path, required=True)
    predicate.add_argument("--risk-gate-passed", action="store_true")
    predicate.add_argument("--output", type=Path, required=True)
    create = commands.add_parser("create-manifest")
    create.add_argument("--break-glass-raw-grype", type=Path, required=True)
    create.add_argument("--break-glass-vendor-vex", type=Path, required=True)
    create.add_argument("--break-glass-vex-grype", type=Path, required=True)
    create.add_argument("--break-glass-sbom", type=Path, required=True)
    create.add_argument("--break-glass-verification", type=Path, required=True)
    create.add_argument("--change-sets", type=Path, required=True)
    create.add_argument("--commit-sha", required=True)
    create.add_argument("--lambda-bundle", type=Path, required=True)
    create.add_argument("--repository-uri", required=True)
    create.add_argument("--runtime-config-parameter", required=True)
    create.add_argument("--normal-sbom", type=Path, required=True)
    create.add_argument("--normal-raw-grype", type=Path, required=True)
    create.add_argument("--normal-vendor-vex", type=Path, required=True)
    create.add_argument("--normal-vex-grype", type=Path, required=True)
    create.add_argument("--normal-verification", type=Path, required=True)
    create.add_argument("--templates", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)
    runtime = commands.add_parser("validate-runtime-template")
    runtime.add_argument("template", type=Path)
    runtime.add_argument("--repository-uri", required=True)
    runtime.add_argument("--normal-digest", required=True)
    runtime.add_argument("--break-glass-digest", required=True)
    change_set = commands.add_parser("validate-change-set")
    change_set.add_argument("change_set", type=Path)
    change_set.add_argument("--expected-arn", required=True)
    change_set.add_argument("--expected-stack", required=True)
    change_set.add_argument("--expected-parameter", action="append", default=[])
    change_set.add_argument("--expected-noecho-parameter", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify-image":
            result = verify_image_evidence(
                coverage=_read(args.coverage),
                digest=args.digest,
                image_details=_read(args.image_details),
                profile_arn=args.profile_arn,
                signing_status=_read(args.signing_status),
                referrers=_read(args.referrers),
                risk_gate_passed=args.risk_gate_passed,
                scan=_read(args.scan),
            )
            _write(args.output, result)
        elif args.command == "create-vulnerability-predicate":
            _write(
                args.output,
                create_vulnerability_predicate(
                    coverage=_read(args.coverage),
                    digest=args.digest,
                    risk_gate_passed=args.risk_gate_passed,
                    scan=_read(args.scan),
                ),
            )
        elif args.command == "create-manifest":
            result = create_manifest(
                break_glass_risk_evidence={
                    "grype_raw": args.break_glass_raw_grype,
                    "grype_vex": args.break_glass_vex_grype,
                    "vendor_vex": args.break_glass_vendor_vex,
                },
                break_glass_sbom_path=args.break_glass_sbom,
                break_glass_verification=_read(args.break_glass_verification),
                change_sets=_read(args.change_sets),
                commit_sha=args.commit_sha,
                lambda_bundle=_read(args.lambda_bundle),
                normal_sbom_path=args.normal_sbom,
                normal_risk_evidence={
                    "grype_raw": args.normal_raw_grype,
                    "grype_vex": args.normal_vex_grype,
                    "vendor_vex": args.normal_vendor_vex,
                },
                normal_verification=_read(args.normal_verification),
                repository_uri=args.repository_uri,
                runtime_config_parameter=args.runtime_config_parameter,
                templates=_read(args.templates),
            )
            _write(args.output, result)
        elif args.command == "validate-manifest":
            validate_manifest(_read(args.manifest))
        elif args.command == "validate-runtime-template":
            validate_runtime_template(
                _read(args.template),
                break_glass_digest=args.break_glass_digest,
                normal_digest=args.normal_digest,
                repository_uri=args.repository_uri,
            )
        elif args.command == "validate-change-set":
            expected_parameters: dict[str, str] = {}
            for raw in args.expected_parameter:
                key, separator, value = raw.partition("=")
                if not separator or not key or key in expected_parameters:
                    raise ValueError("expected change set parameter is invalid")
                expected_parameters[key] = value
            validate_change_set(
                _read(args.change_set),
                expected_arn=args.expected_arn,
                expected_noecho_parameters=args.expected_noecho_parameter,
                expected_parameters=expected_parameters,
                expected_stack=args.expected_stack,
            )
        else:  # pragma: no cover
            raise AssertionError("unreachable command")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"release supply-chain check failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
