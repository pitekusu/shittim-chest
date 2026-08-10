"""Tests for deterministic filesystem-tree transfer."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from tools.transfer_tree_deterministically import transfer_tree_deterministically


def _source_tree(root: Path) -> Path:
    source = root / "source"
    source.mkdir(parents=True)
    source.chmod(0o750)
    package = source / "lib" / "python3.14" / "site-packages" / "example"
    package.mkdir(parents=True)
    package.chmod(0o775)
    module = package / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    module.chmod(0o640)
    (package / "module-link.py").symlink_to("module.py")
    return source


def _set_distinct_atimes(source: Path, *, start: int) -> None:
    for index, path in enumerate((source, *source.rglob("*")), start=start):
        metadata = path.lstat()
        os.utime(
            path,
            ns=(index * 1_000_000_000, metadata.st_mtime_ns),
            follow_symlinks=False,
        )


def _snapshot(root: Path) -> list[tuple[str, int, int, str]]:
    result: list[tuple[str, int, int, str]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode):
            payload = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(metadata.st_mode):
            payload = path.readlink().as_posix()
        else:
            payload = "directory"
        result.append(
            (
                path.relative_to(root).as_posix(),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                payload,
            )
        )
    return result


def test_transfer_is_identical_when_source_atimes_differ(tmp_path: Path) -> None:
    first = _source_tree(tmp_path / "first")
    second = _source_tree(tmp_path / "second")
    _set_distinct_atimes(first, start=1)
    _set_distinct_atimes(second, start=101)
    first_destination = tmp_path / "first-destination"
    second_destination = tmp_path / "second-destination"

    for source, destination in (
        (first, first_destination),
        (second, second_destination),
    ):
        transfer_tree_deterministically(
            source,
            destination,
            source_date_epoch=7,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert _snapshot(first_destination) == _snapshot(second_destination)


def test_transfer_is_identical_under_different_process_umasks(tmp_path: Path) -> None:
    source = _source_tree(tmp_path / "source-root")
    destinations = (tmp_path / "restrictive", tmp_path / "permissive")
    original_umask = os.umask(0o077)
    try:
        transfer_tree_deterministically(
            source,
            destinations[0],
            source_date_epoch=7,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        os.umask(0o002)
        transfer_tree_deterministically(
            source,
            destinations[1],
            source_date_epoch=7,
            uid=os.getuid(),
            gid=os.getgid(),
        )
    finally:
        os.umask(original_umask)

    assert _snapshot(destinations[0]) == _snapshot(destinations[1])
    assert stat.S_IMODE(destinations[0].stat().st_mode) == 0o750
    assert (
        stat.S_IMODE(
            (destinations[0] / "lib" / "python3.14" / "site-packages" / "example").stat().st_mode
        )
        == 0o775
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


def test_transfer_rejects_unsupported_file_type(tmp_path: Path) -> None:
    source = _source_tree(tmp_path)
    os.mkfifo(source / "fifo")

    with pytest.raises(ValueError, match="unsupported file type"):
        transfer_tree_deterministically(
            source,
            tmp_path / "destination",
            source_date_epoch=0,
            uid=os.getuid(),
            gid=os.getgid(),
        )
