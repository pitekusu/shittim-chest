#!/usr/bin/env python3
"""Transfer a filesystem tree without carrying volatile source metadata."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
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


def _destination_path(source: Path, destination: Path, path: Path) -> Path:
    relative = path.relative_to(source)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"source contains an unsafe relative path: {path}")
    return destination.joinpath(*relative.parts)


def _canonical_tree_digest(root: Path) -> str:
    """Hash the filesystem attributes that can affect an OCI layer diff ID."""

    digest = hashlib.sha256()
    for path in _tree_paths(root):
        metadata = path.lstat()
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(metadata.st_mode):
            entry_type = b"file"
        elif stat.S_ISDIR(metadata.st_mode):
            entry_type = b"directory"
        else:
            entry_type = b"symlink"
        fields = [
            relative.encode("utf-8"),
            entry_type,
            f"{stat.S_IMODE(metadata.st_mode):04o}".encode("ascii"),
            str(metadata.st_uid).encode("ascii"),
            str(metadata.st_gid).encode("ascii"),
            str(metadata.st_mtime_ns).encode("ascii"),
        ]
        if stat.S_ISREG(metadata.st_mode):
            fields.append(str(metadata.st_size).encode("ascii"))
        for field in fields:
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
        if stat.S_ISREG(metadata.st_mode):
            with path.open("rb") as contents:
                for chunk in iter(lambda: contents.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(path.readlink())
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
    return digest.hexdigest()


def _canonicalize_destination_times(root: Path, source_date_epoch: int) -> None:
    expected_time_ns = source_date_epoch * 1_000_000_000
    destination_paths = [root, *root.rglob("*")]
    for path in sorted(destination_paths, key=lambda item: len(item.parts), reverse=True):
        os.utime(
            path,
            ns=(expected_time_ns, expected_time_ns),
            follow_symlinks=False,
        )


def transfer_tree_deterministically(
    source: Path,
    destination: Path,
    *,
    source_date_epoch: int,
    uid: int,
    gid: int,
) -> int:
    """Copy content while setting destination ownership and times explicitly."""

    if source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    if uid < 0 or gid < 0:
        raise ValueError("uid and gid must be non-negative integers")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"destination must not already exist: {destination}")
    if not destination.parent.is_dir():
        raise ValueError(f"destination parent must exist: {destination.parent}")

    paths = _tree_paths(source)
    source_mode = stat.S_IMODE(source.lstat().st_mode)
    destination.mkdir(mode=source_mode)
    destination.chmod(source_mode, follow_symlinks=False)
    for path in paths[1:]:
        target = _destination_path(source, destination, path)
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(mode=mode)
            # mkdir applies the process umask. Restore the source mode explicitly
            # so BuildKit worker configuration cannot alter the resulting layer.
            target.chmod(mode, follow_symlinks=False)
        elif stat.S_ISLNK(metadata.st_mode):
            target.symlink_to(path.readlink())
        else:
            with path.open("rb") as source_file, target.open("xb") as target_file:
                shutil.copyfileobj(source_file, target_file)
            target.chmod(mode, follow_symlinks=False)

    destination_paths = [destination, *destination.rglob("*")]
    for path in sorted(destination_paths, key=lambda item: len(item.parts), reverse=True):
        os.chown(path, uid, gid, follow_symlinks=False)
    _canonicalize_destination_times(destination, source_date_epoch)
    return len(paths)


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
        tree_digest = _canonical_tree_digest(args.destination)
        # Reading regular files for the diagnostic digest can update atime.
        _canonicalize_destination_times(args.destination, args.source_date_epoch)
    except (OSError, ValueError) as error:
        print(f"deterministic tree transfer failed: {error}", file=sys.stderr)
        return 1
    print(f"filesystem entries transferred: {count}")
    print(f"filesystem tree sha256: {tree_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
