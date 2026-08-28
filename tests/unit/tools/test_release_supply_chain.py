from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from tools.release_supply_chain import (
    bind_cdk_asset_checksums,
    create_cdk_asset_evidence,
    create_manifest,
    create_vulnerability_predicate,
    select_release_referrers,
    validate_cdk_asset_evidence,
    validate_cdk_asset_evidence_against_assembly,
    validate_change_set,
    validate_manifest,
    validate_runtime_template,
    verify_image_evidence,
)
from tools.release_supply_chain import (
    main as release_supply_chain_main,
)

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "1" * 64
PROFILE = "arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/shittim_chest_ecr"
REPOSITORY = "000000000000.dkr.ecr.ap-northeast-1.amazonaws.com/shittim-chest"
IMAGE_DETAILS = {
    "imageDetails": [
        {
            "imageDigest": DIGEST,
            "imageManifestMediaType": "application/vnd.oci.image.index.v1+json",
        }
    ]
}

CDK_STACKS = (
    ("ShittimChest-Prod-Stateful", "Stateful", "ap-northeast-1"),
    ("ShittimChest-Prod-Runtime", "Runtime", "ap-northeast-1"),
    ("ShittimChest-Prod-Operations", "Operations", "ap-northeast-1"),
    ("ShittimChest-Prod-CostGovernance", "CostGovernance", "us-east-1"),
)


def cdk_assets() -> dict[str, object]:
    manifests: dict[str, object] = {}
    files: list[dict[str, object]] = []
    for index, (stack, artifact_name, region) in enumerate(CDK_STACKS, start=1):
        asset_id = f"{index:x}" * 64
        bucket = f"cdk-hnb659fds-assets-000000000000-{region}"
        manifests[stack] = {
            "artifact": artifact_name,
            "region": region,
            "sha256": f"{index + 4:x}" * 64,
        }
        files.append(
            {
                "stack": stack,
                "asset_id": asset_id,
                "source_path": f"{artifact_name}.template.json",
                "packaging": "file",
                "region": region,
                "bucket": bucket,
                "object_key": f"{asset_id}.json",
                "publisher": "current_credentials",
                "s3_checksum_sha256": "A" * 43 + "=",
            }
        )
        if artifact_name == "Runtime":
            provider_id = "a" * 64
            files.append(
                {
                    "stack": stack,
                    "asset_id": provider_id,
                    "source_path": f"asset.{provider_id}",
                    "packaging": "zip",
                    "region": region,
                    "bucket": bucket,
                    "object_key": f"{provider_id}.zip",
                    "publisher": "current_credentials",
                    "s3_checksum_sha256": "B" * 43 + "=",
                }
            )
    return {"schema_version": 1, "manifests": manifests, "files": files}


def write_cdk_assembly(path: Path, *, include_runtime_provider: bool = True) -> None:
    path.mkdir()
    for index, (_, artifact_name, region) in enumerate(CDK_STACKS, start=1):
        template_id = f"{index:x}" * 64
        template_name = f"{artifact_name}.template.json"
        (path / template_name).write_text("{}\n", encoding="utf-8")
        files: dict[str, object] = {
            template_id: {
                "displayName": f"{artifact_name} Template",
                "source": {"path": template_name, "packaging": "file"},
                "destinations": {
                    f"destination-{index}": {
                        "bucketName": f"cdk-hnb659fds-assets-000000000000-{region}",
                        "objectKey": f"{template_id}.json",
                        "region": region,
                    }
                },
            }
        }
        if artifact_name == "Runtime" and include_runtime_provider:
            provider_id = "a" * 64
            (path / f"asset.{provider_id}").mkdir()
            files[provider_id] = {
                "displayName": "Runtime Provider",
                "source": {"path": f"asset.{provider_id}", "packaging": "zip"},
                "destinations": {
                    "provider-destination": {
                        "bucketName": f"cdk-hnb659fds-assets-000000000000-{region}",
                        "objectKey": f"{provider_id}.zip",
                        "region": region,
                    }
                },
            }
        (path / f"{artifact_name}.assets.json").write_text(
            json.dumps({"version": "54.0.0", "files": files, "dockerImages": {}}),
            encoding="utf-8",
        )


