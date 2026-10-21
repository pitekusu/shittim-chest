# SPDX-License-Identifier: MIT
"""Remove the published emSign incident trust anchors, including cross-signs.

This is shared build glue, not a certificate verifier. OpenSSL parses public
certificates; the TLS implementations retain their normal chain and host checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import subprocess
from functools import lru_cache
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("emudhra_ca_distrust.json")
PEM_PATTERN = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----"
)


class TrustPolicyError(ValueError):
    """A public trust store cannot satisfy the incident policy."""


@lru_cache(maxsize=1)
def denied_identifiers() -> tuple[frozenset[str], frozenset[str]]:
    """Use certificate AND public-key identities, not an issuer-name substring."""
    entries = json.loads(POLICY_PATH.read_text())["certificates"]
    fingerprints = frozenset(entry["sha256"] for entry in entries)
    keys = frozenset(entry["spki_sha256"] for entry in entries)
    if len(fingerprints) < 4 or any(
        re.fullmatch("[0-9a-f]{64}", digest) is None for digest in fingerprints | keys
    ):
        raise TrustPolicyError("tls_distrust_policy_invalid")
    return fingerprints, keys


def pem_certificates(data: bytes) -> tuple[bytes, ...]:
    """Reject malformed bundles rather than silently omitting unparsed entries."""
    certificates = tuple(match.group(0) + b"\n" for match in PEM_PATTERN.finditer(data))
    if not certificates or PEM_PATTERN.sub(b"", data).count(b"-----"):
        raise TrustPolicyError("tls_ca_bundle_invalid")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=b"".join(certificates).decode("ascii"))
    except (ValueError, ssl.SSLError, UnicodeError) as error:
        raise TrustPolicyError("tls_ca_bundle_invalid") from error
    return certificates


def _openssl(command: list[str], data: bytes) -> bytes:
    result = subprocess.run(  # noqa: S603 - fixed OpenSSL operations on public certs.
        ["openssl", *command],  # noqa: S607 - installed system OpenSSL.
        input=data,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        raise TrustPolicyError("tls_ca_certificate_invalid")
    return result.stdout


def is_denied(certificate: bytes) -> bool:
    """Block roots and compromised intermediate keys even if reissued/cross-signed."""
    fingerprints, keys = denied_identifiers()
    try:
        der = ssl.PEM_cert_to_DER_cert(certificate.decode("ascii"))
    except (ValueError, UnicodeError) as error:
        raise TrustPolicyError("tls_ca_certificate_invalid") from error
    if hashlib.sha256(der).hexdigest() in fingerprints:
        return True
    public_key = _openssl(["x509", "-pubkey", "-noout"], certificate)
    spki = _openssl(["pkey", "-pubin", "-outform", "DER"], public_key)
    return hashlib.sha256(spki).hexdigest() in keys


def filter_bundle(data: bytes) -> bytes:
    """Only remove trust; never download or add a certificate."""
    retained = tuple(cert for cert in pem_certificates(data) if not is_denied(cert))
    if not retained:
        raise TrustPolicyError("tls_ca_bundle_empty")
    return b"".join(retained)


def require_safe_bundle(data: bytes) -> None:
    """Fail closed when a provider or an override reintroduces incident roots."""
    if any(is_denied(cert) for cert in pem_certificates(data)):
        raise TrustPolicyError("tls_distrusted_ca_present")
