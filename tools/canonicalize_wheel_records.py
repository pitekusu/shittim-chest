#!/usr/bin/env python3
"""Canonicalize installed wheel RECORD files for reproducible container layers."""

from __future__ import annotations

import argparse
import csv
import io
import os
import stat
import sys
from pathlib import Path


def _record_paths(venv: Path) -> list[Path]:
    return sorted(
        venv.glob("lib/python*/site-packages/*.dist-info/RECORD"),
        key=lambda path: path.as_posix(),
    )


def _canonical_record(record: Path) -> tuple[str, Path | None]:
    with record.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError(f"wheel RECORD is empty: {record}")
    if any(len(row) != 3 or not row[0] for row in rows):
        raise ValueError(f"wheel RECORD must contain three non-empty-path columns: {record}")
    paths = [row[0] for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError(f"wheel RECORD contains duplicate paths: {record}")

    uv_cache_row = f"{record.parent.name}/uv_cache.json"
    unexpected_uv_cache_rows = [
        path for path in paths if path.endswith("/uv_cache.json") and path != uv_cache_row
    ]
    if unexpected_uv_cache_rows:
        raise ValueError(f"wheel RECORD contains an unexpected uv cache path: {record}")
    rows = [row for row in rows if row[0] != uv_cache_row]
    uv_cache = record.parent / "uv_cache.json"
    if uv_cache.is_symlink() or (uv_cache.exists() and not uv_cache.is_file()):
        raise ValueError(f"wheel uv cache metadata must be a regular file: {uv_cache}")

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(sorted(rows, key=lambda row: tuple(row)))
    return output.getvalue(), uv_cache if uv_cache.exists() else None


def _venv_paths(venv: Path) -> list[Path]:
    metadata = venv.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or venv.is_symlink():
        raise ValueError(f"virtual environment root must be a directory: {venv}")
    paths = [venv, *venv.rglob("*")]
    for path in paths:
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            raise ValueError(f"virtual environment contains an unsupported file type: {path}")
    return sorted(paths, key=lambda path: path.as_posix())


def _canonicalize_tree_mtimes(
    venv: Path,
    *,
    source_date_epoch: int,
    check: bool,
) -> None:
    if source_date_epoch < 0:
        raise ValueError("SOURCE_DATE_EPOCH must be a non-negative integer")
    expected_mtime_ns = source_date_epoch * 1_000_000_000
    changed: list[Path] = []
    for path in _venv_paths(venv):
        metadata = path.lstat()
        if metadata.st_mtime_ns == expected_mtime_ns:
            continue
        changed.append(path)
        if check:
            continue
        os.utime(
            path,
            ns=(metadata.st_atime_ns, expected_mtime_ns),
            follow_symlinks=False,
        )
        if path.lstat().st_mtime_ns != expected_mtime_ns:
            raise ValueError(f"virtual environment mtime normalization failed: {path}")
    if check and changed:
        raise ValueError(f"non-canonical virtual environment timestamps: {len(changed)}")


def canonicalize_wheel_records(
    venv: Path,
    *,
    check: bool = False,
    source_date_epoch: int = 0,
) -> int:
    """Canonicalize RECORD files and the complete virtual-environment mtime surface."""

    records = _record_paths(venv)
    if not records:
        raise ValueError(f"virtual environment contains no wheel RECORD files: {venv}")
    changed: list[Path] = []
    for record in records:
        canonical, uv_cache = _canonical_record(record)
        with record.open(encoding="utf-8", newline="") as handle:
            current = handle.read()
        if current == canonical and uv_cache is None:
            continue
        changed.append(record)
        if not check:
            if uv_cache is not None:
                uv_cache.unlink()
            with record.open("w", encoding="utf-8", newline="") as handle:
                handle.write(canonical)
    if check and changed:
        raise ValueError(f"non-canonical wheel RECORD files: {len(changed)}")
    _canonicalize_tree_mtimes(
        venv,
        source_date_epoch=source_date_epoch,
        check=check,
    )
    return len(records)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venv", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        default=os.environ.get("SOURCE_DATE_EPOCH", "0"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        count = canonicalize_wheel_records(
            args.venv,
            check=args.check,
            source_date_epoch=args.source_date_epoch,
        )
    except (OSError, ValueError) as error:
        print(f"wheel RECORD canonicalization failed: {error}", file=sys.stderr)
        return 1
    action = "validated" if args.check else "canonicalized"
    print(f"wheel RECORD files {action}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
