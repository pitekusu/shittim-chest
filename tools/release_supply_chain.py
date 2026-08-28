#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build and revalidate content-addressed STEP-10 release evidence."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
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
_CDK_ASSET_STACKS = (
    ("ShittimChest-Prod-Stateful", "Stateful", "ap-northeast-1"),
    ("ShittimChest-Prod-Runtime", "Runtime", "ap-northeast-1"),
    ("ShittimChest-Prod-Operations", "Operations", "ap-northeast-1"),
    ("ShittimChest-Prod-CostGovernance", "CostGovernance", "us-east-1"),
)
_ACCOUNT_ID = re.compile(r"[0-9]{12}\Z")
_CONTENT_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CDK_ASSET_MANIFEST_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_S3_SHA256_CHECKSUM = re.compile(r"[A-Za-z0-9+/]{43}=\Z")
_S3_SINGLE_PART_MAX_BYTES = 5 * 1024 * 1024 - 1
_ZIP_SOURCE_MAX_BYTES = 1024 * 1024
_ZIP_SOURCE_MAX_FILES = 1000
_ZIP_SOURCE_MAX_PATH_BYTES = 512 * 1024


def _validate_single_part_asset_source(source: Path, *, packaging: str) -> None:
    """Keep HeadObject SHA-256 evidence unambiguously single-part."""

    if packaging == "file":
        if not source.is_file() or source.is_symlink():
            raise ValueError("CDK file asset source does not exist safely")
        if source.stat().st_size > _S3_SINGLE_PART_MAX_BYTES:
            raise ValueError("CDK file asset exceeds the single-part checksum boundary")
        return
    if not source.is_dir() or source.is_symlink():
        raise ValueError("CDK zip asset source does not exist safely")
    file_count = 0
    path_bytes = 0
    total_bytes = 0
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError("CDK zip asset source contains a symlink")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise ValueError("CDK zip asset source contains a special file")
        file_count += 1
        path_bytes += len(entry.relative_to(source).as_posix().encode("utf-8"))
        total_bytes += entry.stat().st_size
        if (
            file_count > _ZIP_SOURCE_MAX_FILES
            or path_bytes > _ZIP_SOURCE_MAX_PATH_BYTES
            or total_bytes > _ZIP_SOURCE_MAX_BYTES
        ):
            raise ValueError("CDK zip asset exceeds the single-part checksum boundary")


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


def select_release_referrers(
    *,
    before: object,
    after: object,
    notation_inspection: object,
    profile_arn: str,
) -> dict[str, object]:
    """Select a stable existing Notation signature and this run's new attestations."""

    if _PROFILE_ARN.fullmatch(profile_arn) is None:
        raise ValueError("signing profile ARN is invalid")
    before_by_digest = _active_referrers_by_digest(before, "pre-attestation referrers")
    after_by_digest = _active_referrers_by_digest(after, "post-attestation referrers")
    if not before_by_digest.keys() <= after_by_digest.keys():
        raise ValueError("an existing active referrer disappeared during attestation")

    added_digests = after_by_digest.keys() - before_by_digest.keys()
    if len(added_digests) != len(_PREDICATES):
        raise ValueError("expected exactly three new active Sigstore referrers")

    predicate_names = {value: name for name, value in _PREDICATES.items()}
    added_by_name: dict[str, Mapping[str, object]] = {}
    for digest in sorted(added_digests):
        item = after_by_digest[digest]
        if item.get("artifactType") != _GITHUB_BUNDLE:
            raise ValueError("a newly added referrer is not a Sigstore bundle")
        annotations = _object(item.get("annotations"), "new Sigstore referrer annotations")
        predicate = annotations.get(_PREDICATE_KEY)
        if not isinstance(predicate, str) or predicate not in predicate_names:
            raise ValueError("a newly added Sigstore referrer has an unknown predicate")
        name = predicate_names[predicate]
        if name in added_by_name:
            raise ValueError(f"new Sigstore referrer predicate is duplicated: {name}")
        added_by_name[name] = item
    if set(added_by_name) != set(_PREDICATES):
        raise ValueError("new Sigstore referrers do not cover every required predicate")

    signatures = [
        item for item in after_by_digest.values() if item.get("artifactType") == _NOTATION_SIGNATURE
    ]
    active_signature_digests = {cast(str, item["digest"]) for item in signatures}
    if not active_signature_digests:
        raise ValueError("expected at least one active Notation signature referrer")
    if any(digest not in before_by_digest for digest in active_signature_digests):
        raise ValueError("a Notation signature was not present before attestations")

    inspected = _object(notation_inspection, "Notation inspection")
    inspected_signatures = _array(inspected.get("Signatures"), "Notation inspection signatures")
    inspected_by_digest: dict[str, Mapping[str, object]] = {}
    for value in inspected_signatures:
        item = _object(value, "Notation inspected signature")
        digest = item.get("digest")
        _require_digest(digest)
        if digest in inspected_by_digest:
            raise ValueError("Notation inspection contains a duplicate signature digest")
        inspected_by_digest[cast(str, digest)] = item
    if set(inspected_by_digest) != active_signature_digests:
        raise ValueError("Notation inspection does not match active signature referrers")

    profile_version = re.compile(rf"{re.escape(profile_arn)}/[A-Za-z0-9]{{10}}")
    matching_signature_digests = []
    for digest, item in inspected_by_digest.items():
        attributes = _object(item.get("signedAttributes"), "Notation signed attributes")
        if profile_version.fullmatch(
            cast(str, attributes.get("com.amazonaws.signer.signingProfileVersion", ""))
        ):
            matching_signature_digests.append(digest)
    if not matching_signature_digests:
        raise ValueError("no active Notation signature matches the expected signing profile")
    signature_digest = min(matching_signature_digests)

    return {
        "referrers": [
            after_by_digest[signature_digest],
            *(added_by_name[name] for name in _PREDICATES),
        ]
    }


