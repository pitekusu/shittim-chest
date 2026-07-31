from __future__ import annotations

import base64
import hashlib
import json
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
BREAK_GLASS_DIGEST = "sha256:" + "e" * 64
CONFIG_DIGEST = "sha256:" + "1" * 64
BREAK_GLASS_CONFIG_DIGEST = "sha256:" + "2" * 64
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
    assert result["scan"] == {
        "schema_version": 1,
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
    }
    assert "findings" not in json.dumps(result).lower()

    signing, referrers, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    cast(dict[str, object], findings["findingSeverityCounts"])["HIGH"] = 1
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


def test_uses_successful_inspector_coverage_timestamp_for_zero_findings() -> None:
    _, _, scan = evidence()
    findings = cast(dict[str, object], scan["imageScanFindings"])
    findings.pop("imageScanCompletedAt")
    findings.pop("findingSeverityCounts")

    result = create_vulnerability_predicate(coverage=inspector_coverage(), digest=DIGEST, scan=scan)

    assert result["scanned_at"] == "2026-07-30T00:00:00Z"
    assert set(cast(dict[str, int], result["severity_counts"]).values()) == {0}


@pytest.mark.parametrize(
    ("coverage", "message"),
    [
        (inspector_coverage(digest=BREAK_GLASS_DIGEST), "does not resolve"),
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
    break_glass_verification = verify_image_evidence(
        digest=BREAK_GLASS_DIGEST,
        image_details={
            "imageDetails": [
                {
                    "imageDigest": BREAK_GLASS_DIGEST,
                    "imageManifestMediaType": "application/vnd.oci.image.index.v1+json",
                }
            ]
        },
        profile_arn=PROFILE,
        signing_status=signing,
        referrers=referrers,
        scan=scan,
    )
    normal_sbom = tmp_path / "normal.spdx.json"
    normal_sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    break_glass_sbom = tmp_path / "break-glass.spdx.json"
    break_glass_sbom.write_text(
        '{"name":"break-glass","spdxVersion":"SPDX-2.3"}\n', encoding="utf-8"
    )
    risk_paths: dict[str, dict[str, Path]] = {}
    for mode in ("normal", "break-glass"):
        mode_paths: dict[str, Path] = {}
        for key in ("grype_raw", "grype_vex", "vendor_vex"):
            path = tmp_path / f"{mode}-{key}.json"
            path.write_text(f'{{"kind":"{key}","mode":"{mode}"}}\n', encoding="utf-8")
            mode_paths[key] = path
        risk_paths[mode] = mode_paths
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
        break_glass_config_digest=BREAK_GLASS_CONFIG_DIGEST,
        break_glass_risk_evidence=risk_paths["break-glass"],
        break_glass_sbom_path=break_glass_sbom,
        break_glass_verification=break_glass_verification,
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
        normal_risk_evidence=risk_paths["normal"],
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
    assert images["break_glass"]["digest"] == BREAK_GLASS_DIGEST
    assert images["break_glass"]["config_digest"] == BREAK_GLASS_CONFIG_DIGEST
    assert value["commit_sha"] == "b" * 40
    assert value["schema_version"] == 3
    assert value["cdk_assets"] == cdk_assets()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(commit_sha="wrong"),
        lambda value: value["images"]["normal"].update(  # type: ignore[index]
            reference=f"{REPOSITORY}:latest"
        ),
        lambda value: value["images"]["normal"].update(config_digest="not-a-digest"),  # type: ignore[index]
        lambda value: value["images"]["normal"]["referrers"].pop("sbom"),  # type: ignore[index]
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


def test_runtime_template_requires_both_exact_digest_images() -> None:
    value = {
        "Resources": {
            "Normal": task("application", f"{REPOSITORY}@{DIGEST}"),
            "BreakGlass": task("break-glass-application", f"{REPOSITORY}@{BREAK_GLASS_DIGEST}"),
        }
    }

    validate_runtime_template(
        value,
        break_glass_digest=BREAK_GLASS_DIGEST,
        normal_digest=DIGEST,
        repository_uri=REPOSITORY,
    )

    value["Resources"]["Normal"] = task("application", f"{REPOSITORY}:latest")
    with pytest.raises(ValueError, match="exact release digest"):
        validate_runtime_template(
            value,
            break_glass_digest=BREAK_GLASS_DIGEST,
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
