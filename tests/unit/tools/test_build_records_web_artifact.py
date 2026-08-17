"""Deterministic Records Web artifact tests."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest
from tools.build_records_web_artifact import build_artifact


def test_artifact_is_deterministic_and_path_safe(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<main>Records</main>\n", encoding="utf-8")
    (source / "assets" / "app.js").write_text("export {};\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    build_artifact(source, first)
    build_artifact(source, second)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["assets/app.js", "index.html"]
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert archive.read("index.html") == b"<main>Records</main>\n"


def test_artifact_rejects_missing_entrypoint(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()

    with pytest.raises(ValueError, match=r"index\.html"):
        build_artifact(source, tmp_path / "records.zip")


def test_artifact_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "dist"
    source.mkdir()
    (source / "index.html").write_text("ok", encoding="utf-8")
    (source / "linked").symlink_to(source / "index.html")

    with pytest.raises(ValueError, match="regular files"):
        build_artifact(source, tmp_path / "records.zip")