def _active_referrers_by_digest(
    value: object,
    name: str,
) -> dict[str, Mapping[str, object]]:
    payload = _object(value, name)
    if payload.get("nextToken") not in (None, ""):
        raise ValueError(f"{name} input is not fully paginated")
    result: dict[str, Mapping[str, object]] = {}
    for value_item in _array(payload.get("referrers"), name):
        item = _object(value_item, "image referrer")
        if item.get("artifactStatus") != "ACTIVE":
            raise ValueError(f"{name} contains a non-active referrer")
        digest = item.get("digest")
        _require_digest(digest)
        artifact_type = item.get("artifactType")
        if not isinstance(artifact_type, str) or not artifact_type:
            raise ValueError(f"{name} contains an invalid artifact type")
        if digest in result:
            raise ValueError(f"{name} contains a duplicate referrer digest")
        result[cast(str, digest)] = item
    return result


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
    """Normalize enhanced ECR evidence without publishing vulnerability identifiers."""

    _require_digest(digest)
    scan_payload = _object(scan, "image scan")
    if scan_payload.get("nextToken") not in (None, ""):
        raise ValueError("image scan input is not fully paginated")
    status = _object(scan_payload.get("imageScanStatus"), "image scan status")
    if status.get("status") not in {"ACTIVE", "COMPLETE"}:
        raise ValueError("enhanced image scan is not active or complete")
    findings = _object(scan_payload.get("imageScanFindings"), "image scan findings")
    raw_counts = _object(findings.get("findingSeverityCounts", {}), "severity counts")
    allowed_severities = {
        "CRITICAL",
        "HIGH",
        "MEDIUM",
        "LOW",
        "INFORMATIONAL",
        "UNDEFINED",
        "UNTRIAGED",
    }
    if not set(raw_counts) <= allowed_severities or (
        "UNDEFINED" in raw_counts and "UNTRIAGED" in raw_counts
    ):
        raise ValueError("severity count keys are invalid")
    counts: dict[str, int] = {}
    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL"):
        value = raw_counts.get(severity, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("severity count is invalid")
        counts[severity.lower()] = value
    undefined = raw_counts.get("UNTRIAGED", raw_counts.get("UNDEFINED", 0))
    if isinstance(undefined, bool) or not isinstance(undefined, int) or undefined < 0:
        raise ValueError("severity count is invalid")
    counts["undefined"] = undefined
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
    vulnerability_set_sha256 = _enhanced_vulnerability_set_sha256(
        findings=findings,
        severity_counts=counts,
    )
    return {
        "schema_version": 2,
        "image_digest": digest,
        "result": "passed",
        "risk_gate": "passed",
        "scanned_at": scanned_at,
        "scanner": "ECR_ENHANCED",
        "severity_counts": counts,
        "vulnerability_set_sha256": vulnerability_set_sha256,
    }


def _enhanced_vulnerability_set_sha256(
    *,
    findings: Mapping[str, object],
    severity_counts: Mapping[str, int],
) -> str:
    raw_findings = _array(findings.get("enhancedFindings", []), "enhanced findings")
    normalized: list[dict[str, str]] = []
    observed_counts = {severity: 0 for severity in severity_counts}
    finding_arns: set[str] = set()
    severity_names = {
        "CRITICAL": "critical",
        "HIGH": "high",
        "MEDIUM": "medium",
        "LOW": "low",
        "INFORMATIONAL": "informational",
        "UNTRIAGED": "undefined",
    }
    for raw_finding in raw_findings:
        finding = _object(raw_finding, "enhanced finding")
        finding_arn = _string(finding, "findingArn", "enhanced finding")
        if finding_arn in finding_arns:
            raise ValueError("enhanced finding ARN is duplicated")
        finding_arns.add(finding_arn)
        severity = _string(finding, "severity", "enhanced finding")
        severity_key = severity_names.get(severity)
        if severity_key is None:
            raise ValueError("enhanced finding severity is invalid")
        observed_counts[severity_key] += 1
        details = _object(
            finding.get("packageVulnerabilityDetails"),
            "enhanced finding package vulnerability details",
        )
        normalized.append(
            {
                "finding_arn": finding_arn,
                "severity": severity,
                "status": _string(finding, "status", "enhanced finding"),
                "vulnerability_id": _string(
                    details,
                    "vulnerabilityId",
                    "enhanced finding package vulnerability details",
                ),
            }
        )
    if observed_counts != severity_counts:
        raise ValueError("enhanced findings do not match severity counts")
    payload = json.dumps(
        sorted(normalized, key=lambda item: item["finding_arn"]),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_cdk_asset_evidence(
    *,
    account: str,
    assembly_dir: Path,
    require_sources: bool = True,
) -> dict[str, object]:
    """Create a complete, canonical inventory from the four CDK asset manifests."""

    if _ACCOUNT_ID.fullmatch(account) is None:
        raise ValueError("CDK asset account is invalid")
    if not assembly_dir.is_dir():
        raise ValueError("CDK cloud assembly does not exist")
    manifests: dict[str, object] = {}
    files: list[dict[str, object]] = []
    for stack, artifact, expected_region in _CDK_ASSET_STACKS:
        manifest_path = assembly_dir / f"{artifact}.assets.json"
        if not manifest_path.is_file():
            raise ValueError(f"CDK asset manifest is missing: {artifact}")
        manifest_bytes = manifest_path.read_bytes()
        manifest = _object(json.loads(manifest_bytes), f"CDK asset manifest {artifact}")
        if set(manifest) != {"dockerImages", "files", "version"}:
            raise ValueError("CDK asset manifest fields are invalid")
        version = manifest.get("version")
        if not isinstance(version, str) or _CDK_ASSET_MANIFEST_VERSION.fullmatch(version) is None:
            raise ValueError("CDK asset manifest version is invalid")
        docker_images = _object(manifest.get("dockerImages"), "CDK Docker assets")
        if docker_images:
            raise ValueError("CDK Docker assets must use the signed release image path")
        raw_files = _object(manifest.get("files"), "CDK file assets")
        if not raw_files:
            raise ValueError("CDK file asset manifest is empty")
        manifests[stack] = {
            "artifact": artifact,
            "region": expected_region,
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        for asset_id in sorted(raw_files):
            if _CONTENT_HASH.fullmatch(asset_id) is None:
                raise ValueError("CDK file asset ID is invalid")
            asset = _object(raw_files[asset_id], "CDK file asset")
            if set(asset) != {"destinations", "displayName", "source"}:
                raise ValueError("CDK file asset fields are invalid")
            _string(asset, "displayName", "CDK file asset")
            source = _object(asset.get("source"), "CDK file asset source")
            if set(source) != {"packaging", "path"}:
                raise ValueError("CDK file asset source fields are invalid")
            source_path = _string(source, "path", "CDK file asset source")
            packaging = _string(source, "packaging", "CDK file asset source")
            if packaging not in {"file", "zip"}:
                raise ValueError("CDK file asset packaging is invalid")
            relative_path = PurePosixPath(source_path)
            if (
                relative_path.is_absolute()
                or relative_path.as_posix() != source_path
                or any(part in {"", ".", ".."} for part in relative_path.parts)
            ):
                raise ValueError("CDK file asset source path is unsafe")
            if require_sources:
                source_on_disk = assembly_dir.joinpath(*relative_path.parts)
                assembly_root = assembly_dir.resolve()
                try:
                    source_on_disk.resolve().relative_to(assembly_root)
                except ValueError:
                    raise ValueError("CDK file asset source escapes the assembly") from None
                _validate_single_part_asset_source(source_on_disk, packaging=packaging)
            destinations = _object(asset.get("destinations"), "CDK file asset destinations")
            if len(destinations) != 1:
                raise ValueError("CDK file asset must have exactly one destination")
            destination = _object(
                next(iter(destinations.values())),
                "CDK file asset destination",
            )
            if set(destination) != {"bucketName", "objectKey", "region"}:
                raise ValueError("CDK file asset destination fields are invalid")
            bucket = _string(destination, "bucketName", "CDK file asset destination")
            object_key = _string(destination, "objectKey", "CDK file asset destination")
            region = _string(destination, "region", "CDK file asset destination")
            expected_bucket = f"cdk-hnb659fds-assets-{account}-{expected_region}"
            if bucket != expected_bucket or region != expected_region:
                raise ValueError("CDK file asset destination is outside the expected environment")
            if re.fullmatch(rf"{asset_id}\.(?:json|zip)", object_key) is None:
                raise ValueError("CDK file asset object key is not content-addressed")
            if packaging == "zip" and object_key != f"{asset_id}.zip":
                raise ValueError("zipped CDK file asset must use a zip object key")
            files.append(
                {
                    "stack": stack,
                    "asset_id": asset_id,
                    "source_path": source_path,
                    "packaging": packaging,
                    "region": region,
                    "bucket": bucket,
                    "object_key": object_key,
                    "publisher": "current_credentials",
                    "s3_checksum_sha256": None,
                }
            )
    evidence = {
        "schema_version": 1,
        "manifests": manifests,
        "files": files,
    }
    validate_cdk_asset_evidence(evidence, account=account, require_checksums=False)
    return evidence


def validate_cdk_asset_evidence(
    value: object,
    *,
    account: str,
    require_checksums: bool = True,
) -> None:
    """Reject incomplete or widened CDK asset inventories."""

    if _ACCOUNT_ID.fullmatch(account) is None:
        raise ValueError("CDK asset account is invalid")
    root = _object(value, "CDK asset evidence")
    if set(root) != {"files", "manifests", "schema_version"} or root.get("schema_version") != 1:
        raise ValueError("CDK asset evidence schema is invalid")
    manifests = _object(root.get("manifests"), "CDK asset evidence manifests")
    if tuple(manifests) != _STACKS:
        raise ValueError("CDK asset evidence manifests are incomplete")
    expected_by_stack = {stack: (artifact, region) for stack, artifact, region in _CDK_ASSET_STACKS}
    for stack, raw_manifest in manifests.items():
        manifest = _object(raw_manifest, "CDK asset evidence manifest")
        if set(manifest) != {"artifact", "region", "sha256"}:
            raise ValueError("CDK asset evidence manifest fields are invalid")
        artifact, region = expected_by_stack[stack]
        if manifest.get("artifact") != artifact or manifest.get("region") != region:
            raise ValueError("CDK asset evidence manifest identity is invalid")
        if (
            not isinstance(value_hash := manifest.get("sha256"), str)
            or _CONTENT_HASH.fullmatch(value_hash) is None
        ):
            raise ValueError("CDK asset evidence manifest hash is invalid")
    files = _array(root.get("files"), "CDK asset evidence files")
    seen: set[tuple[str, str, str, str]] = set()
    stacks_with_template: set[str] = set()
    runtime_zip_found = False
    expected_fields = {
        "asset_id",
        "bucket",
        "object_key",
        "packaging",
        "publisher",
        "region",
        "s3_checksum_sha256",
        "source_path",
        "stack",
    }
    for raw_file in files:
        record = _object(raw_file, "CDK asset evidence file")
        if set(record) != expected_fields:
            raise ValueError("CDK asset evidence file fields are invalid")
        stack = _string(record, "stack", "CDK asset evidence file")
        if stack not in expected_by_stack:
            raise ValueError("CDK asset evidence stack is invalid")
        artifact, expected_region = expected_by_stack[stack]
        asset_id = _string(record, "asset_id", "CDK asset evidence file")
        if _CONTENT_HASH.fullmatch(asset_id) is None:
            raise ValueError("CDK asset evidence file ID is invalid")
        source_path = _string(record, "source_path", "CDK asset evidence file")
        relative_path = PurePosixPath(source_path)
        if (
            relative_path.is_absolute()
            or relative_path.as_posix() != source_path
            or any(part in {"", ".", ".."} for part in relative_path.parts)
        ):
            raise ValueError("CDK asset evidence source path is unsafe")
        packaging = _string(record, "packaging", "CDK asset evidence file")
        if packaging not in {"file", "zip"}:
            raise ValueError("CDK asset evidence packaging is invalid")
        region = _string(record, "region", "CDK asset evidence file")
        bucket = _string(record, "bucket", "CDK asset evidence file")
        object_key = _string(record, "object_key", "CDK asset evidence file")
        if region != expected_region or bucket != (
            f"cdk-hnb659fds-assets-{account}-{expected_region}"
        ):
            raise ValueError("CDK asset evidence destination is invalid")
        if record.get("publisher") != "current_credentials":
            raise ValueError("CDK asset evidence publisher is invalid")
        if re.fullmatch(rf"{asset_id}\.(?:json|zip)", object_key) is None:
            raise ValueError("CDK asset evidence object key is invalid")
        if packaging == "zip" and object_key != f"{asset_id}.zip":
            raise ValueError("CDK asset evidence zip object key is invalid")
        checksum = record.get("s3_checksum_sha256")
        if require_checksums:
            if not isinstance(checksum, str) or _S3_SHA256_CHECKSUM.fullmatch(checksum) is None:
                raise ValueError("CDK asset evidence S3 checksum is invalid")
        elif checksum is not None:
            raise ValueError("unpublished CDK asset evidence must not contain a checksum")
        identity = (stack, asset_id, bucket, object_key)
        if identity in seen:
            raise ValueError("CDK asset evidence file is duplicated")
        seen.add(identity)
        if source_path == f"{artifact}.template.json" and packaging == "file":
            if object_key != f"{asset_id}.json":
                raise ValueError("CDK template asset object key is invalid")
            stacks_with_template.add(stack)
        if (
            stack == "ShittimChest-Prod-Runtime"
            and packaging == "zip"
            and source_path == f"asset.{asset_id}"
        ):
            runtime_zip_found = True
    if stacks_with_template != set(_STACKS):
        raise ValueError("CDK template asset evidence is incomplete")
    if not runtime_zip_found:
        raise ValueError("Runtime CDK provider asset is missing")
    if len(files) != 5:
        raise ValueError("CDK asset evidence must contain exactly five files")


def bind_cdk_asset_checksums(
    value: object,
    *,
    account: str,
    assembly_dir: Path,
    checksums: object,
) -> dict[str, object]:
    """Bind S3 full-object checksums to the exact unpublished asset inventory."""

    validate_cdk_asset_evidence(value, account=account, require_checksums=False)
    root = _object(value, "CDK asset evidence")
    files = _array(root.get("files"), "CDK asset evidence files")
    checksum_records = _array(checksums, "CDK asset S3 checksums")
    if len(files) != len(checksum_records):
        raise ValueError("CDK asset S3 checksum inventory is incomplete")
    bound_files: list[dict[str, object]] = []
    for raw_file, raw_checksum in zip(files, checksum_records, strict=True):
        record = dict(_object(raw_file, "CDK asset evidence file"))
        checksum_record = _object(raw_checksum, "CDK asset S3 checksum")
        if set(checksum_record) != {
            "asset_id",
            "bucket",
            "checksum_sha256",
            "object_key",
            "region",
        }:
            raise ValueError("CDK asset S3 checksum fields are invalid")
        for key in ("asset_id", "bucket", "object_key", "region"):
            if checksum_record.get(key) != record.get(key):
                raise ValueError("CDK asset S3 checksum identity is invalid")
        checksum = _string(checksum_record, "checksum_sha256", "CDK asset S3 checksum")
        if _S3_SHA256_CHECKSUM.fullmatch(checksum) is None:
            raise ValueError("CDK asset S3 checksum is invalid")
        if record.get("packaging") == "file":
            source_path = PurePosixPath(_string(record, "source_path", "CDK asset evidence file"))
            source_on_disk = assembly_dir.joinpath(*source_path.parts)
            if not source_on_disk.is_file() or source_on_disk.is_symlink():
                raise ValueError("CDK file asset source does not exist safely")
            expected_checksum = base64.b64encode(
                hashlib.sha256(source_on_disk.read_bytes()).digest()
            ).decode("ascii")
            if checksum != expected_checksum:
                raise ValueError("CDK file asset S3 checksum does not match its source")
        record["s3_checksum_sha256"] = checksum
        bound_files.append(record)
    bound = {
        "schema_version": root["schema_version"],
        "manifests": dict(_object(root.get("manifests"), "CDK asset evidence manifests")),
        "files": bound_files,
    }
    validate_cdk_asset_evidence(bound, account=account)
    return bound


def validate_cdk_asset_evidence_against_assembly(
    value: object,
    *,
    account: str,
    assembly_dir: Path,
) -> None:
    """Bind downloaded evidence back to the exact asset manifests."""

    validate_cdk_asset_evidence(value, account=account)
    expected = create_cdk_asset_evidence(
        account=account,
        assembly_dir=assembly_dir,
        require_sources=False,
    )
    actual = dict(_object(value, "CDK asset evidence"))
    actual_files = []
    for raw_file in _array(actual.get("files"), "CDK asset evidence files"):
        record = dict(_object(raw_file, "CDK asset evidence file"))
        record["s3_checksum_sha256"] = None
        actual_files.append(record)
    actual["files"] = actual_files
    if actual != expected:
        raise ValueError("CDK asset evidence does not match the cloud assembly")


def create_manifest(
    *,
    cdk_assets: object,
    change_sets: object,
    commit_sha: str,
    lambda_bundle: object,
    normal_config_digest: str,
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
    validate_cdk_asset_evidence(cdk_assets, account=account)
    images = {
        "normal": _manifest_image(
            config_digest=normal_config_digest,
            repository_uri=repository_uri,
            risk_evidence=normal_risk_evidence,
            sbom_path=normal_sbom_path,
            verification=normal_verification,
        ),
    }
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
        "schema_version": 4,
        "repository": "pitekusu/shittim-chest",
        "workflow": ".github/workflows/release.yml",
        "commit_sha": commit_sha,
        "images": images,
        "templates": dict(template_hashes),
        "cdk_assets": cdk_assets,
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
        "cdk_assets",
        "change_sets",
        "lambda_bundle",
        "runtime_config_parameter",
    }
    if set(root) != expected or root.get("schema_version") != 4:
        raise ValueError("release manifest schema is invalid")
    if root.get("repository") != "pitekusu/shittim-chest":
        raise ValueError("release manifest repository is invalid")
    if root.get("workflow") != ".github/workflows/release.yml":
        raise ValueError("release manifest workflow is invalid")
    commit_sha = root.get("commit_sha")
    if not isinstance(commit_sha, str) or _SHA.fullmatch(commit_sha) is None:
        raise ValueError("release manifest commit is invalid")
    images = _object(root.get("images"), "release images")
    if tuple(images) != ("normal",):
        raise ValueError("release image order is invalid")
    validated_digests: list[str] = []
    release_account: str | None = None
    for name, raw_image in images.items():
        image = _object(raw_image, f"release image {name}")
        expected_image_fields = {
            "config_digest",
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
        config_digest = _string(image, "config_digest", "release image")
        _require_digest(config_digest)
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
    if len(validated_digests) != 1:
        raise ValueError("release image set is invalid")
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
    if release_account is None:
        raise ValueError("release account is missing")
    validate_cdk_asset_evidence(root.get("cdk_assets"), account=release_account)
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
    normal_digest: str,
    repository_uri: str,
) -> None:
    """Require every application task image in the runtime template to use one digest."""

    _require_digest(normal_digest)
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
            name = _string(container_record, "Name", "container")
            if name in images:
                raise ValueError("runtime task image container is duplicated")
            images[name] = container_record.get("Image")
    expected = {"application": f"{repository_uri}@{normal_digest}"}
    if images != expected:
        raise ValueError("runtime task images are not the exact release digest")


def _manifest_image(
    *,
    config_digest: str,
    repository_uri: str,
    risk_evidence: Mapping[str, Path],
    sbom_path: Path,
    verification: object,
) -> dict[str, object]:
    _require_digest(config_digest)
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
        "config_digest": config_digest,
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
    raw_parameters = record.get("Parameters")
    parameter_items: Sequence[object] = (
        () if raw_parameters is None else _array(raw_parameters, "change set parameters")
    )
    parameters: dict[str, str] = {}
    for item in parameter_items:
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
        "vulnerability_set_sha256",
    }:
        raise ValueError("release scan fields are invalid")
    if (
        scan.get("schema_version") != 2
        or scan.get("image_digest") != digest
        or scan.get("result") != "passed"
        or scan.get("risk_gate") != "passed"
        or scan.get("scanner") != "ECR_ENHANCED"
        or not isinstance(scan.get("scanned_at"), str)
        or not scan.get("scanned_at")
        or not isinstance(scan.get("vulnerability_set_sha256"), str)
        or _CONTENT_HASH.fullmatch(cast(str, scan["vulnerability_set_sha256"])) is None
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
    select_referrers = commands.add_parser("select-release-referrers")
    select_referrers.add_argument("--before-referrers", type=Path, required=True)
    select_referrers.add_argument("--after-referrers", type=Path, required=True)
    select_referrers.add_argument("--notation-inspection", type=Path, required=True)
    select_referrers.add_argument("--profile-arn", required=True)
    select_referrers.add_argument("--output", type=Path, required=True)
    predicate = commands.add_parser("create-vulnerability-predicate")
    predicate.add_argument("--digest", required=True)
    predicate.add_argument("--coverage", type=Path, required=True)
    predicate.add_argument("--scan", type=Path, required=True)
    predicate.add_argument("--risk-gate-passed", action="store_true")
    predicate.add_argument("--output", type=Path, required=True)
    create_assets = commands.add_parser("create-cdk-assets")
    create_assets.add_argument("--account", required=True)
    create_assets.add_argument("--assembly", type=Path, required=True)
    create_assets.add_argument("--output", type=Path, required=True)
    bind_assets = commands.add_parser("bind-cdk-asset-checksums")
    bind_assets.add_argument("evidence", type=Path)
    bind_assets.add_argument("--account", required=True)
    bind_assets.add_argument("--assembly", type=Path, required=True)
    bind_assets.add_argument("--checksums", type=Path, required=True)
    bind_assets.add_argument("--output", type=Path, required=True)
    validate_assets = commands.add_parser("validate-cdk-assets")
    validate_assets.add_argument("evidence", type=Path)
    validate_assets.add_argument("--account", required=True)
    validate_assets.add_argument("--assembly", type=Path, required=True)
    create = commands.add_parser("create-manifest")
    create.add_argument("--change-sets", type=Path, required=True)
    create.add_argument("--cdk-assets", type=Path, required=True)
    create.add_argument("--commit-sha", required=True)
    create.add_argument("--lambda-bundle", type=Path, required=True)
    create.add_argument("--repository-uri", required=True)
    create.add_argument("--runtime-config-parameter", required=True)
    create.add_argument("--normal-sbom", type=Path, required=True)
    create.add_argument("--normal-config-digest-file", type=Path, required=True)
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
        elif args.command == "select-release-referrers":
            _write(
                args.output,
                select_release_referrers(
                    before=_read(args.before_referrers),
                    after=_read(args.after_referrers),
                    notation_inspection=_read(args.notation_inspection),
                    profile_arn=args.profile_arn,
                ),
            )
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
        elif args.command == "create-cdk-assets":
            _write(
                args.output,
                create_cdk_asset_evidence(
                    account=args.account,
                    assembly_dir=args.assembly,
                ),
            )
        elif args.command == "bind-cdk-asset-checksums":
            _write(
                args.output,
                bind_cdk_asset_checksums(
                    _read(args.evidence),
                    account=args.account,
                    assembly_dir=args.assembly,
                    checksums=_read(args.checksums),
                ),
            )
        elif args.command == "validate-cdk-assets":
            validate_cdk_asset_evidence_against_assembly(
                _read(args.evidence),
                account=args.account,
                assembly_dir=args.assembly,
            )
        elif args.command == "create-manifest":
            result = create_manifest(
                cdk_assets=_read(args.cdk_assets),
                change_sets=_read(args.change_sets),
                commit_sha=args.commit_sha,
                lambda_bundle=_read(args.lambda_bundle),
                normal_config_digest=args.normal_config_digest_file.read_text(
                    encoding="ascii"
                ).strip(),
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
