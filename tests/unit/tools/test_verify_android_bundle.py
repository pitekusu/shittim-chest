"""Test the release verifier without signing keys, a JDK, or downloaded tooling."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tools import verify_android_bundle as verifier

SHA = "a" * 40
CERTIFICATE = "ab" * 32
MANIFEST = (
    f'<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
    f'package="{verifier.PACKAGE}" android:versionCode="7" android:versionName="0.0.7">'
    '<application android:debuggable="false" /></manifest>'
)


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, MagicMock]:
    bundle = tmp_path / "verified.aab"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("META-INF/UPLOAD.RSA", b"synthetic PKCS7 fixture")
        archive.writestr("base/manifest/AndroidManifest.xml", b"synthetic protobuf fixture")
    bundletool = tmp_path / "bundletool.jar"
    bundletool.write_bytes(b"synthetic pinned tool fixture")
    monkeypatch.setenv("JAVA_HOME", str(tmp_path / "jdk"))
    pins = json.loads(
        (Path(verifier.__file__).resolve().parents[1] / ".github/tool-versions.json").read_text()
    )
    original_digest = verifier.digest
    monkeypatch.setattr(
        verifier,
        "digest",
        lambda path: (
            pins["tools"]["bundletool"]["archive_sha256"]
            if path == bundletool
            else original_digest(path)
        ),
    )
    certificate = MagicMock()
    certificate.fingerprint.return_value = bytes.fromhex(CERTIFICATE)
    monkeypatch.setattr(verifier.pkcs7, "load_der_pkcs7_certificates", lambda _: [certificate])
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout=b"jar verified.", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=MANIFEST.encode(), stderr=b""),
        ]
    )
    monkeypatch.setattr(verifier.subprocess, "run", run)
    return bundle, bundletool, run


@pytest.mark.parametrize("jdk_code", (0, 4))
def test_signed_bundle_binds_manifest_source_and_digest(
    inputs: tuple[Path, Path, MagicMock], jdk_code: int
) -> None:
    bundle, bundletool, run = inputs
    run.side_effect = [
        subprocess.CompletedProcess([], jdk_code, stdout=b"jar verified.", stderr=b""),
        subprocess.CompletedProcess([], 0, stdout=MANIFEST.encode(), stderr=b""),
    ]

    result = verifier.verify_bundle(
        bundle, bundletool, code=7, sha=SHA, certificate=CERTIFICATE.upper()
    )

    assert result == {
        "sha": SHA,
        "versionCode": 7,
        "bundleSha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        "track": "internal",
    }
    assert run.call_args_list[0].args[0][1:4] == [
        "-J-Duser.language=en",
        "-verify",
        "-strict",
    ]
    assert run.call_args_list[1].args[0][-3:] == ["dump", "manifest", f"--bundle={bundle}"]


@pytest.mark.parametrize(
    "jdk_code,output", ((16, b"jar verified."), (20, b"jar verified."), (0, b""))
)
def test_unsigned_entries_or_missing_verification_cannot_pass(
    inputs: tuple[Path, Path, MagicMock], jdk_code: int, output: bytes
) -> None:
    bundle, bundletool, run = inputs
    run.side_effect = [subprocess.CompletedProcess([], jdk_code, stdout=output, stderr=b"")]

    with pytest.raises(ValueError, match="android_bundle_signature_invalid"):
        verifier.verify_bundle(bundle, bundletool, code=7, sha=SHA, certificate=CERTIFICATE)

    assert run.call_count == 1


@pytest.mark.parametrize("count,fingerprint", ((0, CERTIFICATE), (2, CERTIFICATE), (1, "cd" * 32)))
def test_pkcs7_requires_the_single_pinned_upload_certificate(
    inputs: tuple[Path, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    fingerprint: str,
) -> None:
    bundle, bundletool, _ = inputs
    certificate = MagicMock()
    certificate.fingerprint.return_value = bytes.fromhex(fingerprint)
    monkeypatch.setattr(
        verifier.pkcs7, "load_der_pkcs7_certificates", lambda _: [certificate] * count
    )

    with pytest.raises(ValueError, match="android_bundle_signer_invalid"):
        verifier.verify_bundle(bundle, bundletool, code=7, sha=SHA, certificate=CERTIFICATE)


@pytest.mark.parametrize(
    "before,after",
    (
        (verifier.PACKAGE, "dev.example.other"),
        ('versionCode="7"', 'versionCode="8"'),
        ('versionName="0.0.7"', 'versionName="0.0.8"'),
        ('debuggable="false"', 'debuggable="true"'),
        ('<application android:debuggable="false" />', ""),
    ),
)
def test_manifest_must_be_the_fixed_non_debuggable_release(
    inputs: tuple[Path, Path, MagicMock], before: str, after: str
) -> None:
    bundle, bundletool, run = inputs
    run.side_effect = [
        subprocess.CompletedProcess([], 0, stdout=b"jar verified.", stderr=b""),
        subprocess.CompletedProcess([], 0, stdout=MANIFEST.replace(before, after).encode()),
    ]

    with pytest.raises(ValueError, match="android_bundle_manifest_invalid"):
        verifier.verify_bundle(bundle, bundletool, code=7, sha=SHA, certificate=CERTIFICATE)


def test_changed_bundletool_is_rejected_before_execution(
    inputs: tuple[Path, Path, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, bundletool, run = inputs
    monkeypatch.setattr(verifier, "digest", lambda _: "0" * 64)

    with pytest.raises(ValueError, match="android_bundletool_digest_invalid"):
        verifier.verify_bundle(bundle, bundletool, code=7, sha=SHA, certificate=CERTIFICATE)

    assert run.call_count == 1


def test_symlink_and_unpinned_source_fail_before_tools_run(
    inputs: tuple[Path, Path, MagicMock], tmp_path: Path
) -> None:
    bundle, bundletool, run = inputs
    linked = tmp_path / "linked.aab"
    linked.symlink_to(bundle)

    for path, sha in ((linked, SHA), (bundle, "main")):
        with pytest.raises(ValueError, match="android_bundle_input_invalid"):
            verifier.verify_bundle(path, bundletool, code=7, sha=sha, certificate=CERTIFICATE)

    run.assert_not_called()
