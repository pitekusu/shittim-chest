"""Offline regression for the actual public-service SSLContext trust boundary."""

from __future__ import annotations

import hashlib
import shutil
import ssl
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shittim_chest.adapters.tls import create_public_tls_context

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tls"
DENIED_ROOTS = frozenset(
    {
        "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
        "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    }
)
VERIFY_TIME = int(datetime(2026, 10, 21, 12, tzinfo=UTC).timestamp())


def test_public_tls_keeps_hostname_checks_and_excludes_os_and_environment_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Even invalid ambient CA paths must not replace the reviewed public store.
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "untrusted.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))

    context = create_public_tls_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    fingerprints = {hashlib.sha256(cert).hexdigest() for cert in context.get_ca_certs(True)}
    assert fingerprints
    assert not fingerprints & DENIED_ROOTS
    assert create_public_tls_context() is not context


@pytest.mark.parametrize(
    "chain",
    sorted([*FIXTURES.glob("emudhra/*.pem"), *FIXTURES.glob("valid/*.pem")]),
    ids=lambda p: p.name,
)
def test_public_tls_rejects_compromised_chains_and_accepts_ordinary_services(
    chain: Path, tmp_path: Path
) -> None:
    openssl = shutil.which("openssl")
    assert openssl is not None, "offline TLS regression requires OpenSSL"
    bundle = tmp_path / "actual-context.pem"
    bundle.write_text(
        "".join(
            ssl.DER_cert_to_PEM_cert(cert)
            for cert in create_public_tls_context().get_ca_certs(True)
        ),
        encoding="ascii",
    )
    result = subprocess.run(  # noqa: S603 - fixed executable, public offline certificate inputs.
        [
            openssl,
            "verify",
            "-no-CApath",
            "-no-CAstore",
            "-CAfile",
            str(bundle),
            "-attime",
            str(VERIFY_TIME),
            "-purpose",
            "sslserver",
            "-verify_hostname",
            chain.stem,
            "-untrusted",
            str(chain),
            str(chain),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if chain.parent.name == "emudhra":
        assert result.returncode != 0
        assert "error 20 " in result.stderr
    else:
        assert result.returncode == 0, result.stderr