def asset_checksums(evidence: dict[str, object], assembly: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for raw_file in cast(list[dict[str, object]], evidence["files"]):
        if raw_file["packaging"] == "file":
            checksum = base64.b64encode(
                hashlib.sha256(
                    (assembly / cast(str, raw_file["source_path"])).read_bytes()
                ).digest()
            ).decode("ascii")
        else:
            checksum = "B" * 43 + "="
        records.append(
            {
                "asset_id": cast(str, raw_file["asset_id"]),
                "bucket": cast(str, raw_file["bucket"]),
                "checksum_sha256": checksum,
                "object_key": cast(str, raw_file["object_key"]),
                "region": cast(str, raw_file["region"]),
            }
        )
    return records


def test_inventory_covers_every_cdk_file_asset_and_binds_the_assembly(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)

    unpublished = create_cdk_asset_evidence(
        account="000000000000",
        assembly_dir=assembly,
    )
    result = bind_cdk_asset_checksums(
        unpublished,
        account="000000000000",
        assembly_dir=assembly,
        checksums=asset_checksums(unpublished, assembly),
    )

    validate_cdk_asset_evidence(result, account="000000000000")
    validate_cdk_asset_evidence_against_assembly(
        result,
        account="000000000000",
        assembly_dir=assembly,
    )
    assert len(cast(list[object], result["files"])) == 5
    assert {
        cast(dict[str, object], item)["object_key"] for item in cast(list[object], result["files"])
    } == {
        "1" * 64 + ".json",
        "2" * 64 + ".json",
        "3" * 64 + ".json",
        "4" * 64 + ".json",
        "a" * 64 + ".zip",
    }


def test_rejects_docker_assets_and_a_missing_runtime_provider(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly, include_runtime_provider=False)

    with pytest.raises(ValueError, match="provider asset is missing"):
        create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)

    runtime_manifest = assembly / "Runtime.assets.json"
    payload = json.loads(runtime_manifest.read_text(encoding="utf-8"))
    payload["dockerImages"] = {"unexpected": {}}
    runtime_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Docker assets"):
        create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)


def test_rejects_a_cdk_asset_destination_that_assumes_a_publisher_role(
    tmp_path: Path,
) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    manifest = assembly / "Runtime.assets.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    destination = next(iter(next(iter(payload["files"].values()))["destinations"].values()))
    destination["assumeRoleArn"] = (
        "arn:aws:iam::000000000000:role/cdk-hnb659fds-file-publishing-role-"
        "000000000000-ap-northeast-1"
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="destination fields"):
        create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)


def test_rejects_an_asset_closure_beyond_the_reviewed_five_files(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    extra_id = "b" * 64
    (assembly / "Extra.template.json").write_text("{}\n", encoding="utf-8")
    manifest = assembly / "Stateful.assets.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["files"][extra_id] = {
        "displayName": "Unexpected Asset",
        "source": {"path": "Extra.template.json", "packaging": "file"},
        "destinations": {
            "unexpected": {
                "bucketName": "cdk-hnb659fds-assets-000000000000-ap-northeast-1",
                "objectKey": f"{extra_id}.json",
                "region": "ap-northeast-1",
            }
        },
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exactly five"):
        create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)


def test_rejects_a_zip_source_that_could_require_a_multipart_checksum(
    tmp_path: Path,
) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    provider = assembly / f"asset.{'a' * 64}" / "provider.js"
    provider.write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(ValueError, match="single-part checksum boundary"):
        create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)


def test_rejects_tampered_cdk_asset_evidence(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    unpublished = create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)
    result = bind_cdk_asset_checksums(
        unpublished,
        account="000000000000",
        assembly_dir=assembly,
        checksums=asset_checksums(unpublished, assembly),
    )
    tampered = deepcopy(result)
    cast(list[dict[str, object]], tampered["files"])[0]["object_key"] = "f" * 64 + ".json"

    with pytest.raises(ValueError, match=r"object key|does not match"):
        validate_cdk_asset_evidence_against_assembly(
            tampered,
            account="000000000000",
            assembly_dir=assembly,
        )


