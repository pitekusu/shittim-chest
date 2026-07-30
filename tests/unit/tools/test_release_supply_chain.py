from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from tools.release_supply_chain import (
    create_manifest,
    create_vulnerability_predicate,
    validate_change_set,
    validate_manifest,
    validate_runtime_template,
    verify_image_evidence,
)

DIGEST = "sha256:" + "a" * 64
BREAK_GLASS_DIGEST = "sha256:" + "e" * 64
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
        break_glass_risk_evidence=risk_paths["break-glass"],
        break_glass_sbom_path=break_glass_sbom,
        break_glass_verification=break_glass_verification,
        change_sets=changes,
        commit_sha="b" * 40,
        lambda_bundle={
            "bucket": "cdk-hnb659fds-assets-000000000000-ap-northeast-1",
            "key": f"lambda/shittim-chest/{'c' * 64}/shittim-chest-lambda-arm64.zip",
            "sha256": "c" * 64,
        },
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
    assert value["commit_sha"] == "b" * 40


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(commit_sha="wrong"),
        lambda value: value["images"]["normal"].update(  # type: ignore[index]
            reference=f"{REPOSITORY}:latest"
        ),
        lambda value: value["images"]["normal"]["referrers"].pop("sbom"),  # type: ignore[index]
        lambda value: value["change_sets"].update({"ShittimChest-Prod-Runtime": "not-an-arn"}),
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
        "ChangeSetType": "UPDATE",
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


def task(name: str, image: str) -> dict[str, object]:
    return {
        "Type": "AWS::ECS::TaskDefinition",
        "Properties": {"ContainerDefinitions": [{"Name": name, "Image": image}]},
    }


def test_manifest_is_json_serializable(tmp_path: Path) -> None:
    json.dumps(manifest(tmp_path))
