# SPDX-License-Identifier: MIT
"""Apply the emSign distrust policy to disposable CI or a scoped local command.

The system mutation is opt-in and restricted to GitHub-hosted Ubuntu runners.
Local runs only write the requested directory and alter the child environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
from pathlib import Path

from tools.tls_policy import (
    TrustPolicyError,
    filter_bundle,
    is_denied,
    pem_certificates,
    require_safe_bundle,
)

JAVA_OPTIONS = (
    "JAVA_TOOL_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "_JAVA_OPTIONS",
    "JAVA_OPTS",
    "GRADLE_OPTS",
)


def _run(
    command: list[str], *, data: bytes | None = None, environment: dict[str, str] | None = None
) -> bytes:
    result = subprocess.run(  # noqa: S603 - explicit local tools, no shell or credentials.
        command, input=data, capture_output=True, check=False, timeout=120, env=environment
    )
    if result.returncode:
        raise TrustPolicyError("tls_build_tool_failed")
    return result.stdout


def _source_bundle() -> Path:
    for candidate in (
        Path("/etc/ssl/certs/ca-certificates.crt"),
        Path("/etc/pki/tls/certs/ca-bundle.crt"),
        Path(ssl.get_default_verify_paths().openssl_cafile),
    ):
        if candidate.is_file():
            return candidate
    raise TrustPolicyError("tls_system_ca_bundle_missing")


def _check_overrides(environment: dict[str, str]) -> dict[str, str]:
    for name in ("NODE_EXTRA_CA_CERTS", "PIP_TRUSTED_HOST", "UV_INSECURE_HOST"):
        if environment.get(name):
            raise TrustPolicyError("tls_build_unsafe_override")
    if environment.get("NODE_TLS_REJECT_UNAUTHORIZED") == "0":
        raise TrustPolicyError("tls_build_unsafe_override")
    if environment.get("GIT_SSL_NO_VERIFY", "").lower() in {"true", "1"}:
        raise TrustPolicyError("tls_build_unsafe_override")
    if environment.get("VP_INSECURE_TLS", "").strip().lower() in {"true", "1", "yes", "on"}:
        raise TrustPolicyError("tls_build_unsafe_override")
    node_options = shlex.split(environment.get("NODE_OPTIONS", ""))
    if any(
        option.startswith(("--use-bundled-ca", "--use-system-ca", "--openssl-config"))
        for option in node_options
    ):
        raise TrustPolicyError("tls_build_unsafe_override")
    cleaned = dict(environment)
    selected = "javax.net.ssl.trustStore"
    for name in JAVA_OPTIONS:
        if selected not in environment.get(name, ""):
            continue
        if name != "JAVA_TOOL_OPTIONS":
            raise TrustPolicyError("tls_build_unsafe_override")
        tokens = shlex.split(environment[name])
        selection = [token for token in tokens if token.startswith("-D" + selected)]
        properties = dict(token[2:].split("=", 1) for token in selection if "=" in token)
        expected = {
            selected: environment.get("SHITTIM_BUILD_JAVA_TRUST_STORE", ""),
            selected + "Type": "PKCS12",
            selected + "Password": "public-only",
        }
        if len(selection) != 3 or properties != expected or not expected[selected]:
            raise TrustPolicyError("tls_build_unsafe_override")
        target = Path(expected[selected])
        if target.name != "java-ca.p12" or not target.is_file():
            raise TrustPolicyError("tls_build_unsafe_override")
        cleaned[name] = shlex.join(token for token in tokens if token not in selection)
        # A marker alone is not proof: read back the actual JDK store and compare
        # it with the selected public PEM before accepting our inherited setting.
        java_home = Path(environment["JAVA_HOME"])
        public = _run(
            [
                str(java_home / "bin/keytool"),
                "-list",
                "-rfc",
                "-keystore",
                str(target),
                "-storetype",
                "PKCS12",
                "-storepass",
                "public-only",
            ],
            environment=cleaned,
        )
        require_safe_bundle(public)
        expected_certs = pem_certificates(target.with_name("java-ca.pem").read_bytes())
        actual_certs = pem_certificates(public)
        if {ssl.PEM_cert_to_DER_cert(cert.decode()) for cert in expected_certs} != {
            ssl.PEM_cert_to_DER_cert(cert.decode()) for cert in actual_certs
        }:
            raise TrustPolicyError("tls_build_unsafe_override")
    return cleaned


def configure_environment(
    output: Path, source: Path | None = None, *, environment: dict[str, str] | None = None
) -> dict[str, str]:
    """Select the filtered store with each tool's supported CA controls."""
    _check_overrides(dict(os.environ) if environment is None else environment)
    output = output.resolve()
    if any(character in str(output) for character in ("\r", "\n")):
        raise TrustPolicyError("tls_build_directory_invalid")
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / "ca.pem"
    bundle.write_bytes(filter_bundle((source or _source_bundle()).read_bytes()))
    directory = output / "empty-ca"
    directory.mkdir(exist_ok=True)
    if any(directory.iterdir()):
        raise TrustPolicyError("tls_build_ca_directory_not_empty")
    # FILE alone leaves OpenSSL/Go's default CA directory active. Both must be set.
    values = dict.fromkeys(
        (
            "SSL_CERT_FILE",
            "AWS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "PIP_CERT",
            "GIT_SSL_CAINFO",
            "npm_config_cafile",
        ),
        str(bundle),
    )
    values.update(
        SSL_CERT_DIR=str(directory),
        GIT_SSL_CAPATH=str(directory),
        UV_SYSTEM_CERTS="false",
        # pip-audit starts ensurepip's old pip before upgrading it. Its vendored
        # certifi is additive in truststore mode; legacy-certs + PIP_CERT replaces
        # that source. This keeps chain verification, strict audit and hashes.
        PIP_USE_DEPRECATED="legacy-certs",
        PIP_CONFIG_FILE=os.devnull,
        npm_config_strict_ssl="true",
    )
    return values


