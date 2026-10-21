#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify TLS trust in a built image using handshakes without HTTP or credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import re
import socket
import ssl
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

INCIDENT_HOSTS = tuple(
    f"{kind}-{issuer}{suffix}-emudhra.we-have-a-problem.com"
    for kind in ("dv", "ov", "ev")
    for issuer in ("g3b", "g3c")
    for suffix in ("", "-aaa")
)
NORMAL_HOSTS = ("api.openai.com", "discord.com", "www.python.org")
DENIED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
}
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT_SECONDS = 10
RETRY_BACKOFF_SECONDS = 0.25
MAX_CONCURRENT_HANDSHAKES = 4
# 45 checks / four slots, each with three 10s attempts, 0.25s + 0.5s
# backoff and at most 1s TLS shutdown: <=381s, plus container startup.
DOCKER_TIMEOUT_SECONDS = 420


def _load_capath(context: ssl.SSLContext, directory: str | None) -> None:
    if not directory or not Path(directory).is_dir():
        raise ValueError("runtime_capath_missing")
    for path in Path(directory).iterdir():
        if re.fullmatch(r"[0-9a-fA-F]{8}\.[0-9]+", path.name) and path.is_file():
            context.load_verify_locations(cafile=str(path))


def _contexts() -> dict[str, ssl.SSLContext]:
    from shittim_chest.adapters.tls import create_public_tls_context

    capath = ssl.get_default_verify_paths().capath
    system = ssl.create_default_context()
    _load_capath(system, capath)
    directory_only = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    directory_only.load_verify_locations(capath=capath)
    _load_capath(directory_only, capath)
    return {
        "system": system,
        "system_capath_only": directory_only,
        "application": create_public_tls_context(),
    }


def _store_metadata(context: ssl.SSLContext) -> dict[str, object]:
    certificates = sorted(set(context.get_ca_certs(binary_form=True)))
    fingerprints = {hashlib.sha256(cert).hexdigest() for cert in certificates}
    public = "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in certificates)
    return {
        "ca_count": len(certificates),
        "trust_set_sha256": hashlib.sha256(public.encode("ascii")).hexdigest(),
        "incident_roots_absent": fingerprints.isdisjoint(DENIED_ROOTS),
        "verify_required": context.verify_mode == ssl.CERT_REQUIRED,
        "check_hostname": context.check_hostname,
    }


