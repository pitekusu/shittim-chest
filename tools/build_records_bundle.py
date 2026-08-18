"""Build a deterministic Records Lambda ZIP from a prepared directory."""

from __future__ import annotations

import argparse
import csv
import io
import stat
import zipfile
from pathlib import Path

_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def _archive_payload(path: Path, *, source: Path) -> bytes | None:
    """Return canonical runtime bytes, excluding uv's install-cache metadata."""

    relative = path.relative_to(source).as_posix()
    parent = path.parent.name
    if parent.endswith(".dist-info") and path.name == "uv_cache.json":
        return None
    if not (parent.endswith(".dist-info") and path.name == "RECORD"):
        return path.read_bytes()

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or any(len(row) != 3 or not row[0] for row in rows):
        raise ValueError(f"wheel RECORD is invalid: {relative}")
    uv_cache_row = f"{parent}/uv_cache.json"
    unexpected_uv_cache_rows = [
        row[0] for row in rows if row[0].endswith("/uv_cache.json") and row[0] != uv_cache_row
    ]
    if unexpected_uv_cache_rows:
        raise ValueError(f"wheel RECORD contains an unexpected uv cache path: {relative}")
    canonical_rows = [row for row in rows if row[0] != uv_cache_row]
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(canonical_rows)
    return output.getvalue().encode()


def build_bundle(source: Path, output: Path) -> None:
    """Write sorted regular files with stable timestamps and permissions."""

    source = source.resolve(strict=True)
    output = output.resolve()
    if not source.is_dir():
        raise ValueError("Records bundle source must be a directory")
    if output == source or source in output.parents:
        raise ValueError("Records bundle output must be outside the source directory")
    files = tuple(
        path
        for path in sorted(source.rglob("*"), key=lambda candidate: candidate.as_posix())
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not files:
        raise ValueError("Records bundle source is empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            if path.is_symlink():
                raise ValueError("Records bundle must not contain symbolic links")
            relative = path.relative_to(source).as_posix()
            payload = _archive_payload(path, source=source)
            if payload is None:
                continue
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_bundle(args.source, args.output)


if __name__ == "__main__":
    main()
