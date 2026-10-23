"""Exercise botocore's actual certifi-import fallback without AWS requests."""

from __future__ import annotations

import importlib.util
import shutil
import ssl
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import patch

import botocore.httpsession
import certifi
import pytest
from tools.tls_policy import require_safe_bundle

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tls"
VERIFY_TIME = int(datetime(2026, 10, 21, 12, tzinfo=UTC).timestamp())


@pytest.fixture(scope="module")
def without_certifi() -> ModuleType:
    # Load the installed transport in its own namespace so no other test's
    # botocore module or certifi installation is modified.
    spec = importlib.util.spec_from_file_location(
        "botocore_without_certifi", botocore.httpsession.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"certifi": None}):
        spec.loader.exec_module(module)
    return module


def test_normal_certifi_and_explicit_fallback_selection_are_preserved() -> None:
    # The SDK accepts paths although its installed stub only declares bool.
    get_cert_path = cast(Callable[[bool | str], str], botocore.httpsession.get_cert_path)
    assert get_cert_path(True) == certifi.where()
    assert (
        get_cert_path(botocore.httpsession.DEFAULT_CA_BUNDLE)
        == botocore.httpsession.DEFAULT_CA_BUNDLE
    )


def test_missing_certifi_selects_safe_bundle_with_required_verification(
    without_certifi: ModuleType,
) -> None:
    bundle = without_certifi.get_cert_path(True)
    assert bundle == without_certifi.DEFAULT_CA_BUNDLE
    require_safe_bundle(Path(bundle).read_bytes())

    context = ssl.create_default_context(cafile=bundle)
    assert context.get_ca_certs()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname

    connection = SimpleNamespace()
    session = without_certifi.URLLib3Session()
    try:
        session._setup_ssl_cert(connection, "https://offline.invalid", True)
        assert connection.cert_reqs == "CERT_REQUIRED"
        assert connection.ca_certs == bundle
    finally:
        session.close()


@pytest.mark.parametrize(
    "chain",
    sorted([*FIXTURES.glob("emudhra/*.pem"), *FIXTURES.glob("valid/*.pem")]),
    ids=lambda path: path.name,
)
def test_actual_fallback_rejects_incident_chains_and_accepts_valid_chains(
    without_certifi: ModuleType, chain: Path
) -> None:
    openssl = shutil.which("openssl")
    assert openssl is not None, "offline TLS regression requires OpenSSL"
    result = subprocess.run(  # noqa: S603 - public offline fixtures, fixed OpenSSL operation.
        [
            openssl,
            "verify",
            "-no-CApath",
            "-no-CAstore",
            "-CAfile",
            without_certifi.get_cert_path(True),
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
