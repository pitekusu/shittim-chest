# SPDX-License-Identifier: MIT
"""Mirror the locked application's public certifi CA into the CDK helper layer."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path

import certifi

from tools.tls_policy import pem_certificates, require_safe_bundle

DESTINATION = Path(__file__).resolve().parents[1] / "infra/assets/cdk-helper-tls/shittim-tls"


def snapshot() -> dict[str, bytes]:
    """Use installed locked distribution data; never download or add a CA."""
    distribution = importlib.metadata.distribution("certifi")
    source = Path(certifi.where()).read_bytes()
    require_safe_bundle(source)
    # Publish only PEM blocks. Certifi's human-readable serial-number comments
    # otherwise resemble private identifiers to the public-surface scanner.
    bundle = b"".join(pem_certificates(source))
    metadata = {
        "source": "certifi",
        "version": distribution.version,
        "sha256": hashlib.sha256(bundle).hexdigest(),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "ca_count": len(pem_certificates(bundle)),
    }
    license_file = next(
        item
        for item in distribution.files or ()
        if str(item).endswith(".dist-info/licenses/LICENSE")
    )
    return {
        "cacert.pem": bundle,
        "metadata.json": (json.dumps(metadata, indent=2) + "\n").encode(),
        "LICENSE.certifi": distribution.locate_file(license_file).read_bytes(),
    }


def main() -> None:
    """Check in CI, or explicitly update after a frozen certifi lock update."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    for name, content in snapshot().items():
        target = DESTINATION / name
        if arguments.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        elif not target.is_file() or target.read_bytes() != content:
            raise SystemExit("CDK helper CA snapshot differs from locked certifi; run --write")
    print("CDK helper CA snapshot matches locked certifi")


if __name__ == "__main__":
    main()
