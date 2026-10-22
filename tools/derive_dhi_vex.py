#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Derive exact binary VEX only from verified DHI attestations and unchanged files.

The caller must authenticate both original in-toto statements before invoking
this tool. This does not verify signatures or make a new vulnerability judgment.
It translates a vendor source-package judgment only when signed inventory,
actual dpkg source identity, full binary PURL, and owned file SHA-256 sets agree.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from packageurl import PackageURL

MAX_JSON_BYTES = 64 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
CVE = re.compile(r"CVE-[0-9]{4}-[0-9]{4,}\Z")
GHSA = re.compile(r"GHSA-[23456789cfghjmpqrvwx]{4}(?:-[23456789cfghjmpqrvwx]{4}){2}\Z")
CONTEXT = "https://openvex.dev/ns/v0.2.0"
AUTHOR = "https://github.com/pitekusu/shittim-chest"
JUSTIFICATIONS = frozenset(
    {
        "component_not_present",
        "vulnerable_code_not_present",
        "vulnerable_code_not_in_execute_path",
        "vulnerable_code_cannot_be_controlled_by_adversary",
        "inline_mitigations_already_exist",
    }
)


class DerivationError(ValueError):
    """Input evidence is malformed or refers to a different immutable image."""


@dataclass(frozen=True)
class Inventory:
    """Image identity and standard Syft dpkg/file catalog results."""

    packages: dict[str, list[dict[str, object]]]
    files: dict[str, str | None]
    links: dict[str, str]
    paths: set[str]
    owned: dict[str, set[str]]
    manifest_digest: str
    config_digest: str


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DerivationError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise DerivationError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DerivationError(f"{label} must be non-empty text")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DerivationError("duplicate JSON key")
        result[key] = value
    return result


