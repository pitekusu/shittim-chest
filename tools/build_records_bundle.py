"""Build a deterministic Records Lambda ZIP from a prepared directory."""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path

_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


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
            info = zipfile.ZipInfo(relative, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_bundle(args.source, args.output)


if __name__ == "__main__":
    main()
