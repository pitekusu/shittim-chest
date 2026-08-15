"""Deterministic Records Lambda bundle tests."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from tools.build_records_bundle import build_bundle


def test_bundle_is_byte_identical_and_excludes_python_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "shittim_records").mkdir()
    (source / "shittim_records" / "handler.py").write_text("VALUE = 1\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "handler.pyc").write_bytes(b"unstable")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    build_bundle(source, first)
    build_bundle(source, second)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["shittim_records/handler.py"]
        info = archive.getinfo("shittim_records/handler.py")
        assert info.date_time == (2020, 1, 1, 0, 0, 0)


def test_bundle_rejects_empty_source_and_nested_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(ValueError, match="empty"):
        build_bundle(source, tmp_path / "empty.zip")

    (source / "value.py").write_text("VALUE = 1\n")
    with pytest.raises(ValueError, match="outside"):
        build_bundle(source, source / "bundle.zip")
