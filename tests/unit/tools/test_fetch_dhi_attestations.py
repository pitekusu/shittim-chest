"""Fail-closed DHI signature, digest and subject boundaries without network access."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tools import fetch_dhi_attestations as fetch
from tools.check_tool_versions import load_tool_pins


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class Registry:
    def __init__(
        self,
        *,
        bad_subject: bool = False,
        file_uri: bool = False,
        bad_predicate: bool = False,
        bad_manifest_subject: bool = False,
    ) -> None:
        self.base = encoded({"schemaVersion": 2, "mediaType": fetch.IMAGE_TYPE})
        self.base_digest = digest(self.base)
        self.data = {self.base_digest: self.base}
        self.blobs: dict[str, bytes] = {}
        self.descriptors: list[dict[str, Any]] = []
        self.statements = {}
        for name, predicate in (("spdx", fetch.SPDX_TYPE), ("vex", fetch.VEX_TYPE)):
            statement = {
                "_type": "https://in-toto.io/Statement/v0.1"
                if name == "vex"
                else "https://in-toto.io/Statement/v1",
                "predicateType": "https://openvex.dev/ns/v9"
                if bad_predicate and name == "vex"
                else predicate,
                "subject": [
                    {
                        "name": "pkg:docker/dhi/python@3?platform=linux%2Farm64",
                        "digest": {
                            "sha256": "0" * 64
                            if bad_subject and name == "vex"
                            else self.base_digest.removeprefix("sha256:")
                        },
                    }
                ],
                "predicate": {
                    "statements": [
                        {"vulnerability": {"name": "CVE-2026-1"}, "status": "not_affected"}
                    ]
                }
                if name == "vex"
                else {"spdxVersion": "SPDX-2.3", "packages": []},
            }
            self.statements[name] = encoded(statement)
            blob = self.statements[name]
            artifact = encoded(
                {
                    "schemaVersion": 2,
                    "mediaType": fetch.IMAGE_TYPE,
                    "artifactType": fetch.INTOTO_TYPE,
                    "subject": {
                        "digest": "sha256:" + "0" * 64
                        if bad_manifest_subject and name == "vex"
                        else self.base_digest,
                        "mediaType": fetch.IMAGE_TYPE,
                        "size": len(self.base),
                    },
                    "annotations": {"in-toto.io/predicate-type": predicate},
                    "layers": [
                        {"digest": digest(blob), "size": len(blob), "mediaType": fetch.INTOTO_TYPE}
                    ],
                }
            )
            artifact_digest = digest(artifact)
            self.data[artifact_digest] = artifact
            self.blobs[artifact_digest] = blob
            self.descriptors.append(
                {
                    "digest": "file:///private" if file_uri and name == "vex" else artifact_digest,
                    "size": len(artifact),
                    "mediaType": fetch.IMAGE_TYPE,
                    "artifactType": fetch.INTOTO_TYPE,
                    "annotations": {"in-toto.io/predicate-type": predicate},
                }
            )
        index = encoded(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "digest": self.base_digest,
                        "size": len(self.base),
                        "mediaType": fetch.IMAGE_TYPE,
                        "platform": {"os": "linux", "architecture": "arm64"},
                    }
                ],
            }
        )
        self.index_digest = digest(index)
        self.data[self.index_digest] = index
        self.reference = "dhi.io/python:3.14.8-debian13@" + self.index_digest
        self.commands: list[list[str]] = []
        self.bad_signature = False
        self.bad_scout = False

    def command(self, args: list[str]) -> bytes:
        self.commands.append(args)
        if args[0] == "cosign":
            target = args[-1].split("@", 1)[1]
            return encoded(
                [
                    {
                        "critical": {
                            "image": {
                                "docker-manifest-digest": "sha256:" + "f" * 64
                                if self.bad_signature
                                else target,
                            }
                        }
                    }
                ]
            )
        if args[0] == "scout":
            data = self.statements["vex" if self.bad_scout else "spdx"]
            Path(args[args.index("--output") + 1]).write_bytes(data)
            return b""
        if args[1:3] == ["manifest", "get"]:
            return self.data[args[3].split("@", 1)[1]]
        if args[1:3] == ["artifact", "list"]:
            return encoded({"manifests": self.descriptors})
        if args[1:3] == ["artifact", "get"]:
            return self.blobs[args[3].split("@", 1)[1]]
        pytest.fail("unexpected test command")


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> bytes:
    key = b"public test signing key"
    monkeypatch.setattr(fetch, "KEY_SHA256", hashlib.sha256(key).hexdigest())
    monkeypatch.setattr(fetch, "download_key", lambda: key)
    return key


def execute(registry: Registry, path: Path) -> dict[str, Any]:
    return fetch.fetch_attestations(
        reference=registry.reference,
        output_dir=path,
        scout="scout",
        regctl="regctl",
        cosign="cosign",
        command=registry.command,
    )


def test_retains_originals_and_binds_every_signature(tmp_path: Path, key: bytes) -> None:
    registry = Registry()
    receipt = execute(registry, tmp_path / "output")
    path = tmp_path / "output"
    assert (path / "vendor-vex.intoto.json").read_bytes() == registry.statements["vex"]
    assert receipt == json.loads((path / "verification-receipt.json").read_text())
    assert receipt["base_digest"] == registry.base_digest
    assert len([c for c in registry.commands if c[0] == "cosign"]) == 3
    assert receipt["transparency_log_checked"] is False
    for name, expected in receipt["files"].items():
        assert digest((path / name).read_bytes()) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "file:///private",
        "https://dhi.io/python@sha256:" + "a" * 64,
        "dhi.io/python:3.14.8-debian13",
        "example.com/python@sha256:" + "a" * 64,
    ],
)
def test_rejects_reference_injection_before_cli(tmp_path: Path, key: bytes, reference: str) -> None:
    registry = Registry()
    registry.reference = reference
    with pytest.raises(fetch.EvidenceError, match="dhi_reference_invalid"):
        execute(registry, tmp_path / "output")
    assert not registry.commands


def test_rejects_signed_vex_for_another_image_even_same_cve(tmp_path: Path, key: bytes) -> None:
    with pytest.raises(fetch.EvidenceError, match="dhi_subject_digest_mismatch"):
        execute(Registry(bad_subject=True), tmp_path / "output")
    assert not (tmp_path / "output/verification-receipt.json").exists()


@pytest.mark.parametrize("boundary", ["index", "base", "manifest", "blob", "signature", "scout"])
def test_rejects_tampered_boundary(tmp_path: Path, key: bytes, boundary: str) -> None:
    registry = Registry()
    if boundary in {"index", "base"}:
        target = registry.index_digest if boundary == "index" else registry.base_digest
        registry.data[target] += b" "
    elif boundary == "blob":
        target = registry.descriptors[-1]["digest"]
        registry.blobs[target] += b" "
    elif boundary == "manifest":
        target = registry.descriptors[-1]["digest"]
        registry.data[target] += b" "
    elif boundary == "signature":
        registry.bad_signature = True
    else:
        registry.bad_scout = True
    with pytest.raises(fetch.EvidenceError):
        execute(registry, tmp_path / "output")
    assert not (tmp_path / "output/verification-receipt.json").exists()


def test_rejects_file_uri_attestation_digest(tmp_path: Path, key: bytes) -> None:
    with pytest.raises(fetch.EvidenceError, match="dhi_descriptor_digest_invalid"):
        execute(Registry(file_uri=True), tmp_path / "output")


@pytest.mark.parametrize("boundary", ["predicate", "manifest_subject"])
def test_rejects_signed_wrong_predicate_or_manifest_subject(
    tmp_path: Path, key: bytes, boundary: str
) -> None:
    registry = Registry(
        bad_predicate=boundary == "predicate", bad_manifest_subject=boundary == "manifest_subject"
    )
    with pytest.raises(fetch.EvidenceError, match=r"dhi_predicate_type|dhi_manifest_subject"):
        execute(registry, tmp_path / "output")
    assert not (tmp_path / "output/verification-receipt.json").exists()


def test_duplicate_arm64_and_json_keys_are_rejected() -> None:
    descriptor = {"platform": {"os": "linux", "architecture": "arm64"}}
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [descriptor, descriptor],
    }
    with pytest.raises(fetch.EvidenceError, match="dhi_arm64_ambiguous"):
        fetch.select_arm64(index)
    with pytest.raises(fetch.EvidenceError, match="dhi_duplicate_json_key"):
        fetch._json(b'{"subject":[],"subject":["other image"]}')


def test_rejects_duplicate_attestations_and_wrong_key(
    tmp_path: Path, key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = Registry()
    registry.descriptors.append(registry.descriptors[0])
    with pytest.raises(fetch.EvidenceError, match="dhi_attestation_ambiguous"):
        execute(registry, tmp_path / "duplicates")
    monkeypatch.setattr(fetch, "download_key", lambda: b"a different public key")
    with pytest.raises(fetch.EvidenceError, match="dhi_signing_key_mismatch"):
        execute(Registry(), tmp_path / "wrong-key")


def test_preserves_existing_output_and_rejects_symlink(tmp_path: Path, key: bytes) -> None:
    original = tmp_path / "original"
    original.mkdir()
    (original / "existing").write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(original)
    with pytest.raises(fetch.EvidenceError, match="dhi_output_symlink"):
        execute(Registry(), link)
    with pytest.raises(fetch.EvidenceError, match="dhi_output_not_empty"):
        execute(Registry(), original)
    assert (original / "existing").read_text() == "keep"


def test_cli_errors_never_include_auth_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a, 1, stdout=b"", stderr=b"private signed URL and authentication response"
        ),
    )
    with pytest.raises(fetch.EvidenceError, match=r"^dhi_command_failed$"):
        fetch.run_command(["regctl", "artifact", "list"])


@pytest.mark.parametrize(
    "name,filename", [("regctl", "regctl-linux-arm64"), ("cosign-dhi", "cosign-linux-arm64")]
)
def test_raw_tool_pins_remain_tracked_and_reject_other_assets(
    tmp_path: Path, name: str, filename: str
) -> None:
    pin = {
        "archive_name": filename,
        "archive_sha256": "a" * 64,
        "repository": "owner/tool",
        "version": "2.6.5",
        "tag_prefix": "v",
    }
    path = tmp_path / "pins.json"
    path.write_text(json.dumps({"schema_version": 1, "tools": {name: pin}}))
    assert load_tool_pins(path)[0].archive_name == filename
    pin["archive_name"] = "../../" + filename
    path.write_text(json.dumps({"schema_version": 1, "tools": {name: pin}}))
    with pytest.raises(ValueError, match="archive name"):
        load_tool_pins(path)
