"""Build a deterministic Records Lambda ZIP from a prepared directory."""

from __future__ import annotations

import argparse
import csv
import io
import stat
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

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


def build_bundle(
    source: Path,
    output: Path,
    *,
    assets: Mapping[str, Path] | None = None,
) -> None:
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
    entries: dict[str, bytes] = {}
    for path in files:
        if path.is_symlink():
            raise ValueError("Records bundle must not contain symbolic links")
        relative = path.relative_to(source).as_posix()
        payload = _archive_payload(path, source=source)
        if payload is not None:
            entries[relative] = payload
    for relative, configured_path in (assets or {}).items():
        destination = PurePosixPath(relative)
        if (
            destination.is_absolute()
            or not destination.parts
            or any(part in {"", ".", ".."} for part in destination.parts)
        ):
            raise ValueError("Records bundle asset destination is invalid")
        if configured_path.is_symlink():
            raise ValueError("Records bundle asset must be a regular file")
        path = configured_path.resolve(strict=True)
        if not path.is_file():
            raise ValueError("Records bundle asset must be a regular file")
        destination_text = destination.as_posix()
        if destination_text in entries:
            raise ValueError("Records bundle asset destination is duplicated")
        entries[destination_text] = path.read_bytes()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, payload in sorted(entries.items()):
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED)


def _assets(values: Sequence[str]) -> dict[str, Path]:
    assets: dict[str, Path] = {}
    for value in values:
        source, separator, destination = value.partition("=")
        if not separator or not source or not destination or destination in assets:
            raise ValueError("Records bundle asset mapping is invalid")
        assets[destination] = Path(source)
    return assets


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        metavar="SOURCE=DESTINATION",
        help="add one deterministic runtime asset at an exact ZIP path",
    )
    args = parser.parse_args()
    build_bundle(args.source, args.output, assets=_assets(args.asset))


if __name__ == "__main__":
    main()
