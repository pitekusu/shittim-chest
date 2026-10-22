#!/usr/bin/env python3
"""Transfer the validated upstream CA trees, including regenerated hashed aliases."""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
from pathlib import Path

from tools.transfer_tree_deterministically import transfer_tree_deterministically

DENIED_ROOTS = frozenset(
    {
        "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
        "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    }
)


def install_runtime_ca(source: Path, root: Path, *, source_date_epoch: int) -> None:
    """Replace both CA trees; merging would retain old hashed trust aliases.

    Source is a read-only bind from the digest-pinned, patched upstream builder.
    Retain dpkg's actual package inventory: this is a configuration overlay,
    not a claim that the runtime ca-certificates package has been upgraded.
    """

    bundle = source / "certs/ca-certificates.crt"
    context = ssl.create_default_context(cafile=str(bundle))
    fingerprints = {hashlib.sha256(cert).hexdigest() for cert in context.get_ca_certs(True)}
    if not fingerprints or fingerprints & DENIED_ROOTS:
        raise ValueError("upstream runtime CA source still trusts compromised roots")
    configuration = (source / "ca-certificates.conf").read_bytes()
    if source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be non-negative")
    for relative_source, relative_destination in (
        ("anchors", "usr/share/ca-certificates"),
        ("certs", "etc/ssl/certs"),
    ):
        source_tree = source / relative_source
        if source_tree.is_symlink() or not source_tree.is_dir():
            raise ValueError("upstream runtime CA tree is missing")
        destination = root / relative_destination
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        transfer_tree_deterministically(
            source_tree,
            destination,
            source_date_epoch=source_date_epoch,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    destination_conf = root / "etc/ca-certificates.conf"
    destination_conf.write_bytes(configuration)
    destination_conf.chmod(0o644)
    os.utime(destination_conf, (source_date_epoch, source_date_epoch))


if __name__ == "__main__":
    install_runtime_ca(
        Path("/run/updated-ca"),
        Path("/"),
        source_date_epoch=int(os.environ.get("SOURCE_DATE_EPOCH", "0")),
    )
