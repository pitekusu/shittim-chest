"""Fetch Docker-signed DHI evidence without modifying its statements or trust stores.

Scout's VEX exporter loses Debian source relationships in current DHI metadata.
Use Docker's documented regctl/Cosign OCI 1.1 path to retain the original in-toto
statement, while keeping Scout's independent SPDX verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

KEY_COMMIT = "d6b11e0475ac7ddf74687268d16a4201a15e163f"
KEY_SHA256 = "1d02bbccf149283ae6288d96264dcad3fb23ee1911d90324a48eab28e4cb8a5f"
KEY_URL = (
    "https://raw.githubusercontent.com/docker-hardened-images/keyring/"
    f"{KEY_COMMIT}/publickey/dhi-latest.pub"
)
SPDX_TYPE = "https://spdx.dev/Document"
VEX_TYPE = "https://openvex.dev/ns/v0.2.0"
IMAGE_TYPE = "application/vnd.oci.image.manifest.v1+json"
INTOTO_TYPE = "application/vnd.in-toto+json"
MAX_BYTES = 32 * 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REFERENCE = re.compile(r"dhi\.io/python:3\.14\.8-debian13(?:-dev)?@(sha256:[0-9a-f]{64})\Z")
Command = Callable[[list[str]], bytes]


class EvidenceError(ValueError):
    """An evidence boundary failed; messages never include provider responses."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "dhi_duplicate_json_key")
        result[key] = value
    return result


def _json(data: bytes) -> Any:
    _require(len(data) <= MAX_BYTES, "dhi_evidence_too_large")
    try:
        return json.loads(data, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("dhi_json_invalid") from error


def _object(data: object) -> dict[str, Any]:
    _require(isinstance(data, dict), "dhi_object_required")
    return cast(dict[str, Any], data)


def _array(data: object) -> list[Any]:
    _require(isinstance(data, list), "dhi_array_required")
    return cast(list[Any], data)


def _descriptor(data: object, *, media_type: str | None = None) -> dict[str, Any]:
    value = _object(data)
    _require(isinstance(value.get("digest"), str), "dhi_descriptor_digest_invalid")
    _require(DIGEST.fullmatch(value["digest"]), "dhi_descriptor_digest_invalid")
    size = value.get("size")
    _require(type(size) is int and 0 < size <= MAX_BYTES, "dhi_descriptor_size_invalid")
    if media_type is not None:
        _require(value.get("mediaType") == media_type, "dhi_descriptor_type_invalid")
    return value


def run_command(command: list[str]) -> bytes:
    """Use CLI-managed credentials; never disclose their captured diagnostics."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed commands, validated references, no shell.
            command, capture_output=True, check=False, timeout=180
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EvidenceError("dhi_command_unavailable") from error
    _require(result.returncode == 0, "dhi_command_failed")
    _require(len(result.stdout) <= MAX_BYTES, "dhi_evidence_too_large")
    return result.stdout


def download_key() -> bytes:
    """Pin the public signing key independently of the registry's latest alias."""
    try:
        with urllib.request.urlopen(KEY_URL, timeout=30) as response:  # noqa: S310 - fixed HTTPS URL.
            data = response.read(4097)
    except OSError as error:
        raise EvidenceError("dhi_key_download_failed") from error
    _require(len(data) <= 4096, "dhi_key_too_large")
    return data


def select_arm64(index: object) -> dict[str, Any]:
    """Require one unambiguous Linux ARM64 descriptor in the pinned index."""
    value = _object(index)
    _require(value.get("schemaVersion") == 2, "dhi_index_schema_invalid")
    _require(
        value.get("mediaType") == "application/vnd.oci.image.index.v1+json",
        "dhi_index_type_invalid",
    )
    manifests = _array(value.get("manifests"))
    candidates = [
        item
        for item in manifests
        if isinstance(item, dict)
        and item.get("platform") == {"os": "linux", "architecture": "arm64"}
    ]
    _require(len(candidates) == 1, "dhi_arm64_ambiguous")
    return _descriptor(candidates[0], media_type=IMAGE_TYPE)


def select_attestation(referrers: object, predicate_type: str) -> dict[str, Any]:
    """Never pick an arbitrary newest attestation when identities conflict."""
    values = _array(_object(referrers).get("manifests"))
    candidates = [
        item
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("annotations"), dict)
        and item["annotations"].get("in-toto.io/predicate-type") == predicate_type
    ]
    _require(len(candidates) == 1, "dhi_attestation_ambiguous")
    result = _descriptor(candidates[0], media_type=IMAGE_TYPE)
    _require(result.get("artifactType") == INTOTO_TYPE, "dhi_artifact_type_invalid")
    return result