def test_rejects_a_remote_checksum_that_does_not_match_a_file_asset(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    unpublished = create_cdk_asset_evidence(account="000000000000", assembly_dir=assembly)
    checksums = asset_checksums(unpublished, assembly)
    checksums[0]["checksum_sha256"] = "C" * 43 + "="

    with pytest.raises(ValueError, match="does not match its source"):
        bind_cdk_asset_checksums(
            unpublished,
            account="000000000000",
            assembly_dir=assembly,
            checksums=checksums,
        )


def test_cdk_asset_cli_round_trip_matches_the_artifact_layout(tmp_path: Path) -> None:
    assembly = tmp_path / "cdk.out"
    write_cdk_assembly(assembly)
    unpublished_path = tmp_path / "cdk-assets.unpublished.json"
    checksums_path = tmp_path / "cdk-asset-checksums.json"
    evidence_path = tmp_path / "cdk-assets.json"

    assert (
        release_supply_chain_main(
            (
                "create-cdk-assets",
                "--account",
                "000000000000",
                "--assembly",
                str(assembly),
                "--output",
                str(unpublished_path),
            )
        )
        == 0
    )
    unpublished = json.loads(unpublished_path.read_text(encoding="utf-8"))
    checksums_path.write_text(
        json.dumps(asset_checksums(unpublished, assembly)),
        encoding="utf-8",
    )
    assert (
        release_supply_chain_main(
            (
                "bind-cdk-asset-checksums",
                str(unpublished_path),
                "--account",
                "000000000000",
                "--assembly",
                str(assembly),
                "--checksums",
                str(checksums_path),
                "--output",
                str(evidence_path),
            )
        )
        == 0
    )
    assert (
        release_supply_chain_main(
            (
                "validate-cdk-assets",
                str(evidence_path),
                "--account",
                "000000000000",
                "--assembly",
                str(assembly),
            )
        )
        == 0
    )


def enhanced_finding(*, severity: str, suffix: str) -> dict[str, object]:
    return {
        "findingArn": (
            "arn:aws:inspector2:ap-northeast-1:000000000000:finding/"
            f"00000000-0000-0000-0000-00000000000{suffix}"
        ),
        "packageVulnerabilityDetails": {"vulnerabilityId": f"CVE-2026-000{suffix}"},
        "severity": severity,
        "status": "ACTIVE",
    }


def evidence() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    signing = {"signingStatuses": [{"signingProfileArn": PROFILE, "status": "COMPLETE"}]}
    referrers = {
        "referrers": [
            artifact("application/vnd.cncf.notary.signature", "1"),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "2",
                "https://slsa.dev/provenance/v1",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "3",
                "https://spdx.dev/Document/v2.3",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "4",
                "https://github.com/pitekusu/shittim-chest/attestations/"
                "vulnerability-assessment/v1",
            ),
        ]
    }
    scan = {
        "imageScanStatus": {"status": "ACTIVE"},
        "imageScanFindings": {
            "enhancedFindings": [
                enhanced_finding(severity="MEDIUM", suffix="1"),
                enhanced_finding(severity="LOW", suffix="2"),
                enhanced_finding(severity="MEDIUM", suffix="3"),
                enhanced_finding(severity="LOW", suffix="4"),
                enhanced_finding(severity="LOW", suffix="5"),
            ],
            "findingSeverityCounts": {"MEDIUM": 2, "LOW": 3},
            "imageScanCompletedAt": "2026-07-29T00:00:00Z",
        },
    }
    return signing, referrers, scan


def inspector_coverage(*, digest: str = DIGEST, reason: str = "SUCCESSFUL") -> dict[str, object]:
    return {
        "coveredResources": [
            {
                "lastScannedAt": "2026-07-30T00:00:00Z",
                "resourceId": f"arn:aws:ecr:ap-northeast-1:000000000000:image/{digest}",
                "resourceType": "AWS_ECR_CONTAINER_IMAGE",
                "scanStatus": {"reason": reason, "statusCode": "ACTIVE"},
                "scanType": "PACKAGE",
            }
        ]
    }


def artifact(kind: str, suffix: str, predicate: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "artifactStatus": "ACTIVE",
        "artifactType": kind,
        "digest": "sha256:" + suffix * 64,
    }
    if predicate is not None:
        value["annotations"] = {"dev.sigstore.bundle.predicateType": predicate}
    return value


def referrer_snapshots() -> tuple[dict[str, object], dict[str, object]]:
    before = {
        "referrers": [
            artifact("application/vnd.cncf.notary.signature", "1"),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "2",
                "https://slsa.dev/provenance/v1",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "3",
                "https://spdx.dev/Document/v2.3",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "4",
                "https://github.com/pitekusu/shittim-chest/attestations/"
                "vulnerability-assessment/v1",
            ),
        ]
    }
    after = deepcopy(before)
    cast(list[dict[str, object]], after["referrers"]).extend(
        (
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "5",
                "https://slsa.dev/provenance/v1",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "6",
                "https://spdx.dev/Document/v2.3",
            ),
            artifact(
                "application/vnd.dev.sigstore.bundle.v0.3+json",
                "7",
                "https://github.com/pitekusu/shittim-chest/attestations/"
                "vulnerability-assessment/v1",
            ),
        )
    )
    return before, after


def notation_inspection(
    *suffixes: str,
    profile_arn: str = PROFILE,
) -> dict[str, object]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "Signatures": [
            {
                "digest": "sha256:" + suffix * 64,
                "signedAttributes": {
                    "com.amazonaws.signer.signingProfileVersion": f"{profile_arn}/ABCDEFGHIJ"
                },
            }
            for suffix in suffixes
        ],
    }


def select_referrer_fixture(
    *,
    before: object,
    after: object,
    inspection: object | None = None,
    profile_arn: str = PROFILE,
) -> dict[str, object]:
    return select_release_referrers(
        before=before,
        after=after,
        notation_inspection=inspection or notation_inspection("1"),
        profile_arn=profile_arn,
    )


