#!/usr/bin/env python3
"""Transfer a filesystem tree without carrying volatile source metadata."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tarfile
import tempfile
from pathlib import Path


def _tree_paths(source: Path) -> list[Path]:
    metadata = source.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or source.is_symlink():
        raise ValueError(f"source root must be a directory: {source}")
    paths = [source, *source.rglob("*")]
    for path in paths:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise ValueError(f"source contains an unsupported file type: {path}")
    return sorted(paths, key=lambda path: path.relative_to(source).as_posix())


def write_deterministic_tar(
    source: Path,
    archive: Path,
    *,
    source_date_epoch: int,
    uid: int,
    gid: int,
) -> int:
    """Write a GNU tar stream with stable order, timestamps, and ownership."""

    if source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    if uid < 0 or gid < 0:
        raise ValueError("uid and gid must be non-negative integers")
    paths = _tree_paths(source)
    with tarfile.open(archive, mode="w", format=tarfile.GNU_FORMAT, dereference=False) as output:
        for path in paths:
            relative = path.relative_to(source)
            archive_name = "." if not relative.parts else f"./{relative.as_posix()}"
            member = output.gettarinfo(str(path), arcname=archive_name)
            member.uid = uid
            member.gid = gid
            member.uname = ""
            member.gname = ""
            member.mtime = source_date_epoch
            member.pax_headers = {}
            if member.isreg():
                with path.open("rb") as contents:
                    output.addfile(member, contents)
            else:
                output.addfile(member)
    return len(paths)


def transfer_tree_deterministically(
    source: Path,
    destination: Path,
    *,
    source_date_epoch: int,
    uid: int,
    gid: int,
) -> int:
    """Transfer a tree through a deterministic tar archive."""

    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination must not already exist: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"destination parent must exist: {destination.parent}")

    archive_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="deterministic-tree-", suffix=".tar", delete=False
        ) as archive:
            archive_path = Path(archive.name)
        count = write_deterministic_tar(
            source,
            archive_path,
            source_date_epoch=source_date_epoch,
            uid=uid,
            gid=gid,
        )
        destination.mkdir()
        with tarfile.open(archive_path, mode="r:") as input_archive:
            # The private archive is synthesized above from a validated tree.
            input_archive.extractall(  # noqa: S202
                destination,
                numeric_owner=True,
                filter="fully_trusted",
            )
    finally:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)

    expected_time_ns = source_date_epoch * 1_000_000_000
    destination_paths = [destination, *destination.rglob("*")]
    for path in sorted(destination_paths, key=lambda item: len(item.parts), reverse=True):
        os.chown(path, uid, gid, follow_symlinks=False)
        os.utime(
            path,
            ns=(expected_time_ns, expected_time_ns),
            follow_symlinks=False,
        )
    return count


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=os.environ.get("SOURCE_DATE_EPOCH", "0"),
    )
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        count = transfer_tree_deterministically(
            args.source,
            args.destination,
            source_date_epoch=args.source_date_epoch,
            uid=args.uid,
            gid=args.gid,
        )
    except (OSError, tarfile.TarError, ValueError) as error:
        print(f"deterministic tree transfer failed: {error}", file=sys.stderr)
        return 1
    print(f"filesystem entries transferred: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