def validate_statement(value: object, predicate_type: str, base_digest: str) -> dict[str, Any]:
    """Bind every in-toto subject to the selected ARM64 image, retaining versions."""
    statement = _object(value)
    _require(
        statement.get("_type")
        in {"https://in-toto.io/Statement/v1", "https://in-toto.io/Statement/v0.1"},
        "dhi_statement_type",
    )
    _require(statement.get("predicateType") == predicate_type, "dhi_predicate_type")
    subjects = _array(statement.get("subject"))
    _require(subjects, "dhi_subject_missing")
    for subject in subjects:
        subject = _object(subject)
        _require(
            isinstance(subject.get("name"), str)
            and subject["name"].startswith("pkg:docker/dhi/python@"),
            "dhi_subject_name_invalid",
        )
        _require(
            subject.get("digest") == {"sha256": base_digest.removeprefix("sha256:")},
            "dhi_subject_digest_mismatch",
        )
    _object(statement.get("predicate"))
    return statement


def fetch_attestations(
    *,
    reference: str,
    output_dir: Path,
    scout: str,
    regctl: str,
    cosign: str,
    command: Command = run_command,
) -> dict[str, Any]:
    """Fetch immutable evidence; a receipt is written only after every check passes."""
    match = REFERENCE.fullmatch(reference)
    _require(match is not None, "dhi_reference_invalid")
    match = cast(re.Match[str], match)
    _require(not output_dir.is_symlink(), "dhi_output_symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    _require(not any(output_dir.iterdir()), "dhi_output_not_empty")
    files: dict[str, str] = {}

    def save(name: str, data: bytes) -> None:
        _require(len(data) <= MAX_BYTES, "dhi_evidence_too_large")
        with (output_dir / name).open("xb") as stream:
            stream.write(data)
        files[name] = _digest(data)

    key = download_key()
    _require(_digest(key) == "sha256:" + KEY_SHA256, "dhi_signing_key_mismatch")
    save("dhi-signing-key.pub", key)
    index_digest = match[1]
    immutable = "dhi.io/python@" + index_digest
    index_raw = command([regctl, "manifest", "get", immutable, "--format", "raw-body"])
    _require(_digest(index_raw) == index_digest, "dhi_index_digest_mismatch")
    base = select_arm64(_json(index_raw))
    save("index.manifest.json", index_raw)
    base_digest = base["digest"]
    base_reference = "dhi.io/python@" + base_digest
    base_raw = command([regctl, "manifest", "get", base_reference, "--format", "raw-body"])
    _require(_digest(base_raw) == base_digest, "dhi_base_digest_mismatch")
    _require(len(base_raw) == base["size"], "dhi_base_size_mismatch")
    manifest = _object(_json(base_raw))
    _require(manifest.get("mediaType") == IMAGE_TYPE, "dhi_base_type_invalid")
    save("base.manifest.json", base_raw)
    save("base-digest.txt", (base_digest + "\n").encode())

    def verify_signature(target: str, name: str) -> None:
        signature = command(
            [
                cosign,
                "verify",
                "--experimental-oci11",
                "--key",
                str(output_dir / "dhi-signing-key.pub"),
                "--insecure-ignore-tlog=true",
                "dhi.io/python@" + target,
            ]
        )
        claims = _array(_json(signature))
        _require(claims, "dhi_signature_claim_missing")
        for claim in claims:
            critical = _object(_object(claim).get("critical"))
            _require(
                _object(critical.get("image")).get("docker-manifest-digest") == target,
                "dhi_signature_claim_mismatch",
            )
        save(name, signature)

    verify_signature(index_digest, "index.signature.json")
    referrers_raw = command([regctl, "artifact", "list", base_reference, "--format", "body"])
    referrers = _json(referrers_raw)
    save("referrers.json", referrers_raw)
    artifact_receipts: dict[str, Any] = {}
    for kind, predicate_type in (("spdx", SPDX_TYPE), ("vex", VEX_TYPE)):
        descriptor = select_attestation(referrers, predicate_type)
        artifact_reference = "dhi.io/python@" + descriptor["digest"]
        raw = command([regctl, "manifest", "get", artifact_reference, "--format", "raw-body"])
        _require(_digest(raw) == descriptor["digest"], "dhi_attestation_digest_mismatch")
        _require(len(raw) == descriptor["size"], "dhi_attestation_size_mismatch")
        artifact = _object(_json(raw))
        _require(artifact.get("artifactType") == INTOTO_TYPE, "dhi_artifact_type_invalid")
        _require(
            artifact.get("subject")
            == {"digest": base_digest, "mediaType": IMAGE_TYPE, "size": len(base_raw)},
            "dhi_manifest_subject_mismatch",
        )
        _require(
            _object(artifact.get("annotations")).get("in-toto.io/predicate-type") == predicate_type,
            "dhi_manifest_predicate_mismatch",
        )
        layers = _array(artifact.get("layers"))
        _require(len(layers) == 1, "dhi_blob_ambiguous")
        layer = _descriptor(layers[0], media_type=INTOTO_TYPE)
        save(f"{kind}.attestation.manifest.json", raw)
        verify_signature(descriptor["digest"], f"{kind}.signature.json")
        blob = command([regctl, "artifact", "get", artifact_reference])
        _require(_digest(blob) == layer["digest"], "dhi_blob_digest_mismatch")
        _require(len(blob) == layer["size"], "dhi_blob_size_mismatch")
        statement = validate_statement(_json(blob), predicate_type, base_digest)
        save(f"vendor-{kind}.intoto.json", blob)
        if kind == "spdx":
            scout_path = output_dir / "scout-verified-spdx.intoto.json"
            command(
                [
                    scout,
                    "attest",
                    "get",
                    "--verify",
                    "--skip-tlog",
                    "--key",
                    str(output_dir / "dhi-signing-key.pub"),
                    "--platform",
                    "linux/arm64",
                    "--predicate-type",
                    SPDX_TYPE,
                    "--output",
                    str(scout_path),
                    "registry://" + immutable,
                ]
            )
            _require(
                not scout_path.is_symlink() and scout_path.is_file(), "dhi_scout_output_missing"
            )
            scout_raw = scout_path.read_bytes()
            _require(_json(scout_raw) == statement, "dhi_scout_statement_mismatch")
            files[scout_path.name] = _digest(scout_raw)
        else:
            save(
                "vendor.openvex.json",
                (json.dumps(statement["predicate"], indent=2) + "\n").encode(),
            )
        artifact_receipts[kind] = {
            "manifest_digest": descriptor["digest"],
            "blob_digest": layer["digest"],
            "subject_digest": base_digest,
            "predicate_type": predicate_type,
            "signature_verified": True,
        }
    receipt = {
        "schema_version": 1,
        "reference": reference,
        "index_digest": index_digest,
        "base_digest": base_digest,
        "platform": "linux/arm64",
        "key_commit": KEY_COMMIT,
        "key_sha256": KEY_SHA256,
        "index_signature_verified": True,
        "transparency_log_checked": False,
        "artifacts": artifact_receipts,
        "files": files,
    }
    with (output_dir / "verification-receipt.json").open("xb") as stream:
        stream.write((json.dumps(receipt, indent=2) + "\n").encode())
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scout", required=True)
    parser.add_argument("--regctl", required=True)
    parser.add_argument("--cosign", required=True)
    args = parser.parse_args()
    try:
        receipt = fetch_attestations(
            reference=args.reference,
            output_dir=args.output_dir,
            scout=args.scout,
            regctl=args.regctl,
            cosign=args.cosign,
        )
    except (EvidenceError, OSError, TypeError, KeyError, IndexError) as error:
        code = str(error) if isinstance(error, EvidenceError) else "dhi_evidence_invalid"
        print(code, file=sys.stderr)
        return 1
    print(json.dumps({"status": "verified", "base_digest": receipt["base_digest"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