async def _handshake_attempt(context: ssl.SSLContext, host: str, accept: bool) -> dict[str, object]:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                443,
                ssl=context,
                server_hostname=host,
                happy_eyeballs_delay=0.25,
                interleave=1,
            ),
            timeout=ATTEMPT_TIMEOUT_SECONDS,
        )
    except ssl.SSLCertVerificationError as error:
        return {
            "result": "certificate_rejected",
            "reason": "certificate_verification_failed",
            "verify_code": error.verify_code,
            "passed": not accept and error.verify_code in {20, 23},
            "retryable": False,
        }
    except ssl.SSLError:
        # Protocol failures are not evidence of CA distrust or transport noise.
        return {
            "result": "tls_protocol_error",
            "reason": "tls_protocol_error",
            "passed": False,
            "retryable": False,
        }
    except (TimeoutError, OSError) as error:
        if isinstance(error, TimeoutError):
            reason = "timeout"
        elif isinstance(error, socket.gaierror):
            reason = "dns_error"
        elif isinstance(error, ConnectionResetError):
            reason = "connection_reset"
        elif isinstance(error, ConnectionRefusedError):
            reason = "connection_refused"
        else:
            reason = "transport_error"
        return {
            "result": "unverified_connection",
            "reason": reason,
            "passed": False,
            "retryable": True,
        }
    else:
        writer.close()
        # The certificate-verifying handshake is already complete. There is no
        # HTTP data to flush, including on a missing close_notify.
        with suppress(TimeoutError, OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        return {
            "result": "accepted",
            "reason": "tls_handshake_completed",
            "passed": accept,
            "retryable": False,
        }


async def _handshake(context: ssl.SSLContext, host: str, accept: bool) -> dict[str, object]:
    attempts = []
    for number in range(1, MAX_ATTEMPTS + 1):
        result = await _handshake_attempt(context, host, accept)
        attempts.append({"attempt": number, **result})
        if not result["retryable"] or number == MAX_ATTEMPTS:
            break
        await asyncio.sleep(RETRY_BACKOFF_SECONDS * number)
    return {
        "host": host,
        "expected": "accept" if accept else "reject",
        **result,
        "attempts": attempts,
    }


async def _probe() -> dict[str, object]:
    contexts = _contexts()
    stores = {name: _store_metadata(context) for name, context in contexts.items()}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_HANDSHAKES)

    async def bounded(name: str, context: ssl.SSLContext, host: str, accept: bool):
        async with semaphore:
            return {"store": name, **await _handshake(context, host, accept)}

    rows = await asyncio.gather(
        *(
            bounded(name, context, host, accept)
            for name, context in contexts.items()
            for hosts, accept in ((INCIDENT_HOSTS, False), (NORMAL_HOSTS, True))
            for host in hosts
        )
    )
    safe_stores = all(
        store["ca_count"]
        and store["incident_roots_absent"]
        and store["verify_required"]
        and store["check_hostname"]
        for store in stores.values()
    )
    return {
        "status": "passed" if safe_stores and all(row["passed"] for row in rows) else "failed",
        "python": sys.version.split()[0],
        "openssl": ssl.OPENSSL_VERSION,
        "native_machine": platform.machine(),
        "stores": stores,
        "checks": rows,
        "http_sent": False,
        "credentials_supplied": False,
    }


def _docker(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker command, no shell/credential arguments.
        ["docker", *arguments],  # noqa: S607 - runner-installed Docker.
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_TIMEOUT_SECONDS,
    )


def verify_image(image: str, output: Path) -> dict[str, object]:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", image) is None:
        raise ValueError("runtime_image_reference_invalid")
    # Keep Docker's ID name literal: the containerd store may return a manifest
    # digest here. It must not be mislabeled as the raw image configuration hash.
    identity = _docker(
        [
            "image",
            "inspect",
            "--format",
            '{"docker_image_id":{{json .Id}},"architecture":{{json .Architecture}},'
            '"rootfs_diff_ids":{{json .RootFS.Layers}}}',
            image,
        ]
    )
    if identity.returncode:
        raise ValueError("runtime_image_inspection_failed")
    report = json.loads(identity.stdout)
    report["image_reference"] = image
    result = _docker(
        [
            "run",
            "--rm",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--mount",
            f"type=bind,source={Path(__file__).resolve()},target=/run/verify_image_tls.py,readonly",
            "--entrypoint",
            "python",
            image,
            "/run/verify_image_tls.py",
            "--in-container",
        ]
    )
    try:
        report.update(json.loads(result.stdout))
    except ValueError, TypeError:
        report.update(status="unverified", error="runtime_tls_probe_failed")
    if result.returncode:
        report["status"] = "failed"
    configuration = output.parent / "production-image-config-digest.txt"
    if configuration.is_file():
        digest = configuration.read_text().strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise ValueError("runtime_config_digest_invalid")
        report["image_config_digest"] = digest
        report["config_digest_source"] = "CI saved raw image configuration"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.in_container:
        try:
            report = asyncio.run(_probe())
        except Exception:
            report = {"status": "unverified", "error": "runtime_tls_context_unavailable"}
        print(json.dumps(report))
    else:
        if args.image is None or args.output is None:
            parser.error("--image and --output are required")
        try:
            report = verify_image(args.image, args.output)
        except ValueError, OSError, subprocess.SubprocessError:
            print("runtime image TLS verification unavailable", file=sys.stderr)
            return 1
        print(f"Runtime image TLS verification: {report['status']}")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