def configure_java(output: Path, environment: dict[str, str]) -> dict[str, str]:
    """Filter the selected JDK's existing trust, never union it with another store."""
    environment = _check_overrides(environment)
    java_home = Path(environment["JAVA_HOME"])
    public = _run(
        [str(java_home / "bin/keytool"), "-list", "-rfc", "-cacerts", "-storepass", "changeit"],
        environment=environment,
    )
    filtered = output / "java-ca.pem"
    filtered.write_bytes(filter_bundle(public))
    target = output / "java-ca.p12"
    _run(
        [
            str(java_home / "bin/java"),
            str(Path(__file__).with_name("BuildTrustStore.java")),
            str(filtered),
            str(target),
        ],
        environment=environment,
    )
    option = (
        f'"-Djavax.net.ssl.trustStore={target}" -Djavax.net.ssl.trustStoreType=PKCS12'
        " -Djavax.net.ssl.trustStorePassword=public-only"
    )
    return {
        "JAVA_TOOL_OPTIONS": (environment.get("JAVA_TOOL_OPTIONS", "") + " " + option).strip(),
        "SHITTIM_BUILD_JAVA_TRUST_STORE": str(target),
    }


def discover_java_home(environment: dict[str, str], project: Path) -> Path:
    """Use the configured JDK, PATH, or the existing pinned local toolchain."""
    candidates: list[Path] = []
    if environment.get("JAVA_HOME"):
        candidates.append(Path(environment["JAVA_HOME"]))
    else:
        pin = project / ".java-version"
        if pin.is_file():
            version = pin.read_text().strip()
            if re.fullmatch(r"[0-9][0-9.+-]*", version) is None:
                raise TrustPolicyError("tls_java_version_invalid")
            data = Path(environment.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            candidates.append(data / "shittim-chest/android-toolchain" / ("jdk-" + version))
        java = shutil.which("java", path=environment.get("PATH"))
        if java:
            candidates.append(Path(java).resolve().parent.parent)
    for candidate in candidates:
        if all(
            os.access(candidate / "bin" / tool, os.X_OK) for tool in ("java", "javac", "keytool")
        ):
            return candidate.resolve()
    raise TrustPolicyError("tls_java_home_missing")


def update_runner_system(output: Path, *, docker: bool = False) -> None:
    """Use Ubuntu's standard blocklist/rebuild only on disposable hosted runners."""
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or "ID=ubuntu\n" not in Path("/etc/os-release").read_text()
    ):
        raise TrustPolicyError("tls_system_update_requires_hosted_ubuntu")
    config = Path("/etc/ca-certificates.conf")
    lines = config.read_text().splitlines()
    for index, line in enumerate(lines):
        if not line or line.startswith(("#", "!")):
            continue
        certificate = Path("/usr/share/ca-certificates") / line
        if any(is_denied(cert) for cert in pem_certificates(certificate.read_bytes())):
            lines[index] = "!" + line
    prepared = output / "ca-certificates.conf"
    prepared.write_text("\n".join(lines) + "\n")
    _run(["sudo", "-n", "install", "-m", "644", str(prepared), str(config)])
    # Locally added roots bypass ca-certificates.conf. Quarantine only a public
    # certificate that actually matches the incident policy; retain its bytes.
    disabled = output / "disabled"
    disabled.mkdir(exist_ok=True)
    for certificate in Path("/usr/local/share/ca-certificates").rglob("*.crt"):
        if any(is_denied(cert) for cert in pem_certificates(certificate.read_bytes())):
            import hashlib

            target = disabled / (hashlib.sha256(str(certificate).encode()).hexdigest() + ".crt")
            _run(["sudo", "-n", "mv", str(certificate), str(target)])
    _run(["sudo", "-n", "update-ca-certificates", "--fresh"])
    require_safe_bundle(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes())
    for certificate in Path("/etc/ssl/certs").glob("*.pem"):
        require_safe_bundle(certificate.read_bytes())
    if docker:
        for certificate in Path("/etc/docker/certs.d").rglob("*.crt"):
            require_safe_bundle(certificate.read_bytes())
        # A pre-existing Go daemon may have cached its original system roots.
        _run(["sudo", "-n", "systemctl", "restart", "docker"])