def _read(path: Path) -> tuple[dict[str, object], str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise DerivationError("evidence must be a bounded regular non-symlink file")
    payload = path.read_bytes()
    try:
        value: object = json.loads(payload, object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DerivationError("evidence must be UTF-8 JSON") from error
    return _object(value, "document"), hashlib.sha256(payload).hexdigest()


def _digest(value: str) -> str:
    if DIGEST.fullmatch(value) is None:
        raise DerivationError("expected a complete SHA-256 image digest")
    return value


def _attestation(
    document: dict[str, object], *, predicate_type: str, base_digest: str
) -> dict[str, object]:
    if (
        document.get("_type")
        not in {
            "https://in-toto.io/Statement/v0.1",
            "https://in-toto.io/Statement/v1",
        }
        or document.get("predicateType") != predicate_type
    ):
        raise DerivationError("expected an original in-toto attestation")
    subjects = _array(document.get("subject"), "subject")
    if not subjects:
        raise DerivationError("attestation subject is empty")
    for raw in subjects:
        subject = _object(raw, "subject[]")
        name = _text(subject.get("name"), "subject name")
        # Scout's older subject representation used '&platform=' without '?'.
        normalized = name.replace("&platform=", "?platform=", 1) if "?" not in name else name
        identity = _purl(normalized)
        if (
            identity.type != "docker"
            or identity.namespace != "dhi"
            or identity.name != "python"
            or not identity.version
            or identity.qualifiers.get("platform") != "linux/arm64"
        ):
            raise DerivationError("attestation subject must identify ARM64 DHI Python")
        digest = _object(subject.get("digest"), "subject digest")
        if digest != {"sha256": base_digest.removeprefix("sha256:")}:
            raise DerivationError("attestation subject digest differs from the base image")
    return _object(document.get("predicate"), "predicate")


def _purl(value: object) -> PackageURL:
    text = _text(value, "PURL")
    try:
        return PackageURL.from_string(text)
    except (ValueError, TypeError) as error:
        raise DerivationError("invalid PURL") from error


def _debian13(purl: PackageURL, *, vendor: bool) -> bool:
    if purl.type != "deb" or purl.namespace != "debian" or not purl.version:
        return False
    qualifiers = purl.qualifiers
    if not isinstance(qualifiers, dict):
        return False
    if vendor:
        return qualifiers.get("os_name") == "debian" and qualifiers.get("os_version") == "13"
    return (
        qualifiers.get("arch") in {"arm64", "all"}
        and re.fullmatch(r"debian-13(?:\.[0-9]+)?", qualifiers.get("distro", "")) is not None
    )


def _path(value: object) -> str:
    text = _text(value, "owned file path")
    if ".." in PurePosixPath(text).parts or "\x00" in text:
        raise DerivationError("unsafe owned file path")
    return "/" + str(PurePosixPath(text)).lstrip("/")


def _checksums(values: object, *, syft: bool) -> str | None:
    found: set[str] = set()
    for raw in _array(values, "file checksums"):
        checksum = _object(raw, "file checksum")
        if checksum.get("algorithm") == ("sha256" if syft else "SHA256"):
            value = _text(checksum.get("value" if syft else "checksumValue"), "file SHA256")
            if SHA256.fullmatch(value) is None:
                raise DerivationError("invalid file SHA256")
            found.add(value)
    if len(found) > 1:
        raise DerivationError("conflicting file SHA256 values")
    return next(iter(found), None)


def _spdx(
    document: dict[str, object],
) -> tuple[dict[str, list[dict[str, object]]], dict[str, dict[str, str | None]]]:
    if document.get("spdxVersion") != "SPDX-2.3":
        raise DerivationError("expected SPDX-2.3")
    by_name: dict[str, list[dict[str, object]]] = defaultdict(list)
    by_id: dict[str, dict[str, object]] = {}
    for raw in _array(document.get("packages"), "SPDX packages"):
        package = _object(raw, "SPDX package")
        identifier = _text(package.get("SPDXID"), "package SPDXID")
        if identifier in by_id:
            raise DerivationError("duplicate package SPDXID")
        by_id[identifier] = package
        by_name[_text(package.get("name"), "package name")].append(package)
    files: dict[str, tuple[str, str | None]] = {}
    for raw in _array(document.get("files"), "SPDX files"):
        file = _object(raw, "SPDX file")
        identifier = _text(file.get("SPDXID"), "file SPDXID")
        if identifier in files:
            raise DerivationError("duplicate file SPDXID")
        files[identifier] = (
            _path(file.get("fileName")),
            _checksums(file.get("checksums", []), syft=False),
        )
    owned: dict[str, dict[str, str | None]] = defaultdict(dict)
    for raw in _array(document.get("relationships"), "SPDX relationships"):
        relation = _object(raw, "SPDX relationship")
        if relation.get("relationshipType") != "CONTAINS":
            continue
        package_id = _text(relation.get("spdxElementId"), "relationship source")
        file_id = _text(relation.get("relatedSpdxElement"), "relationship target")
        if package_id not in by_id or file_id not in files:
            continue
        path, digest = files[file_id]
        if path in owned[package_id] and owned[package_id][path] != digest:
            raise DerivationError("conflicting owned file hashes")
        owned[package_id][path] = digest
    return dict(by_name), dict(owned)


def _package_purls(package: dict[str, object]) -> list[PackageURL]:
    result: list[PackageURL] = []
    for raw in _array(package.get("externalRefs", []), "package externalRefs"):
        reference = _object(raw, "package externalRef")
        if reference.get("referenceType") == "purl":
            purl = _purl(reference.get("referenceLocator"))
            if purl.type == "deb" and (
                purl.name != package.get("name") or purl.version != package.get("versionInfo")
            ):
                raise DerivationError("SPDX package identity contradicts its PURL")
            result.append(purl)
    return result


def _signed_packages(
    by_name: dict[str, list[dict[str, object]]], name: str, version: str
) -> list[dict[str, object]]:
    return [
        package
        for package in by_name.get(name, [])
        if package.get("versionInfo") == version
        and any(_debian13(purl, vendor=True) for purl in _package_purls(package))
    ]


def _owned_files(
    packages: list[dict[str, object]], owned: dict[str, dict[str, str | None]]
) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for package in packages:
        for path, digest in owned.get(_text(package.get("SPDXID"), "package SPDXID"), {}).items():
            if path in result and result[path] != digest:
                raise DerivationError("conflicting package file inventories")
            result[path] = digest
    return result


def _verification_codes(packages: list[dict[str, object]]) -> set[str]:
    result: set[str] = set()
    for package in packages:
        raw = package.get("packageVerificationCode")
        if raw is not None:
            code = _text(
                _object(raw, "verification code").get("packageVerificationCodeValue"), "code"
            )
            if SHA1.fullmatch(code) is None or package.get("filesAnalyzed") is not True:
                raise DerivationError("invalid package verification code")
            result.add(code)
    return result


def _inventory(
    document: dict[str, object], *, config_digest: str | None = None, base_digest: str | None = None
) -> Inventory:
    source = _object(document.get("source"), "Syft source")
    metadata = _object(source.get("metadata"), "Syft source metadata")
    actual_config = _digest(_text(metadata.get("imageID"), "Syft image config digest"))
    if source.get("type") != "image" or (
        config_digest is not None and actual_config != config_digest
    ):
        raise DerivationError("Syft inventory config digest differs from the completed image")
    manifest_digest = _digest(_text(metadata.get("manifestDigest"), "Syft manifest digest"))
    if base_digest is not None and manifest_digest != base_digest:
        raise DerivationError("base inventory manifest digest differs from the signed subject")
    config = _text(metadata.get("config"), "Syft embedded image config")
    try:
        payload = base64.b64decode(config, validate=True)
        parsed = _object(json.loads(payload, object_pairs_hook=_unique), "image config")
    except (ValueError, UnicodeDecodeError) as error:
        raise DerivationError("invalid embedded image config") from error
    if "sha256:" + hashlib.sha256(payload).hexdigest() != actual_config:
        raise DerivationError("embedded image config hash mismatch")
    if parsed.get("architecture") != "arm64" or parsed.get("os") != "linux":
        raise DerivationError("completed image must be Linux ARM64")
    try:
        manifest_payload = base64.b64decode(
            _text(metadata.get("manifest"), "Syft embedded image manifest"), validate=True
        )
        manifest = _object(
            json.loads(manifest_payload, object_pairs_hook=_unique), "image manifest"
        )
    except (ValueError, UnicodeDecodeError) as error:
        raise DerivationError("invalid embedded image manifest") from error
    if "sha256:" + hashlib.sha256(manifest_payload).hexdigest() != manifest_digest:
        raise DerivationError("embedded image manifest hash mismatch")
    if _object(manifest.get("config"), "manifest config").get("digest") != actual_config:
        raise DerivationError("embedded manifest refers to another config")
    distro = _object(document.get("distro"), "Syft distro")
    if (
        distro.get("id") != "debian"
        or re.fullmatch(r"13(?:\.[0-9]+)?", _text(distro.get("versionID"), "distro version"))
        is None
    ):
        raise DerivationError("completed image must be Debian 13")
    descriptor = _object(document.get("descriptor"), "Syft descriptor")
    configuration = _object(descriptor.get("configuration"), "Syft configuration")
    if _object(configuration.get("files"), "Syft file configuration").get("selection") != "all":
        raise DerivationError("Syft inventory must retain all file metadata, including symlinks")
    by_name: dict[str, list[dict[str, object]]] = defaultdict(list)
    artifact_ids: set[str] = set()
    for raw in _array(document.get("artifacts"), "Syft artifacts"):
        artifact = _object(raw, "Syft artifact")
        identifier = _text(artifact.get("id"), "Syft artifact id")
        if identifier in artifact_ids:
            raise DerivationError("duplicate Syft artifact id")
        artifact_ids.add(identifier)
        by_name[_text(artifact.get("name"), "artifact name")].append(artifact)
    files: dict[str, str | None] = {}
    links: dict[str, str] = {}
    file_ids: dict[str, str] = {}
    for raw in _array(document.get("files"), "Syft files"):
        file = _object(raw, "Syft file")
        path = _path(_object(file.get("location"), "file location").get("path"))
        identifier = _text(file.get("id"), "Syft file id")
        if identifier in file_ids:
            raise DerivationError("duplicate Syft file id")
        file_ids[identifier] = path
        file_metadata = _object(file.get("metadata"), "Syft file metadata")
        if file_metadata.get("type") == "SymbolicLink":
            target = _text(file_metadata.get("linkDestination"), "symlink destination")
            if path in links or path in files or "\x00" in target:
                raise DerivationError("ambiguous symlink metadata")
            links[path] = target
            continue
        if file_metadata.get("type") != "RegularFile":
            continue
        digest = _checksums(file.get("digests", []), syft=True)
        if path in links or (path in files and files[path] != digest):
            raise DerivationError("conflicting actual file hashes")
        files[path] = digest
    owned: dict[str, set[str]] = defaultdict(set)
    for raw in _array(document.get("artifactRelationships"), "Syft artifact relationships"):
        relationship = _object(raw, "Syft artifact relationship")
        if relationship.get("type") == "contains":
            parent = _text(relationship.get("parent"), "Syft relationship parent")
            child = _text(relationship.get("child"), "Syft relationship child")
            if child in file_ids:
                owned[parent].add(file_ids[child])
    return Inventory(
        dict(by_name),
        files,
        links,
        set(file_ids.values()),
        dict(owned),
        manifest_digest,
        actual_config,
    )


def _actual_owned(inventory: Inventory, name: str) -> set[str]:
    paths: set[str] = set()
    for artifact in inventory.packages.get(name, []):
        paths.update(inventory.owned.get(_text(artifact.get("id"), "Syft package id"), set()))
    return paths


def _binary_identity(
    artifacts: list[dict[str, object]], packages: list[dict[str, object]]
) -> tuple[str, str, str, str, set[str]] | None:
    identities: set[tuple[str, str, str, str]] = set()
    paths: set[str] = set()
    authoritative = False
    for artifact in artifacts:
        if artifact.get("type") != "deb" or artifact.get("foundBy") != "dpkg-db-cataloger":
            return None
        if artifact.get("metadataType") != "dpkg-db-entry":
            return None
        metadata = _object(artifact.get("metadata"), "dpkg metadata")
        source, source_version = metadata.get("source"), metadata.get("sourceVersion")
        version = _text(artifact.get("version"), "binary version")
        if not isinstance(source, str) or not source or not isinstance(source_version, str):
            return None
        # Debian Policy 5.6.1: omitted Source version equals the full binary version.
        # https://www.debian.org/doc/debian-policy/ch-controlfields.html#source
        source_version = source_version or version
        purl = _purl(artifact.get("purl"))
        if not _debian13(purl, vendor=False) or (
            metadata.get("architecture") not in {"arm64", "all"}
            or purl.qualifiers.get("arch") != metadata.get("architecture")
            or metadata.get("package") != artifact.get("name")
            or metadata.get("version") != version
            or purl.name != artifact.get("name")
            or purl.version != version
            or purl.qualifiers.get("upstream") != source
        ):
            return None
        identities.add((purl.to_string(), version, source, source_version))
        for raw in _array(metadata.get("files"), "dpkg files"):
            paths.add(_path(_object(raw, "dpkg file").get("path")))
        for raw in _array(artifact.get("locations"), "artifact locations"):
            location = _object(raw, "artifact location")
            if location.get("path") == "/var/lib/dpkg/status":
                authoritative = True
    if len(identities) != 1 or not authoritative or not paths:
        return None
    identity = next(iter(identities))
    if not packages:
        return None
    for package in packages:
        purls = _package_purls(package)
        if package.get("versionInfo") != identity[1] or len(purls) != 1:
            return None
        if purls[0].to_string() != identity[0]:
            return None
    # Preserve Syft's exact spelling (including Debian epoch percent-encoding)
    # after the maintained PURL parser has checked the complete identity.
    native_purl = next(
        _text(reference.get("referenceLocator"), "native binary PURL")
        for raw in _array(packages[0].get("externalRefs"), "package externalRefs")
        if (reference := _object(raw, "package externalRef")).get("referenceType") == "purl"
    )
    return (native_purl, *identity[1:], paths)


def _bind_spdx_image(document: dict[str, object], manifest_digest: str, config_digest: str) -> None:
    roots = [
        _object(raw, "root package")
        for raw in _array(document.get("packages"), "packages")
        if _object(raw, "package").get("primaryPackagePurpose") == "CONTAINER"
    ]
    if len(roots) != 1:
        raise DerivationError("completed SPDX must identify exactly one container")
    hashes = {
        _text(item.get("checksumValue"), "container checksum")
        for raw in _array(roots[0].get("checksums"), "container checksums")
        if (item := _object(raw, "container checksum")).get("algorithm") == "SHA256"
    }
    if len(hashes) != 1 or not hashes <= {
        manifest_digest.removeprefix("sha256:"),
        config_digest.removeprefix("sha256:"),
    }:
        raise DerivationError("completed SPDX image hash differs from Syft inventory")


def derive_vex(
    *,
    vendor_sbom: Path,
    vendor_vex: Path,
    package_inventory: Path,
    base_package_inventory: Path,
    image_sbom: Path,
    base_digest: str,
    image_config_digest: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Produce exact binary statements and a deterministic evidence report."""

    _digest(base_digest)
    _digest(image_config_digest)
    inputs = {
        "vendor_sbom": _read(vendor_sbom),
        "vendor_vex": _read(vendor_vex),
        "package_inventory": _read(package_inventory),
        "base_package_inventory": _read(base_package_inventory),
        "image_sbom": _read(image_sbom),
    }
    signed_sbom = _attestation(
        inputs["vendor_sbom"][0],
        predicate_type="https://spdx.dev/Document",
        base_digest=base_digest,
    )
    signed_vex = _attestation(
        inputs["vendor_vex"][0], predicate_type=CONTEXT, base_digest=base_digest
    )
    if signed_vex.get("@context") != CONTEXT:
        raise DerivationError("unsupported vendor VEX context")
    signed_packages, signed_owned = _spdx(signed_sbom)
    image_packages, image_owned = _spdx(inputs["image_sbom"][0])
    final = _inventory(inputs["package_inventory"][0], config_digest=image_config_digest)
    base = _inventory(inputs["base_package_inventory"][0], base_digest=base_digest)
    artifacts, actual_files, manifest_digest = final.packages, final.files, final.manifest_digest
    # Conservative: any changed or missing existing base link invalidates all
    # mappings. New venv and regenerated CA aliases do not change existing links.
    base_links_unchanged = all(
        final.links.get(path) == target for path, target in base.links.items()
    )
    _bind_spdx_image(inputs["image_sbom"][0], manifest_digest, image_config_digest)
    statements_by_source: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for raw in _array(signed_vex.get("statements"), "vendor VEX statements"):
        statement = _object(raw, "vendor VEX statement")
        vulnerability = _object(statement.get("vulnerability"), "vulnerability")
        cve = _text(vulnerability.get("name"), "vulnerability name")
        if GHSA.fullmatch(cve) is not None:
            continue
        if CVE.fullmatch(cve) is None:
            raise DerivationError("vendor vulnerability must be a complete CVE identifier")
        _text(statement.get("status"), "VEX status")
        for raw_product in _array(statement.get("products"), "VEX products"):
            product = _object(raw_product, "VEX product")
            purl = _purl(product.get("@id"))
            if _debian13(purl, vendor=True):
                key = (_text(purl.name, "source name"), _text(purl.version, "source version"), cve)
                if statement not in statements_by_source[key]:
                    statements_by_source[key].append(statement)
    derived: list[dict[str, object]] = []
    mappings: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for name in sorted(artifacts):
        identity = _binary_identity(artifacts[name], image_packages.get(name, []))
        if identity is None:
            skipped.append({"package": name, "reason": "ambiguous_or_unproven_dpkg_identity"})
            continue
        full_purl, version, source_name, source_version, paths = identity
        if not base_links_unchanged:
            skipped.append({"package": name, "reason": "base_symlinks_changed_or_missing"})
            continue
        if _binary_identity(base.packages.get(name, []), image_packages[name]) != identity:
            skipped.append({"package": name, "reason": "different_base_dpkg_identity"})
            continue
        binary = _signed_packages(signed_packages, name, version)
        source = _signed_packages(signed_packages, source_name, source_version)
        signed_files = _owned_files(binary, signed_owned)
        source_files = _owned_files(source, signed_owned)
        final_files = _owned_files(image_packages[name], image_owned)
        codes = _verification_codes(binary)
        if (
            not source
            or not binary
            or len(codes) != 1
            or codes != _verification_codes(image_packages[name])
        ):
            skipped.append({"package": name, "reason": "not_same_signed_source_and_binary"})
            continue
        if (
            not signed_files
            or any(source_files.get(path) != digest for path, digest in signed_files.items())
            or signed_files != final_files
            or not set(signed_files) <= paths
            or _actual_owned(base, name) != set(signed_files)
            or _actual_owned(final, name) != set(signed_files)
            # DHI prunes documentation while retaining dpkg's md5 records.
            # Such paths must remain absent in both scanned filesystems.
            or any(path in base.paths or path in final.paths for path in paths - set(signed_files))
            or any(base.files.get(path) != digest for path, digest in signed_files.items())
            or any(
                digest is None or actual_files.get(path) != digest
                for path, digest in signed_files.items()
            )
        ):
            skipped.append({"package": name, "reason": "owned_files_changed_or_unproven"})
            continue
        for (candidate_name, candidate_version, cve), candidates in sorted(
            statements_by_source.items()
        ):
            if (candidate_name, candidate_version) != (source_name, source_version):
                continue
            if any(item.get("status") != "not_affected" for item in candidates):
                skipped.append(
                    {"package": name, "vulnerability": cve, "reason": "conflicting_vendor_status"}
                )
                continue
            for statement in candidates:
                if statement.get("justification") not in JUSTIFICATIONS:
                    raise DerivationError(
                        "not_affected vendor statement lacks a valid justification"
                    )
                notes = _text(statement.get("status_notes"), "vendor status notes")
                derived.append(
                    {
                        "vulnerability": copy.deepcopy(statement["vulnerability"]),
                        "products": [{"@id": full_purl}],
                        "status": "not_affected",
                        "justification": statement["justification"],
                        "status_notes": notes,
                        "timestamp": _text(statement.get("timestamp"), "vendor timestamp"),
                    }
                )
                mappings.append(
                    {
                        "binary_purl": full_purl,
                        "source_name": source_name,
                        "source_version": source_version,
                        "vulnerability": cve,
                        "vendor_statement_id": _text(statement.get("@id"), "vendor statement id"),
                        "owned_file_count": len(signed_files),
                        "dpkg_recorded_file_count": len(paths),
                        "pruned_recorded_file_count": len(paths - set(signed_files)),
                        "owned_files_sha256": hashlib.sha256(
                            json.dumps(signed_files, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
    timestamp = _text(signed_vex.get("last_updated", signed_vex.get("timestamp")), "VEX timestamp")
    try:
        dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise DerivationError("vendor VEX timestamp is invalid") from error
    hashes = {name: digest for name, (_, digest) in inputs.items()}
    identity_hash = hashlib.sha256(
        json.dumps(
            {"inputs": hashes, "base": base_digest, "config": image_config_digest}, sort_keys=True
        ).encode()
    ).hexdigest()
    document: dict[str, object] = {
        "@context": CONTEXT,
        "@id": f"{AUTHOR}/security/vex/sha256:{identity_hash}",
        "author": AUTHOR,
        "role": "Document Creator",
        "timestamp": timestamp,
        "version": 1,
        "tooling": "tools/derive_dhi_vex.py",
        "statements": derived,
    }
    report: dict[str, object] = {
        "schema_version": 1,
        "base_arm64_digest": base_digest,
        "image_config_digest": image_config_digest,
        "image_inventory_manifest_digest": manifest_digest,
        "base_inventory_config_digest": base.config_digest,
        "base_symlink_count": len(base.links),
        "base_symlinks_unchanged": base_links_unchanged,
        "base_symlinks_sha256": hashlib.sha256(
            json.dumps(base.links, sort_keys=True).encode()
        ).hexdigest(),
        "input_sha256": hashes,
        "vendor_sbom_subject": inputs["vendor_sbom"][0]["subject"],
        "vendor_vex_subject": inputs["vendor_vex"][0]["subject"],
        "derived_statement_count": len(derived),
        "mappings": mappings,
        "skipped": skipped,
    }
    return document, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "vendor-sbom",
        "vendor-vex",
        "package-inventory",
        "base-package-inventory",
        "image-sbom",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--base-digest", required=True)
    parser.add_argument("--image-config-digest-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        inputs = {
            path.resolve()
            for path in (
                args.vendor_sbom,
                args.vendor_vex,
                args.package_inventory,
                args.base_package_inventory,
                args.image_sbom,
                args.image_config_digest_file,
            )
        }
        outputs = {args.output.resolve(), args.report.resolve()}
        if (
            len(outputs) != 2
            or inputs & outputs
            or args.output.is_symlink()
            or args.report.is_symlink()
        ):
            raise DerivationError("outputs must be separate from each other and original evidence")
        document, report = derive_vex(
            vendor_sbom=args.vendor_sbom,
            vendor_vex=args.vendor_vex,
            package_inventory=args.package_inventory,
            base_package_inventory=args.base_package_inventory,
            image_sbom=args.image_sbom,
            base_digest=args.base_digest,
            image_config_digest=args.image_config_digest_file.read_text().strip(),
        )
        for path, value in ((args.output, document), (args.report, report)):
            path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except (DerivationError, OSError) as error:
        print(f"DHI VEX derivation failed: {error}", file=sys.stderr)
        return 1
    print(f"Derived {report['derived_statement_count']} exact binary VEX statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