def test_selects_only_this_runs_three_sigstore_referrers() -> None:
    before, after = referrer_snapshots()

    selected = select_referrer_fixture(before=before, after=after)

    selected_digests = [
        cast(dict[str, object], item)["digest"]
        for item in cast(list[object], selected["referrers"])
    ]
    assert selected_digests == [
        "sha256:" + "1" * 64,
        "sha256:" + "5" * 64,
        "sha256:" + "6" * 64,
        "sha256:" + "7" * 64,
    ]
    signing, _, scan = evidence()
    result = verify_image_evidence(
        digest=DIGEST,
        image_details=IMAGE_DETAILS,
        profile_arn=PROFILE,
        signing_status=signing,
        referrers=selected,
        scan=scan,
    )
    assert result["referrers"] == {
        "signature": "sha256:" + "1" * 64,
        "provenance": "sha256:" + "5" * 64,
        "sbom": "sha256:" + "6" * 64,
        "vulnerability": "sha256:" + "7" * 64,
    }


def test_referrer_delta_selects_one_stable_existing_signature() -> None:
    before, after = referrer_snapshots()
    older_signature = artifact("application/vnd.cncf.notary.signature", "0")
    cast(list[dict[str, object]], before["referrers"]).insert(0, older_signature)
    cast(list[dict[str, object]], after["referrers"]).append(deepcopy(older_signature))

    selected = select_referrer_fixture(
        before=before,
        after=after,
        inspection=notation_inspection("1", "0"),
    )

    selected_digests = [
        cast(dict[str, object], item)["digest"]
        for item in cast(list[object], selected["referrers"])
    ]
    assert selected_digests == [
        "sha256:" + "0" * 64,
        "sha256:" + "5" * 64,
        "sha256:" + "6" * 64,
        "sha256:" + "7" * 64,
    ]


def test_referrer_delta_rejects_a_missing_notation_signature() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], before["referrers"]).pop(0)
    cast(list[dict[str, object]], after["referrers"]).pop(0)

    with pytest.raises(ValueError, match="at least one active Notation signature"):
        select_referrer_fixture(
            before=before,
            after=after,
            inspection=notation_inspection(),
        )


def test_referrer_delta_selects_only_a_signature_from_the_expected_profile() -> None:
    before, after = referrer_snapshots()
    other_signature = artifact("application/vnd.cncf.notary.signature", "0")
    cast(list[dict[str, object]], before["referrers"]).insert(0, other_signature)
    cast(list[dict[str, object]], after["referrers"]).append(deepcopy(other_signature))
    inspection = notation_inspection("1")
    other_inspection = notation_inspection(
        "0",
        profile_arn="arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/other",
    )
    cast(list[dict[str, object]], inspection["Signatures"]).append(
        cast(list[dict[str, object]], other_inspection["Signatures"])[0]
    )

    selected = select_referrer_fixture(before=before, after=after, inspection=inspection)

    assert cast(list[dict[str, object]], selected["referrers"])[0]["digest"] == (
        "sha256:" + "1" * 64
    )


def test_referrer_delta_rejects_inspection_without_the_expected_profile() -> None:
    before, after = referrer_snapshots()

    with pytest.raises(ValueError, match="matches the expected signing profile"):
        select_referrer_fixture(
            before=before,
            after=after,
            inspection=notation_inspection(
                "1",
                profile_arn="arn:aws:signer:ap-northeast-1:000000000000:/signing-profiles/other",
            ),
        )


def test_referrer_delta_cli_writes_the_selected_snapshot(tmp_path: Path) -> None:
    before, after = referrer_snapshots()
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    inspection_path = tmp_path / "notation.json"
    output_path = tmp_path / "selected.json"
    before_path.write_text(json.dumps(before), encoding="utf-8")
    after_path.write_text(json.dumps(after), encoding="utf-8")
    inspection_path.write_text(json.dumps(notation_inspection("1")), encoding="utf-8")

    assert (
        release_supply_chain_main(
            (
                "select-release-referrers",
                "--before-referrers",
                str(before_path),
                "--after-referrers",
                str(after_path),
                "--notation-inspection",
                str(inspection_path),
                "--profile-arn",
                PROFILE,
                "--output",
                str(output_path),
            )
        )
        == 0
    )
    selected = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(selected["referrers"]) == 4


