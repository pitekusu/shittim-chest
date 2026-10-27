"""Only unchanged, signed source/binary identities can inherit vendor VEX."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from tools.derive_dhi_vex import CONTEXT, DerivationError, derive_vex

CONFIG_BYTES = json.dumps({"architecture": "arm64", "os": "linux"}).encode()
CONFIG_DIGEST = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()
MANIFEST_BYTES = json.dumps({"schemaVersion": 2, "config": {"digest": CONFIG_DIGEST}}).encode()
MANIFEST = "sha256:" + hashlib.sha256(MANIFEST_BYTES).hexdigest()
BASE = MANIFEST
VERSION = "6.5+20250216-2+dhi4"
FILE_HASH = "c" * 64
FILE_PATH = "/usr/lib/aarch64-linux-gnu/libncursesw.so.6.5"
BINARY_PURL = (
    "pkg:deb/debian/libncursesw6@6.5%2B20250216-2%2Bdhi4"
    "?arch=arm64&distro=debian-13.7&upstream=ncurses"
)


def package(name: str, identifier: str, purl: str) -> dict[str, object]:
    return {
        "SPDXID": identifier,
        "name": name,
        "versionInfo": VERSION,
        "filesAnalyzed": True,
        "packageVerificationCode": {"packageVerificationCodeValue": "d" * 40},
        "externalRefs": [{"referenceType": "purl", "referenceLocator": purl}],
    }


def spdx(packages: list[dict[str, object]]) -> dict[str, object]:
    return {
        "spdxVersion": "SPDX-2.3",
        "packages": packages,
        "files": [
            {
                "SPDXID": "SPDXRef-File",
                "fileName": FILE_PATH.lstrip("/"),
                "checksums": [{"algorithm": "SHA256", "checksumValue": FILE_HASH}],
            }
        ],
        "relationships": [
            {
                "spdxElementId": "SPDXRef-Binary",
                "relationshipType": "CONTAINS",
                "relatedSpdxElement": "SPDXRef-File",
            }
        ],
    }


def envelope(predicate: dict[str, object], predicate_type: str) -> dict[str, object]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": predicate_type,
        "subject": [
            {
                "name": "pkg:docker/dhi/python@3.14.8-debian13?platform=linux%2Farm64",
                "digest": {"sha256": BASE.removeprefix("sha256:")},
            }
        ],
        "predicate": predicate,
    }


def evidence() -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any]
]:
    vendor_purl = "pkg:deb/debian/libncursesw6@6.5%2B20250216-2%2Bdhi4?os_name=debian&os_version=13"
    signed = envelope(
        spdx(
            [
                package("libncursesw6", "SPDXRef-Binary", vendor_purl),
                package(
                    "ncurses", "SPDXRef-Source", vendor_purl.replace("libncursesw6", "ncurses")
                ),
            ]
        ),
        "https://spdx.dev/Document",
    )
    cast(dict[str, Any], signed)["predicate"]["relationships"].append(
        {
            "spdxElementId": "SPDXRef-Source",
            "relationshipType": "CONTAINS",
            "relatedSpdxElement": "SPDXRef-File",
        }
    )
    vex = envelope(
        {
            "@context": CONTEXT,
            "@id": "https://vendor.example/original-vex",
            "author": "Docker Hardened Images",
            "timestamp": "2026-10-22T00:00:00Z",
            "statements": [
                {
                    "@id": "vendor-statement-id",
                    "vulnerability": {"name": "CVE-2025-69720"},
                    "status": "not_affected",
                    "justification": "vulnerable_code_not_present",
                    "status_notes": "The affected command is absent from the vendor binary.",
                    "timestamp": "2026-10-22T00:00:00Z",
                    "products": [{"@id": vendor_purl.replace("libncursesw6", "ncurses")}],
                }
            ],
        },
        CONTEXT,
    )
    config_digest = CONFIG_DIGEST
    inventory: dict[str, Any] = {
        "descriptor": {"configuration": {"files": {"selection": "all"}}},
        "source": {
            "type": "image",
            "metadata": {
                "imageID": config_digest,
                "manifestDigest": MANIFEST,
                "config": base64.b64encode(CONFIG_BYTES).decode(),
                "manifest": base64.b64encode(MANIFEST_BYTES).decode(),
            },
        },
        "distro": {"id": "debian", "versionID": "13.7"},
        "artifacts": [
            {
                "id": "binary-id",
                "name": "libncursesw6",
                "version": VERSION,
                "type": "deb",
                "purl": BINARY_PURL,
                "foundBy": "dpkg-db-cataloger",
                "metadataType": "dpkg-db-entry",
                "locations": [{"path": "/var/lib/dpkg/status"}],
                "metadata": {
                    "package": "libncursesw6",
                    "version": VERSION,
                    "source": "ncurses",
                    "sourceVersion": "",
                    "architecture": "arm64",
                    "files": [{"path": FILE_PATH}],
                },
            }
        ],
        "files": [
            {
                "id": "file-id",
                "location": {"path": FILE_PATH},
                "metadata": {"type": "RegularFile"},
                "digests": [{"algorithm": "sha256", "value": FILE_HASH}],
            }
        ],
        "artifactRelationships": [{"parent": "binary-id", "type": "contains", "child": "file-id"}],
    }
    image = spdx([package("libncursesw6", "SPDXRef-Binary", BINARY_PURL)])
    cast(list[Any], image["packages"]).append(
        {
            "SPDXID": "SPDXRef-Image",
            "name": "app-image",
            "primaryPackagePurpose": "CONTAINER",
            "checksums": [
                {"algorithm": "SHA256", "checksumValue": MANIFEST.removeprefix("sha256:")}
            ],
        }
    )
    return signed, vex, inventory, image, config_digest, copy.deepcopy(inventory)


def run(
    tmp_path: Path,
    values: tuple[
        dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any]
    ],
) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = []
    for index, document in enumerate(values[:4]):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps(document))
        paths.append(path)
    base_path = tmp_path / "base-inventory.json"
    base_path.write_text(json.dumps(values[5]))
    return derive_vex(
        vendor_sbom=paths[0],
        vendor_vex=paths[1],
        package_inventory=paths[2],
        base_package_inventory=base_path,
        image_sbom=paths[3],
        base_digest=BASE,
        image_config_digest=values[4],
    )


def test_exact_full_binary_purl_preserves_vendor_judgment_and_originals(tmp_path: Path) -> None:
    values = evidence()
    originals = copy.deepcopy(values)
    result, report = run(tmp_path, values)
    assert values == originals
    assert result["author"] == "https://github.com/pitekusu/shittim-chest"
    assert result["statements"][0]["products"] == [{"@id": BINARY_PURL}]
    assert result["statements"][0]["justification"] == "vulnerable_code_not_present"
    assert (
        result["statements"][0]["status_notes"]
        == values[1]["predicate"]["statements"][0]["status_notes"]
    )
    assert report["base_arm64_digest"] == BASE
    assert report["image_config_digest"] == values[4]
    assert set(report["input_sha256"]) == {
        "vendor_sbom",
        "vendor_vex",
        "package_inventory",
        "base_package_inventory",
        "image_sbom",
    }
    assert report["mappings"][0]["source_version"] == VERSION


def test_https_image_identifier_does_not_change_source_package_mapping(tmp_path: Path) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["products"].append(
        {"@id": f"https://dhi.io/v2/python/manifests/{BASE}"}
    )
    originals = copy.deepcopy(values)
    result, report = run(tmp_path, values)
    assert values == originals
    assert len(result["statements"]) == 1
    assert result["statements"][0]["products"] == [{"@id": BINARY_PURL}]
    assert report["derived_statement_count"] == 1
    assert report["mappings"][0]["source_version"] == VERSION


def test_https_image_identifier_alone_cannot_establish_binary_judgment(tmp_path: Path) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["products"] = [
        {"@id": f"https://dhi.io/v2/python/manifests/{BASE}"}
    ]
    result, report = run(tmp_path, values)
    assert result["statements"] == []
    assert report["mappings"] == []
    assert report["derived_statement_count"] == 0


@pytest.mark.parametrize("identifier", ["pkg:", "pkg:deb", "pkg:deb/debian/ncurses@1?invalid"])
def test_malformed_product_purl_is_rejected_even_with_https_identifier(
    tmp_path: Path, identifier: str
) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["products"] = [
        {"@id": f"https://dhi.io/v2/python/manifests/{BASE}"},
        {"@id": identifier},
    ]
    with pytest.raises(DerivationError, match="invalid PURL"):
        run(tmp_path, values)


@pytest.mark.parametrize(
    "identifier",
    [
        "https:///missing-host",
        "https://[invalid-host",
        "https://dhi.io:invalid/python",
        "https://dhi.io:65536/python",
        "https://dhi.io/invalid path",
        "https://dhi.io/invalid\npath",
        "https://dhi.io/invalid\x7fpath",
        "http://dhi.io/python",
        "urn:example:python",
    ],
)
def test_unsupported_or_malformed_product_identifier_is_rejected(
    tmp_path: Path, identifier: str
) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["products"].append({"@id": identifier})
    with pytest.raises(DerivationError, match="invalid VEX product identifier"):
        run(tmp_path, values)


@pytest.mark.parametrize("index", [0, 1])
def test_tampered_attestation_subject_fails(tmp_path: Path, index: int) -> None:
    values = evidence()
    cast(dict[str, Any], values[index])["subject"][0]["digest"]["sha256"] = "f" * 64
    with pytest.raises(DerivationError, match="subject digest"):
        run(tmp_path, values)


def test_embedded_config_hash_mismatch_fails(tmp_path: Path) -> None:
    values = evidence()
    values[2]["source"]["metadata"]["config"] = base64.b64encode(b"{}").decode()
    with pytest.raises(DerivationError, match="config hash mismatch"):
        run(tmp_path, values)


def test_other_completed_image_hash_fails(tmp_path: Path) -> None:
    values = evidence()
    values[2]["source"]["metadata"]["imageID"] = "sha256:" + "f" * 64
    with pytest.raises(DerivationError, match="config digest"):
        run(tmp_path, values)


def test_spdx_from_another_image_fails(tmp_path: Path) -> None:
    values = evidence()
    values[3]["packages"][-1]["checksums"][0]["checksumValue"] = "f" * 64
    with pytest.raises(DerivationError, match="SPDX image hash"):
        run(tmp_path, values)


@pytest.mark.parametrize("source_version", ["6.5+20250216-2", "6.5+20250216-2+dhi3", None])
def test_different_or_unknown_source_version_remains_unsuppressed(
    tmp_path: Path, source_version: str | None
) -> None:
    values = evidence()
    values[2]["artifacts"][0]["metadata"]["sourceVersion"] = source_version
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


@pytest.mark.parametrize("source", [None, "", "unrelated-source"])
def test_unconfirmed_source_remains_unsuppressed(tmp_path: Path, source: str | None) -> None:
    values = evidence()
    values[2]["artifacts"][0]["metadata"]["source"] = source
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


@pytest.mark.parametrize("status", ["affected", "fixed", "under_investigation", "unknown"])
def test_other_vendor_status_is_never_derived(tmp_path: Path, status: str) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["status"] = status
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_conflicting_vendor_status_does_not_select_not_affected(tmp_path: Path) -> None:
    values = evidence()
    conflict = copy.deepcopy(values[1]["predicate"]["statements"][0])
    conflict["status"] = "affected"
    values[1]["predicate"]["statements"].append(conflict)
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_file_tamper_fails_even_with_same_package_version_and_verification_code(
    tmp_path: Path,
) -> None:
    values = evidence()
    values[2]["files"][0]["digests"][0]["value"] = "f" * 64
    values[3]["files"][0]["checksums"][0]["checksumValue"] = "f" * 64
    result, report = run(tmp_path, values)
    assert result["statements"] == []
    assert report["skipped"][0]["reason"] == "owned_files_changed_or_unproven"


def test_unhashed_actual_file_is_not_suppressed(tmp_path: Path) -> None:
    values = evidence()
    values[2]["files"][0]["digests"] = []
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_missing_vendor_binary_is_not_inferred_from_source_version(tmp_path: Path) -> None:
    values = evidence()
    values[0]["predicate"]["packages"] = values[0]["predicate"]["packages"][1:]
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_copied_out_package_requires_real_authoritative_dpkg_location(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifacts"][0]["locations"] = [{"path": "/app/copied/status"}]
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_other_binary_version_cannot_inherit_name_only_risk_gate_suppression(
    tmp_path: Path,
) -> None:
    values = evidence()
    other = copy.deepcopy(values[3]["packages"][0])
    other["SPDXID"] += "-Other"
    other["versionInfo"] = "old-version"
    other["externalRefs"][0]["referenceLocator"] = BINARY_PURL.replace(
        "6.5%2B20250216-2%2Bdhi4", "old-version"
    )
    values[3]["packages"].append(other)
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_same_name_other_package_type_is_ambiguous(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifacts"].append({"id": "python-id", "name": "libncursesw6", "type": "python"})
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_status_d_duplicates_allow_only_the_same_full_identity(tmp_path: Path) -> None:
    values = evidence()
    duplicate = copy.deepcopy(values[2]["artifacts"][0])
    duplicate["id"] = "duplicate-id"
    duplicate["locations"] = [{"path": "/var/lib/dpkg/status.d/libncursesw6"}]
    duplicate["metadata"]["files"] = []
    values[2]["artifacts"].append(duplicate)
    spdx_duplicate = copy.deepcopy(values[3]["packages"][0])
    spdx_duplicate["SPDXID"] += "-Duplicate"
    spdx_duplicate["filesAnalyzed"] = False
    del spdx_duplicate["packageVerificationCode"]
    values[3]["packages"].append(spdx_duplicate)
    result, _ = run(tmp_path, values)
    assert len(result["statements"]) == 1


def test_architecture_independent_package_retains_all_in_exact_purl(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifacts"][0]["metadata"]["architecture"] = "all"
    values[2]["artifacts"][0]["purl"] = BINARY_PURL.replace("arch=arm64", "arch=all")
    values[5]["artifacts"][0]["metadata"]["architecture"] = "all"
    values[5]["artifacts"][0]["purl"] = BINARY_PURL.replace("arch=arm64", "arch=all")
    values[3]["packages"][0]["externalRefs"][0]["referenceLocator"] = BINARY_PURL.replace(
        "arch=arm64", "arch=all"
    )
    result, _ = run(tmp_path, values)
    assert result["statements"][0]["products"][0]["@id"] == BINARY_PURL.replace(
        "arch=arm64", "arch=all"
    )


def test_standalone_scout_export_is_not_an_original_attestation(tmp_path: Path) -> None:
    signed, vex, inventory, image, config, base = evidence()
    with pytest.raises(DerivationError, match="original in-toto"):
        run(tmp_path, (signed, vex["predicate"], inventory, image, config, base))


def test_signed_source_without_the_binary_files_does_not_establish_relationship(
    tmp_path: Path,
) -> None:
    values = evidence()
    values[0]["predicate"]["relationships"] = values[0]["predicate"]["relationships"][:1]
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_embedded_manifest_hash_mismatch_fails(tmp_path: Path) -> None:
    values = evidence()
    values[2]["source"]["metadata"]["manifest"] = base64.b64encode(b"{}").decode()
    with pytest.raises(DerivationError, match="manifest hash mismatch"):
        run(tmp_path, values)


def test_manifest_cannot_bind_the_spdx_to_another_config(tmp_path: Path) -> None:
    values = evidence()
    payload = json.dumps({"config": {"digest": "sha256:" + "e" * 64}}).encode()
    digest = hashlib.sha256(payload).hexdigest()
    values[2]["source"]["metadata"]["manifest"] = base64.b64encode(payload).decode()
    values[2]["source"]["metadata"]["manifestDigest"] = "sha256:" + digest
    values[3]["packages"][-1]["checksums"][0]["checksumValue"] = digest
    with pytest.raises(DerivationError, match="another config"):
        run(tmp_path, values)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64-invalid"])
def test_wrong_subject_platform_fails(tmp_path: Path, platform: str) -> None:
    values = evidence()
    values[0]["subject"][0]["name"] = f"pkg:docker/dhi/python@3.14.8?platform={platform}"
    with pytest.raises(DerivationError, match="ARM64 DHI Python"):
        run(tmp_path, values)


def test_wrong_subject_namespace_fails(tmp_path: Path) -> None:
    values = evidence()
    values[0]["subject"][0]["name"] = "pkg:docker/other/python@3?platform=linux/arm64"
    with pytest.raises(DerivationError, match="ARM64 DHI Python"):
        run(tmp_path, values)


def test_missing_source_is_not_inferred_from_the_version(tmp_path: Path) -> None:
    values = evidence()
    del values[2]["artifacts"][0]["metadata"]["source"]
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_syft_same_name_mixed_full_versions_are_not_suppressed(tmp_path: Path) -> None:
    values = evidence()
    other = copy.deepcopy(values[2]["artifacts"][0])
    other["version"] = "older-version"
    other["id"] = "older-id"
    other["metadata"]["version"] = "older-version"
    other["purl"] = BINARY_PURL.replace("6.5%2B20250216-2%2Bdhi4", "older-version")
    values[2]["artifacts"].append(other)
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_verification_code_alone_cannot_override_a_binary_version_change(tmp_path: Path) -> None:
    values = evidence()
    new_version = "6.5+20250216-2+dhi5"
    values[2]["artifacts"][0]["version"] = new_version
    values[2]["artifacts"][0]["metadata"]["version"] = new_version
    values[2]["artifacts"][0]["purl"] = BINARY_PURL.replace("%2Bdhi4", "%2Bdhi5")
    values[3]["packages"][0]["versionInfo"] = new_version
    values[3]["packages"][0]["externalRefs"][0]["referenceLocator"] = BINARY_PURL.replace(
        "%2Bdhi4", "%2Bdhi5"
    )
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_added_owned_file_outside_vendor_package_is_not_suppressed(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifacts"][0]["metadata"]["files"].append({"path": "/app/extra-program"})
    values[2]["files"].append(
        {
            "id": "extra-file",
            "location": {"path": "/app/extra-program"},
            "metadata": {"type": "RegularFile"},
            "digests": [{"algorithm": "sha256", "value": FILE_HASH}],
        }
    )
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_mixed_binary_architectures_are_not_suppressed(tmp_path: Path) -> None:
    values = evidence()
    other = copy.deepcopy(values[2]["artifacts"][0])
    other["metadata"]["architecture"] = "amd64"
    other["id"] = "other-architecture-id"
    other["purl"] = BINARY_PURL.replace("arch=arm64", "arch=amd64")
    values[2]["artifacts"].append(other)
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_changed_package_verification_code_stays_unsuppressed(tmp_path: Path) -> None:
    values = evidence()
    values[3]["packages"][0]["packageVerificationCode"]["packageVerificationCodeValue"] = "f" * 40
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_unversioned_product_never_expands_to_binary_packages(tmp_path: Path) -> None:
    values = evidence()
    values[1]["predicate"]["statements"][0]["products"] = [{"@id": "pkg:deb/debian/ncurses"}]
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


@pytest.mark.parametrize("change", ["remove", "retarget"])
def test_changed_existing_base_symlink_blocks_every_mapping(tmp_path: Path, change: str) -> None:
    values = evidence()
    link = {
        "id": "link-id",
        "location": {"path": "/usr/lib/aarch64-linux-gnu/libncursesw.so.6"},
        "metadata": {"type": "SymbolicLink", "linkDestination": "libncursesw.so.6.5"},
    }
    values[5]["files"].append(copy.deepcopy(link))
    if change == "retarget":
        link["metadata"]["linkDestination"] = "/app/other-library"
        values[2]["files"].append(link)
    result, report = run(tmp_path, values)
    assert result["statements"] == []
    assert report["base_symlinks_unchanged"] is False
    assert report["skipped"][0]["reason"] == "base_symlinks_changed_or_missing"


def test_new_venv_or_ca_alias_preserves_all_existing_base_links(tmp_path: Path) -> None:
    values = evidence()
    link = {
        "id": "link-id",
        "location": {"path": "/usr/lib/aarch64-linux-gnu/libncursesw.so.6"},
        "metadata": {"type": "SymbolicLink", "linkDestination": "libncursesw.so.6.5"},
    }
    values[2]["files"].append(copy.deepcopy(link))
    values[5]["files"].append(copy.deepcopy(link))
    values[2]["files"].append(
        {
            "id": "new-link-id",
            "location": {"path": "/etc/ssl/certs/01234567.0"},
            "metadata": {"type": "SymbolicLink", "linkDestination": "ordinary.pem"},
        }
    )
    result, report = run(tmp_path, values)
    assert len(result["statements"]) == 1
    assert report["base_symlinks_unchanged"] is True
    assert report["base_symlink_count"] == 1


def test_base_inventory_must_resolve_to_the_signed_arm64_digest(tmp_path: Path) -> None:
    values = evidence()
    values[5]["source"]["metadata"]["manifestDigest"] = "sha256:" + "e" * 64
    with pytest.raises(DerivationError, match="base inventory manifest digest"):
        run(tmp_path, values)


def test_file_inventory_without_symlink_metadata_fails_closed(tmp_path: Path) -> None:
    values = evidence()
    values[2]["descriptor"]["configuration"]["files"]["selection"] = "owned-by-package"
    with pytest.raises(DerivationError, match="all file metadata"):
        run(tmp_path, values)


def test_missing_actual_syft_package_ownership_is_not_suppressed(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifactRelationships"] = []
    result, _ = run(tmp_path, values)
    assert result["statements"] == []


def test_explicit_full_source_version_is_preserved(tmp_path: Path) -> None:
    values = evidence()
    values[2]["artifacts"][0]["metadata"]["sourceVersion"] = VERSION
    values[5]["artifacts"][0]["metadata"]["sourceVersion"] = VERSION
    result, report = run(tmp_path, values)
    assert len(result["statements"]) == 1
    assert report["mappings"][0]["source_version"] == VERSION


@pytest.mark.parametrize("drop_epoch", [False, True])
def test_debian_epoch_is_preserved_in_the_native_full_purl(
    tmp_path: Path, drop_epoch: bool
) -> None:
    values = json.loads(
        json.dumps(evidence()).replace(VERSION, "1:" + VERSION).replace("@6.5%2B", "@1%3A6.5%2B")
    )
    if drop_epoch:
        values[2]["artifacts"][0]["metadata"]["sourceVersion"] = VERSION
    result, report = run(tmp_path, values)
    if drop_epoch:
        assert result["statements"] == []
    else:
        assert result["statements"][0]["products"] == [
            {"@id": BINARY_PURL.replace("@6.5%2B", "@1%3A6.5%2B")}
        ]
        assert report["mappings"][0]["source_version"] == "1:" + VERSION


def test_other_vendor_advisory_types_remain_untouched(tmp_path: Path) -> None:
    values = evidence()
    advisory = copy.deepcopy(values[1]["predicate"]["statements"][0])
    advisory["vulnerability"]["name"] = "GHSA-6v7p-g79w-8964"
    values[1]["predicate"]["statements"].append(advisory)
    result, _ = run(tmp_path, values)
    assert len(result["statements"]) == 1
    assert result["statements"][0]["vulnerability"]["name"] == "CVE-2025-69720"


def test_pruned_dpkg_documentation_must_stay_absent_in_both_images(tmp_path: Path) -> None:
    values = evidence()
    for index in (2, 5):
        values[index]["artifacts"][0]["metadata"]["files"].append(
            {"path": "/usr/share/doc/libncursesw6/changelog.gz"}
        )
    result, report = run(tmp_path, values)
    assert len(result["statements"]) == 1
    assert report["mappings"][0]["pruned_recorded_file_count"] == 1


@pytest.mark.parametrize("file_type", ["RegularFile", "Directory", "SymbolicLink"])
def test_reintroducing_pruned_file_cannot_inherit_vendor_judgment(
    tmp_path: Path, file_type: str
) -> None:
    values = evidence()
    pruned = "/usr/share/doc/libncursesw6/changelog.gz"
    for index in (2, 5):
        values[index]["artifacts"][0]["metadata"]["files"].append({"path": pruned})
    values[2]["files"].append(
        {
            "id": "reintroduced-file",
            "location": {"path": pruned},
            "metadata": {"type": file_type, "linkDestination": "/app/other-file"},
            "digests": [{"algorithm": "sha256", "value": FILE_HASH}],
        }
    )
    result, _ = run(tmp_path, values)
    assert result["statements"] == []
