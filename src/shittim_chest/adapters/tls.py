"""Public-service TLS trust, independent of managed-runtime OS CA updates."""

from __future__ import annotations

import ssl

import certifi


def create_public_tls_context() -> ssl.SSLContext:
    """Keep hostname verification and use only the maintained certifi bundle.

    HTTPX2 otherwise uses system truststore, which can retain compromised CAs
    after certifi is updated. An explicit cafile avoids merging those OS roots
    or SSL_CERT_FILE/SSL_CERT_DIR overrides into public-service connections.
    """

    return ssl.create_default_context(cafile=certifi.where())