@pytest.mark.parametrize("snapshot_name", ("before", "after"))
def test_referrer_delta_rejects_incomplete_pagination(snapshot_name: str) -> None:
    before, after = referrer_snapshots()
    snapshot = before if snapshot_name == "before" else after
    snapshot["nextToken"] = "more"

    with pytest.raises(ValueError, match="not fully paginated"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_a_missing_new_predicate() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], after["referrers"]).pop()

    with pytest.raises(ValueError, match="exactly three new"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_an_extra_new_referrer() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], after["referrers"]).append(
        artifact("application/vnd.dev.sigstore.bundle.v0.3+json", "8", "unknown")
    )

    with pytest.raises(ValueError, match="exactly three new"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_a_duplicated_new_predicate() -> None:
    before, after = referrer_snapshots()
    added = cast(list[dict[str, object]], after["referrers"])[-3:]
    added[1]["annotations"] = {
        "dev.sigstore.bundle.predicateType": "https://slsa.dev/provenance/v1"
    }

    with pytest.raises(ValueError, match="predicate is duplicated"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_an_unknown_new_predicate() -> None:
    before, after = referrer_snapshots()
    added = cast(list[dict[str, object]], after["referrers"])[-3:]
    added[1]["annotations"] = {"dev.sigstore.bundle.predicateType": "unknown"}

    with pytest.raises(ValueError, match="unknown predicate"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_a_new_non_sigstore_referrer() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], after["referrers"])[-1]["artifactType"] = "unknown"

    with pytest.raises(ValueError, match="not a Sigstore bundle"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_duplicate_digests() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], after["referrers"]).append(
        deepcopy(cast(list[dict[str, object]], after["referrers"])[-1])
    )

    with pytest.raises(ValueError, match="duplicate referrer digest"):
        select_referrer_fixture(before=before, after=after)


def test_referrer_delta_rejects_a_disappearing_existing_referrer() -> None:
    before, after = referrer_snapshots()
    cast(list[dict[str, object]], after["referrers"]).pop(1)

    with pytest.raises(ValueError, match="existing active referrer disappeared"):
        select_referrer_fixture(before=before, after=after)


def test_verifies_exact_managed_signature_referrers_and_scan() -> None:
    signing, referrers, scan = evidence()

    result = verify_image_evidence(
        digest=DIGEST,
        image_details=IMAGE_DETAILS,
        profile_arn=PROFILE,
        signing_status=signing,
        referrers=referrers,
        scan=scan,
    )

    assert result["referrers"] == {
        "signature": "sha256:" + "1" * 64,
        "provenance": "sha256:" + "2" * 64,
        "sbom": "sha256:" + "3" * 64,
        "vulnerability": "sha256:" + "4" * 64,
    }
    scan_result = cast(dict[str, object], result["scan"])
    vulnerability_set_sha256 = scan_result.pop("vulnerability_set_sha256")
    assert isinstance(vulnerability_set_sha256, str)
    assert re.fullmatch(r"[0-9a-f]{64}", vulnerability_set_sha256)
    assert scan_result == {
        "schema_version": 2,
        "image_digest": DIGEST,
        "result": "passed",
        "risk_gate": "passed",
        "scanned_at": "2026-07-29T00:00:00Z",
        "scanner": "ECR_ENHANCED",
        "severity_counts": {
            "critical": 0,
            "high": 0,
            "medium": 2,
            "low": 3,
            "informational": 0,
            "undefined": 0,
        },
    }


def test_rejects_versioned_signing_profile_arn() -> None:
    signing, referrers, scan = evidence()

    with pytest.raises(ValueError, match="signing profile ARN is invalid"):
        verify_image_evidence(
            digest=DIGEST,
            image_details=IMAGE_DETAILS,
            profile_arn=f"{PROFILE}/ABCDEFGHIJ",
            signing_status=signing,
            referrers=referrers,
            scan=scan,
        )


@pytest.mark.parametrize("missing_index", range(4))
def test_rejects_every_missing_referrer(missing_index: int) -> None:
    signing, referrers, scan = evidence()
    cast(list[dict[str, object]], referrers["referrers"]).pop(missing_index)

    with pytest.raises(ValueError, match="exactly one active"):
        verify_image_evidence(
            digest=DIGEST,
            image_details=IMAGE_DETAILS,
            profile_arn=PROFILE,
            signing_status=signing,
            referrers=referrers,
            scan=scan,
        )


def test_rejects_incomplete_signing_and_high_findings() -> None:
    signing, referrers, scan = evidence()
    cast(list[dict[str, object]], signing["signingStatuses"])[0]["status"] = "IN_PROGRESS"
    with pytest.raises(ValueError, match="not complete"):
        verify_image_evidence(
            digest=DIGEST,
            image_details=IMAGE_DETAILS,
            profile_arn=PROFILE,
            signing_status=signing,
            referrers=referrers,
            scan=scan,
        )


