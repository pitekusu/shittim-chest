#!/usr/bin/env python3
"""Canonicalize installed wheel RECORD files for reproducible container layers."""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path


def _record_paths(venv: Path) -> list[Path]:
    return sorted(
        venv.glob("lib/python*/site-packages/*.dist-info/RECORD"),
        key=lambda path: path.as_posix(),
    )


def _canonical_record(record: Path) -> str:
    with record.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        raise ValueError(f"wheel RECORD is empty: {record}")
    if any(len(row) != 3 or not row[0] for row in rows):
        raise ValueError(f"wheel RECORD must contain three non-empty-path columns: {record}")
    paths = [row[0] for row in rows]
    if len(paths) != len(set(paths)):
        raise ValueError(f"wheel RECORD contains duplicate paths: {record}")

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(sorted(rows, key=lambda row: tuple(row)))
    return output.getvalue()


def canonicalize_wheel_records(venv: Path, *, check: bool = False) -> int:
    """Write or check canonical CSV ordering for every installed wheel RECORD."""

    records = _record_paths(venv)
    if not records:
        raise ValueError(f"virtual environment contains no wheel RECORD files: {venv}")
    changed: list[Path] = []
    for record in records:
        canonical = _canonical_record(record)
        with record.open(encoding="utf-8", newline="") as handle:
            current = handle.read()
        if current == canonical:
            continue
        changed.append(record)
        if not check:
            with record.open("w", encoding="utf-8", newline="") as handle:
                handle.write(canonical)
    if check and changed:
        raise ValueError(f"non-canonical wheel RECORD files: {len(changed)}")
    return len(records)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("venv", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        count = canonicalize_wheel_records(args.venv, check=args.check)
    except (OSError, ValueError) as error:
        print(f"wheel RECORD canonicalization failed: {error}", file=sys.stderr)
        return 1
    action = "validated" if args.check else "canonicalized"
    print(f"wheel RECORD files {action}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
