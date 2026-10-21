"""The runtime CA overlay must remove stale hashed aliases as well as the bundle."""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import pytest
from tools.install_runtime_ca import install_runtime_ca

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/tls"


def source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "upstream"
    (source / "certs").mkdir(parents=True)
    (source / "anchors").mkdir()
    (source / "certs/ca-certificates.crt").write_bytes(Path(certifi.where()).read_bytes())
    first = ssl.create_default_context(cafile=certifi.where()).get_ca_certs(True)[0]
    (source / "anchors/ordinary.crt").write_text(ssl.DER_cert_to_PEM_cert(first))
    (source / "certs/ordinary.pem").symlink_to("/usr/share/ca-certificates/ordinary.crt")
    (source / "certs/12345678.0").symlink_to("ordinary.pem")
    (source / "ca-certificates.conf").write_text("ordinary.crt\n")
    return source


def test_overlay_removes_old_aliases_and_copies_upstream_configuration(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    root = tmp_path / "runtime"
    old = root / "etc/ssl/certs"
    old.mkdir(parents=True)
    (old / "old-hash.0").symlink_to("/usr/share/ca-certificates/old.crt")
    (old / "ca-certificates.crt").write_text("stale")
    (root / "usr/share/ca-certificates").mkdir(parents=True)
    (root / "usr/share/ca-certificates/old.crt").write_text("stale")

    install_runtime_ca(source, root, source_date_epoch=123)

    assert not (old / "old-hash.0").is_symlink()
    assert not (root / "usr/share/ca-certificates/old.crt").exists()
    assert (old / "ca-certificates.crt").read_bytes() == Path(certifi.where()).read_bytes()
    assert (old / "ordinary.pem").readlink() == Path("/usr/share/ca-certificates/ordinary.crt")
    assert (old / "12345678.0").readlink() == Path("ordinary.pem")
    assert (root / "etc/ca-certificates.conf").read_text() == "ordinary.crt\n"
    assert (old / "ca-certificates.crt").stat().st_mtime == 123
    assert (root / "etc/ca-certificates.conf").stat().st_mtime == 123


def test_compromised_upstream_bundle_is_rejected_before_modifying_runtime(tmp_path: Path) -> None:
    source = source_tree(tmp_path)
    bundle = source / "certs/ca-certificates.crt"
    bundle.write_bytes(bundle.read_bytes() + (FIXTURES / "roots/emsign-g3.pem").read_bytes())
    root = tmp_path / "runtime"

    with pytest.raises(ValueError, match="compromised roots"):
        install_runtime_ca(source, root, source_date_epoch=123)

    assert not root.exists()