def test_vulnerability_predicate_is_content_free() -> None:
    _, _, scan = evidence()

    result = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    assert set(result) == {
        "schema_version",
        "image_digest",
        "result",
        "risk_gate",
        "scanned_at",
        "scanner",
        "severity_counts",
        "vulnerability_set_sha256",
    }
    serialized = json.dumps(result).lower()
    assert "cve-" not in serialized
    assert "finding/" not in serialized

    signing, referrers, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    cast(dict[str, object], findings["findingSeverityCounts"])["HIGH"] = 1
    cast(list[dict[str, object]], findings["enhancedFindings"]).append(
        enhanced_finding(severity="HIGH", suffix="6")
    )
    with pytest.raises(ValueError, match="high or critical"):
        verify_image_evidence(
            digest=DIGEST,
            image_details=IMAGE_DETAILS,
            profile_arn=PROFILE,
            signing_status=signing,
            referrers=referrers,
            scan=scan,
        )

    accepted = create_vulnerability_predicate(
        digest=DIGEST,
        risk_gate_passed=True,
        scan=scan,
    )
    assert cast(dict[str, int], accepted["severity_counts"])["high"] == 1


def test_vulnerability_set_fingerprint_binds_current_finding_identity() -> None:
    _, _, scan = evidence()
    original = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    findings = cast(dict[str, object], scan["imageScanFindings"])
    first = cast(list[dict[str, object]], findings["enhancedFindings"])[0]
    details = cast(dict[str, object], first["packageVulnerabilityDetails"])
    details["vulnerabilityId"] = "CVE-2026-9999"
    changed = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    assert changed["severity_counts"] == original["severity_counts"]
    assert changed["vulnerability_set_sha256"] != original["vulnerability_set_sha256"]


def test_vulnerability_set_fingerprint_ignores_finding_order() -> None:
    _, _, scan = evidence()
    original = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    findings = cast(dict[str, object], scan["imageScanFindings"])
    cast(list[dict[str, object]], findings["enhancedFindings"]).reverse()
    reordered = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    assert reordered["vulnerability_set_sha256"] == original["vulnerability_set_sha256"]