def check_buildkit(builder: str) -> None:
    """Check the actual daemon's explicit store, before any build/push is allowed."""
    names = _run(
        ["docker", "ps", "--filter", f"name=buildx_buildkit_{builder}", "--format", "{{.Names}}"]
    )
    containers = names.decode().splitlines()
    if not containers:
        raise TrustPolicyError("tls_buildkit_container_missing")
    for container in containers:
        result = json.loads(
            _run(["docker", "inspect", "--format", "{{json .Config.Env}}", container])
        )
        environment = dict(entry.split("=", 1) for entry in result if "=" in entry)
        if (
            environment.get("SSL_CERT_FILE") != "/etc/buildkit/certs/docker.io/ca.pem"
            or environment.get("SSL_CERT_DIR") != "/etc/buildkit/no-extra-ca"
        ):
            raise TrustPolicyError("tls_buildkit_store_override")
        data = _run(["docker", "exec", container, "cat", environment["SSL_CERT_FILE"]])
        require_safe_bundle(data)
        # The explicitly selected directory must not add a second trust source.
        _run(["docker", "exec", container, "sh", "-c", 'test ! -e "$SSL_CERT_DIR"'])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--runner-system", action="store_true")
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--java", action="store_true")
    parser.add_argument("--check-buildkit")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.check_buildkit:
        check_buildkit(args.check_buildkit)
        print("BuildKit CA policy verified")
        return 0
    if args.output is None:
        parser.error("--output is required")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.runner_system:
        update_runner_system(args.output, docker=args.docker)
    values = configure_environment(args.output, args.source)
    environment = dict(os.environ) | values
    if args.java:
        values.update(configure_java(args.output, environment))
        environment.update(values)
    # GitHub forbids setting NODE_OPTIONS via GITHUB_ENV. Workflows declare the
    # same supported option at workflow scope, including action post handlers.
    environment["NODE_OPTIONS"] = (
        environment.get("NODE_OPTIONS", "") + " --use-openssl-ca"
    ).strip()
    if args.github_env:
        with args.github_env.open("a") as stream:
            for name, value in values.items():
                if any(character in value for character in ("\r", "\n")):
                    raise TrustPolicyError("tls_build_environment_invalid")
                stream.write(f"{name}={value}\n")
    if args.command:
        command = args.command[1:] if args.command[0] == "--" else args.command
        return subprocess.run(command, env=environment, check=False).returncode  # noqa: S603
    print("Build TLS CA policy configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
