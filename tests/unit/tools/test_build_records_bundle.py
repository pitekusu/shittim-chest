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


def test_bundle_excludes_uv_cache_metadata_from_wheel_records(tmp_path: Path) -> None:
    first_source = tmp_path / "first-source"
    second_source = tmp_path / "second-source"
    for source, timestamp in ((first_source, "1"), (second_source, "2")):
        dist_info = source / "example-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (source / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
        (dist_info / "uv_cache.json").write_text(f'{{"timestamp":{timestamp}}}', encoding="utf-8")
        (dist_info / "RECORD").write_text(
            "example.py,sha256=stable,10\n"
            f"example-1.0.dist-info/uv_cache.json,sha256={timestamp},15\n"
            "example-1.0.dist-info/RECORD,,\n",
            encoding="utf-8",
        )

    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    build_bundle(first_source, first)
    build_bundle(second_source, second)

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert "example-1.0.dist-info/uv_cache.json" not in archive.namelist()
        record = archive.read("example-1.0.dist-info/RECORD")
        assert b"uv_cache.json" not in record


def test_bundle_rejects_cross_distribution_uv_cache_record(tmp_path: Path) -> None:
    source = tmp_path / "source"
    dist_info = source / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "RECORD").write_text(
        "other-1.0.dist-info/uv_cache.json,sha256=unstable,15\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected uv cache path"):
        build_bundle(source, tmp_path / "bundle.zip")