def test_vulnerability_predicate_normalizes_untriaged_severity() -> None:
    _, _, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    counts = cast(dict[str, object], findings["findingSeverityCounts"])
    counts["LOW"] = 2
    counts["UNTRIAGED"] = 1
    second = cast(list[dict[str, object]], findings["enhancedFindings"])[1]
    second["severity"] = "UNTRIAGED"

    result = create_vulnerability_predicate(digest=DIGEST, scan=scan)

    assert cast(dict[str, int], result["severity_counts"])["undefined"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda counts: counts.update(TOTAL=5),
        lambda counts: counts.update(UNDEFINED=0, UNTRIAGED=0),
    ],
)
def test_rejects_unknown_or_ambiguous_severity_count_keys(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    _, _, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    counts = cast(dict[str, object], findings["findingSeverityCounts"])
    mutation(counts)

    with pytest.raises(ValueError, match="severity count keys"):
        create_vulnerability_predicate(digest=DIGEST, scan=scan)


def test_rejects_incomplete_or_count_mismatched_enhanced_findings() -> None:
    _, _, scan = evidence()
    scan["nextToken"] = "not-fully-paginated"
    with pytest.raises(ValueError, match="fully paginated"):
        create_vulnerability_predicate(digest=DIGEST, scan=scan)

    scan.pop("nextToken")
    findings = cast(dict[str, object], scan["imageScanFindings"])
    cast(list[dict[str, object]], findings["enhancedFindings"]).pop()
    with pytest.raises(ValueError, match="do not match severity counts"):
        create_vulnerability_predicate(digest=DIGEST, scan=scan)


def test_uses_successful_inspector_coverage_timestamp_for_zero_findings() -> None:
    _, _, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    findings.pop("imageScanCompletedAt")
    findings.pop("findingSeverityCounts")
    findings["enhancedFindings"] = []

    result = create_vulnerability_predicate(coverage=inspector_coverage(), digest=DIGEST, scan=scan)

    assert result["scanned_at"] == "2026-07-30T00:00:00Z"
    assert set(cast(dict[str, int], result["severity_counts"]).values()) == {0}


@pytest.mark.parametrize(
    ("coverage", "message"),
    [
        (inspector_coverage(digest=OTHER_DIGEST), "does not resolve"),
        (inspector_coverage(reason="PENDING_INITIAL_SCAN"), "is not successful"),
        ({"coveredResources": []}, "does not resolve"),
    ],
)
def test_rejects_unbound_or_incomplete_inspector_coverage(
    coverage: dict[str, object], message: str
) -> None:
    _, _, scan = evidence()
    cast(dict[str, object], scan["imageScanFindings"]).pop("imageScanCompletedAt")

    with pytest.raises(ValueError, match=message):
        create_vulnerability_predicate(coverage=coverage, digest=DIGEST, scan=scan)


def test_rejects_unsuccessful_coverage_even_when_ecr_has_a_timestamp() -> None:
    _, _, scan = evidence()

    with pytest.raises(ValueError, match="is not successful"):
        create_vulnerability_predicate(
            coverage=inspector_coverage(reason="PENDING_INITIAL_SCAN"),
            digest=DIGEST,
            scan=scan,
        )


def manifest(tmp_path: Path) -> dict[str, object]:
    signing, referrers, scan = evidence()
    verification = verify_image_evidence(
        digest=DIGEST,
        image_details=IMAGE_DETAILS,
        profile_arn=PROFILE,
        signing_status=signing,
        referrers=referrers,
        scan=scan,
    )
    normal_sbom = tmp_path / "normal.spdx.json"
    normal_sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    risk_paths: dict[str, Path] = {}
    for key in ("grype_raw", "grype_vex", "vendor_vex"):
        path = tmp_path / f"normal-{key}.json"
        path.write_text(f'{{"kind":"{key}","mode":"normal"}}\n', encoding="utf-8")
        risk_paths[key] = path
    stacks = (
        "ShittimChest-Prod-Stateful",
        "ShittimChest-Prod-Runtime",
        "ShittimChest-Prod-Operations",
        "ShittimChest-Prod-CostGovernance",
    )
    changes = {}
    for stack in stacks:
        region = "us-east-1" if stack.endswith("CostGovernance") else "ap-northeast-1"
        changes[stack] = (
            f"arn:aws:cloudformation:{region}:000000000000:"
            f"changeSet/release-{stack}/00000000-0000-0000-0000-000000000000"
        )
    return create_manifest(
        cdk_assets=cdk_assets(),
        change_sets=changes,
        commit_sha="b" * 40,
        lambda_bundle={
            "bucket": "cdk-hnb659fds-assets-000000000000-ap-northeast-1",
            "key": f"lambda/shittim-chest/{'c' * 64}/shittim-chest-lambda-arm64.zip",
            "sha256": "c" * 64,
        },
        normal_config_digest=CONFIG_DIGEST,
        normal_sbom_path=normal_sbom,
        normal_risk_evidence=risk_paths,
        normal_verification=verification,
        repository_uri=REPOSITORY,
        runtime_config_parameter="/shittim-chest/production/runtime/v0001",
        templates={stack: "d" * 64 for stack in stacks},
    )


def test_manifest_binds_all_immutable_release_outputs(tmp_path: Path) -> None:
    value = manifest(tmp_path)

    validate_manifest(value)

    images = cast(dict[str, dict[str, object]], value["images"])
    assert images["normal"] | {
        "referrers": None,
        "risk_evidence": None,
        "sbom": None,
        "scan": None,
        "signing_profile_arn": None,
    } == {
        "config_digest": CONFIG_DIGEST,
        "digest": DIGEST,
        "media_type": "application/vnd.oci.image.index.v1+json",
        "reference": f"{REPOSITORY}@{DIGEST}",
        "repository_uri": REPOSITORY,
        "referrers": None,
        "risk_evidence": None,
        "sbom": None,
        "scan": None,
        "signing_profile_arn": None,
    }
    assert tuple(images) == ("normal",)
    assert value["commit_sha"] == "b" * 40
    assert value["schema_version"] == 4
    assert value["cdk_assets"] == cdk_assets()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(commit_sha="wrong"),
        lambda value: value.update(schema_version=3),
        lambda value: value["images"].update(  # type: ignore[index]
            {"break_glass": value["images"]["normal"]}  # type: ignore[index]
        ),
        lambda value: value["images"]["normal"].update(  # type: ignore[index]
            reference=f"{REPOSITORY}:latest"
        ),
        lambda value: value["images"]["normal"].update(config_digest="not-a-digest"),  # type: ignore[index]
        lambda value: value["images"]["normal"]["referrers"].pop("sbom"),  # type: ignore[index]
        lambda value: value["images"]["normal"]["scan"].update(  # type: ignore[index]
            vulnerability_set_sha256="not-a-hash"
        ),
        lambda value: value["change_sets"].update({"ShittimChest-Prod-Runtime": "not-an-arn"}),
        lambda value: value["cdk_assets"]["files"].pop(),  # type: ignore[index]
    ],
)
def test_manifest_validation_rejects_tampering(
    tmp_path: Path, mutation: Callable[[dict[str, object]], None]
) -> None:
    value = deepcopy(manifest(tmp_path))
    mutation(value)

    with pytest.raises(ValueError):
        validate_manifest(value)


def test_runtime_template_requires_only_the_exact_production_digest_image() -> None:
    value = {
        "Resources": {
            "Normal": task("application", f"{REPOSITORY}@{DIGEST}"),
        }
    }

    validate_runtime_template(
        value,
        normal_digest=DIGEST,
        repository_uri=REPOSITORY,
    )

    value["Resources"]["Normal"] = task("application", f"{REPOSITORY}:latest")
    with pytest.raises(ValueError, match="exact release digest"):
        validate_runtime_template(
            value,
            normal_digest=DIGEST,
            repository_uri=REPOSITORY,
        )

    value["Resources"]["Normal"] = task("application", f"{REPOSITORY}@{DIGEST}")
    value["Resources"]["RemovedEmergencyTask"] = task(
        "break-glass-application", f"{REPOSITORY}@{OTHER_DIGEST}"
    )
    with pytest.raises(ValueError, match="exact release digest"):
        validate_runtime_template(
            value,
            normal_digest=DIGEST,
            repository_uri=REPOSITORY,
        )


