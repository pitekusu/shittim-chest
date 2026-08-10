"""Tests for deterministic filesystem-tree transfer."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from tools.transfer_tree_deterministically import (
    transfer_tree_deterministically,
    write_deterministic_tar,
)


def _source_tree(root: Path) -> Path:
    source = root / "source"
    package = source / "lib" / "python3.14" / "site-packages" / "example"
    package.mkdir(parents=True)
    module = package / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    module.chmod(0o640)
    (package / "module-link.py").symlink_to("module.py")
    return source


def _archive_digest(source: Path, archive: Path) -> str:
    write_deterministic_tar(
        source,
        archive,
        source_date_epoch=7,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def test_tar_is_identical_when_source_atimes_differ(tmp_path: Path) -> None:
    first = _source_tree(tmp_path / "first")
    second = _source_tree(tmp_path / "second")
    for index, path in enumerate((first, *first.rglob("*")), start=1):
        metadata = path.lstat()
        os.utime(
            path,
            ns=(index * 1_000_000_000, metadata.st_mtime_ns),
            follow_symlinks=False,
        )
    for index, path in enumerate((second, *second.rglob("*")), start=101):
        metadata = path.lstat()
        os.utime(
            path,
            ns=(index * 1_000_000_000, metadata.st_mtime_ns),
            follow_symlinks=False,
        )

    assert _archive_digest(first, tmp_path / "first.tar") == _archive_digest(
        second,
        tmp_path / "second.tar",
    )


def test_transfer_preserves_content_modes_and_symlinks(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    destination = tmp_path / "destination"

    count = transfer_tree_deterministically(
        source,
        destination,
        source_date_epoch=7,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    module = destination / "lib" / "python3.14" / "site-packages" / "example" / "module.py"
    link = module.with_name("module-link.py")
    assert count == len([source, *source.rglob("*")])
    assert module.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert module.stat().st_mode & 0o777 == 0o640
    assert link.is_symlink()
    assert link.readlink() == Path("module.py")
    assert all(
        path.lstat().st_mtime_ns == 7_000_000_000 for path in (destination, *destination.rglob("*"))
    )


def test_transfer_rejects_existing_destination(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(ValueError, match="must not already exist"):
        transfer_tree_deterministically(
            source,
            destination,
            source_date_epoch=0,
            uid=os.getuid(),
            gid=os.getgid(),
        )


def test_tar_rejects_unsupported_file_type(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    fifo = source / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="unsupported file type"):
        write_deterministic_tar(
            source,
            tmp_path / "tree.tar",
            source_date_epoch=0,
            uid=os.getuid(),
            gid=os.getgid(),
        )
