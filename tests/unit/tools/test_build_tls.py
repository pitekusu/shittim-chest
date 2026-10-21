"""Trust source regressions: cross-signs, CA directories and tool bootstrapping."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from tools import configure_build_tls as build
from tools import tls_policy as policy


@pytest.fixture
def certificates() -> tuple[bytes, bytes, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local policy test")])

    def certificate(serial: int) -> bytes:
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, None)
            .public_bytes(serialization.Encoding.PEM)
        )

    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return certificate(1), certificate(2), hashlib.sha256(spki).hexdigest()


def test_public_key_distrust_covers_reissued_certificates(
    certificates: tuple[bytes, bytes, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, reissued, spki = certificates
    fingerprint = hashlib.sha256(
        x509.load_pem_x509_certificate(first).public_bytes(serialization.Encoding.DER)
    ).hexdigest()
    monkeypatch.setattr(
        policy, "denied_identifiers", lambda: (frozenset({fingerprint}), frozenset({spki}))
    )
    assert first != reissued
    assert policy.is_denied(first)
    assert policy.is_denied(reissued)
    with pytest.raises(policy.TrustPolicyError, match="tls_ca_bundle_empty"):
        policy.filter_bundle(first + reissued)


def test_removes_only_the_affected_certificate(
    certificates: tuple[bytes, bytes, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    affected, retained, _ = certificates
    monkeypatch.setattr(policy, "is_denied", lambda cert: cert == affected)
    assert policy.filter_bundle(affected + retained) == retained
    with pytest.raises(policy.TrustPolicyError, match="tls_distrusted_ca_present"):
        policy.require_safe_bundle(retained + affected)
    policy.require_safe_bundle(retained)


@pytest.mark.parametrize(
    "data", (b"", b"-----BEGIN CERTIFICATE-----\nbroken", b"unexpected object")
)
def test_malformed_bundles_do_not_silently_drop_certificates(data: bytes) -> None:
    with pytest.raises(policy.TrustPolicyError, match="tls_ca_bundle_invalid"):
        policy.filter_bundle(data)


def test_tool_store_excludes_the_default_ca_directory(
    tmp_path: Path, certificates: tuple[bytes, bytes, str]
) -> None:
    source = tmp_path / "existing.pem"
    source.write_bytes(certificates[0])
    values = build.configure_environment(tmp_path / "selected", source)
    assert Path(values["SSL_CERT_DIR"]).is_dir()
    assert not tuple(Path(values["SSL_CERT_DIR"]).iterdir())
    assert values["PIP_CERT"] == values["REQUESTS_CA_BUNDLE"] == values["SSL_CERT_FILE"]
    assert values["PIP_USE_DEPRECATED"] == "legacy-certs"
    assert values["GIT_SSL_CAPATH"] == values["SSL_CERT_DIR"]
    assert values["AWS_CA_BUNDLE"] == values["SSL_CERT_FILE"]


def test_unexpected_extra_ca_directory_is_rejected(
    tmp_path: Path, certificates: tuple[bytes, bytes, str]
) -> None:
    source = tmp_path / "existing.pem"
    source.write_bytes(certificates[0])
    output = tmp_path / "selected"
    (output / "empty-ca").mkdir(parents=True)
    (output / "empty-ca/extra.pem").write_bytes(certificates[0])
    with pytest.raises(policy.TrustPolicyError, match="tls_build_ca_directory_not_empty"):
        build.configure_environment(output, source)


@pytest.mark.parametrize(
    "name,value",
    (
        ("NODE_EXTRA_CA_CERTS", "/unknown.pem"),
        ("PIP_TRUSTED_HOST", "example.invalid"),
        ("UV_INSECURE_HOST", "example.invalid"),
        ("NODE_TLS_REJECT_UNAUTHORIZED", "0"),
        ("GIT_SSL_NO_VERIFY", "true"),
        ("VP_INSECURE_TLS", "true"),
        ("NODE_OPTIONS", "--use-system-ca"),
        ("JAVA_TOOL_OPTIONS", "-Djavax.net.ssl.trustStore=/unknown"),
        ("GRADLE_OPTS", "-Djavax.net.ssl.trustStore=/unknown"),
    ),
)
def test_additive_or_insecure_overrides_fail_closed(name: str, value: str) -> None:
    with pytest.raises(policy.TrustPolicyError, match="tls_build_unsafe_override"):
        build._check_overrides({name: value})


def test_default_execution_cannot_mutate_the_local_os(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(policy.TrustPolicyError, match="tls_system_update_requires_hosted_ubuntu"):
        build.update_runner_system(tmp_path, docker=True)


def test_java_discovery_uses_the_existing_pinned_toolchain_without_java_home(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".java-version").write_text("21.0.12.1+1\n")
    data = tmp_path / "data"
    jdk = data / "shittim-chest/android-toolchain/jdk-21.0.12.1+1"
    (jdk / "bin").mkdir(parents=True)
    for tool in ("java", "javac", "keytool"):
        binary = jdk / "bin" / tool
        binary.write_bytes(b"invented existing executable")
        binary.chmod(0o700)
    assert build.discover_java_home({"XDG_DATA_HOME": str(data), "PATH": ""}, project) == jdk
    with pytest.raises(policy.TrustPolicyError, match="tls_java_home_missing"):
        build.discover_java_home(
            {"JAVA_HOME": str(tmp_path / "missing"), "XDG_DATA_HOME": str(data)}, project
        )


def test_our_inherited_java_store_is_verified_then_replaced_without_duplicate_options(
    tmp_path: Path, certificates: tuple[bytes, bytes, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    first, other, _ = certificates
    exported = first

    def run(command: list[str], **_: object) -> bytes:
        if command[0].endswith("keytool"):
            return exported
        Path(command[-1]).write_bytes(b"invented public CA store")
        return b""

    monkeypatch.setattr(build, "_run", run)
    environment = {"JAVA_HOME": "/invented-jdk", "JAVA_TOOL_OPTIONS": "-Djava.io.tmpdir=/disk/tmp"}
    environment.update(build.configure_java(tmp_path, environment))
    environment.update(build.configure_java(tmp_path, environment))
    assert environment["JAVA_TOOL_OPTIONS"].count("-Djavax.net.ssl.trustStore=") == 1
    assert "-Djava.io.tmpdir=/disk/tmp" in environment["JAVA_TOOL_OPTIONS"]
    exported = other
    with pytest.raises(policy.TrustPolicyError, match="tls_build_unsafe_override"):
        build.configure_java(tmp_path, environment)


@pytest.mark.parametrize("unsafe", ("store", "extra-directory", "bad-certificate"))
def test_buildkit_checks_the_actual_daemon_store(
    certificates: tuple[bytes, bytes, str], monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    values = {
        "SSL_CERT_FILE": "/etc/buildkit/certs/docker.io/ca.pem",
        "SSL_CERT_DIR": "/etc/buildkit/no-extra-ca",
    }
    if unsafe == "store":
        values["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"

    def run(command: list[str], **_: object) -> bytes:
        if command[1] == "ps":
            return b"buildx_buildkit_test0\n"
        if command[1] == "inspect":
            return json.dumps([f"{key}={value}" for key, value in values.items()]).encode()
        if "cat" in command:
            return certificates[0]
        if unsafe == "extra-directory":
            raise policy.TrustPolicyError("tls_build_tool_failed")
        return b""

    monkeypatch.setattr(build, "_run", run)
    if unsafe == "bad-certificate":
        monkeypatch.setattr(policy, "is_denied", lambda _: True)
    with pytest.raises(policy.TrustPolicyError):
        build.check_buildkit("test")


def test_every_checked_out_job_configures_tls_before_later_actions() -> None:
    directory = Path(__file__).resolve().parents[3] / ".github/workflows"
    for workflow in directory.glob("*.yml"):
        text = workflow.read_text()
        assert "  NODE_OPTIONS: --use-openssl-ca\n" in text, workflow.name
        for block in re.split(r"(?m)^  [a-zA-Z][\w-]*:\s*$", text.split("\njobs:\n", 1)[1]):
            if "uses: actions/checkout@" not in block:
                continue
            steps = block.split("    steps:\n", 1)[1]
            guard = (
                "      - name: Update and verify system trust before checkout\n"
                "        working-directory: ${{ runner.temp }}\n"
            )
            assert guard in steps, workflow.name
            assert steps.index(guard) < steps.index("uses:"), workflow.name
            checkout = block.index("uses: actions/checkout@")
            before = block[:checkout]
            assert "name: Update and verify system trust before checkout\n" in before
            assert "sudo apt-get install --yes --only-upgrade" in before
            assert "refusing checkout" in before
            following = block[checkout:].split("\n      - ", 1)[1]
            if following.startswith("name: Require an explicit CI scope\n"):
                following = following.split("\n      - ", 1)[1]
            assert following.startswith("name: Configure the incident TLS trust policy\n"), (
                workflow.name
            )
            if "uses: actions/setup-java@" in block:
                java = block.index("uses: actions/setup-java@")
                following_java = block[java:].split("\n      - ", 1)[1]
                assert following_java.startswith("name: Select the filtered JDK trust store\n"), (
                    workflow.name
                )


@pytest.mark.parametrize("unsafe", (False, True))
def test_precheckout_guard_rejects_an_unfixed_ca_package(
    tmp_path: Path, certificates: tuple[bytes, bytes, str], unsafe: bool
) -> None:
    repository = Path(__file__).resolve().parents[3]
    workflow = (repository / ".github/workflows/ci.yml").read_text()
    script = workflow.split("python3 - <<'PY_TLS'\n", 1)[1].split("          PY_TLS\n", 1)[0]
    script = textwrap.dedent(script).replace("/etc/ssl/certs", str(tmp_path))
    data = certificates[0]
    if unsafe:
        data += (repository / "tests/fixtures/tls/roots/emsign-g3.pem").read_bytes()
    (tmp_path / "ca-certificates.crt").write_bytes(data)
    result = subprocess.run(  # noqa: S603 - only the repository's inline CA guard.
        [sys.executable, "-c", script], capture_output=True, check=False
    )
    assert result.returncode == int(unsafe)
    if unsafe:
        assert b"refusing checkout" in result.stderr
    else:
        assert b"distrust verified before checkout" in result.stdout