def test_change_set_binds_identity_status_and_exact_parameters() -> None:
    arn = (
        "arn:aws:cloudformation:ap-northeast-1:000000000000:"
        "changeSet/release-example/00000000-0000-0000-0000-000000000000"
    )
    value = {
        "ChangeSetId": arn,
        "ExecutionStatus": "AVAILABLE",
        "Parameters": [
            {"ParameterKey": "RuntimeImageDigest", "ParameterValue": DIGEST},
            {"ParameterKey": "OperatorNotificationEmail", "ParameterValue": "*****"},
            {"ParameterKey": "BootstrapVersion", "ParameterValue": "/cdk-bootstrap/value"},
        ],
        "StackName": "ShittimChest-Prod-Runtime",
        "Status": "CREATE_COMPLETE",
    }

    validate_change_set(
        value,
        expected_arn=arn,
        expected_noecho_parameters=("OperatorNotificationEmail",),
        expected_parameters={"RuntimeImageDigest": DIGEST},
        expected_stack="ShittimChest-Prod-Runtime",
    )

    value["Parameters"][0]["ParameterValue"] = "sha256:" + "f" * 64  # type: ignore[index]
    with pytest.raises(ValueError, match="RuntimeImageDigest"):
        validate_change_set(
            value,
            expected_arn=arn,
            expected_noecho_parameters=("OperatorNotificationEmail",),
            expected_parameters={"RuntimeImageDigest": DIGEST},
            expected_stack="ShittimChest-Prod-Runtime",
        )

    value["Parameters"][0]["ParameterValue"] = DIGEST  # type: ignore[index]
    value["Parameters"][1]["ParameterValue"] = "not-redacted"  # type: ignore[index]
    with pytest.raises(ValueError, match="NoEcho"):
        validate_change_set(
            value,
            expected_arn=arn,
            expected_noecho_parameters=("OperatorNotificationEmail",),
            expected_parameters={"RuntimeImageDigest": DIGEST},
            expected_stack="ShittimChest-Prod-Runtime",
        )


@pytest.mark.parametrize("parameters", [None, pytest.param("missing", id="missing")])
def test_change_set_accepts_no_parameter_collection_only_when_none_are_expected(
    parameters: object,
) -> None:
    arn = (
        "arn:aws:cloudformation:ap-northeast-1:000000000000:"
        "changeSet/release-example/00000000-0000-0000-0000-000000000000"
    )
    value: dict[str, object] = {
        "ChangeSetId": arn,
        "ExecutionStatus": "AVAILABLE",
        "StackName": "ShittimChest-Prod-Stateful",
        "Status": "CREATE_COMPLETE",
    }
    if parameters != "missing":
        value["Parameters"] = parameters

    validate_change_set(
        value,
        expected_arn=arn,
        expected_parameters={},
        expected_stack="ShittimChest-Prod-Stateful",
    )

    with pytest.raises(ValueError, match="RuntimeImageDigest"):
        validate_change_set(
            value,
            expected_arn=arn,
            expected_parameters={"RuntimeImageDigest": DIGEST},
            expected_stack="ShittimChest-Prod-Stateful",
        )
    with pytest.raises(ValueError, match="NoEcho"):
        validate_change_set(
            value,
            expected_arn=arn,
            expected_noecho_parameters=("OperatorNotificationEmail",),
            expected_parameters={},
            expected_stack="ShittimChest-Prod-Stateful",
        )


@pytest.mark.parametrize("parameters", [{}, "not-an-array"])
def test_change_set_rejects_a_non_array_parameter_collection(parameters: object) -> None:
    arn = (
        "arn:aws:cloudformation:ap-northeast-1:000000000000:"
        "changeSet/release-example/00000000-0000-0000-0000-000000000000"
    )

    with pytest.raises(ValueError, match="parameters must be an array"):
        validate_change_set(
            {
                "ChangeSetId": arn,
                "ExecutionStatus": "AVAILABLE",
                "Parameters": parameters,
                "StackName": "ShittimChest-Prod-Stateful",
                "Status": "CREATE_COMPLETE",
            },
            expected_arn=arn,
            expected_parameters={},
            expected_stack="ShittimChest-Prod-Stateful",
        )


def task(name: str, image: str) -> dict[str, object]:
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Properties": {"ContainerDefinitions": [{"Name": name, "Image": image}]},
    }


def test_manifest_is_json_serializable(tmp_path: Path) -> None:
    json.dumps(manifest(tmp_path))
