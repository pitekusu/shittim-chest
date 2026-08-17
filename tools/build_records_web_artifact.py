#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build a deterministic, path-safe Records Web ZIP artifact."""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path, PurePosixPath

_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build_artifact(source: Path, output: Path) -> None:
    """Archive a built SPA without timestamps, symlinks, or ambiguous paths."""

    if not source.is_dir() or not (source / "index.html").is_file():
        raise ValueError("Records Web source must contain index.html")
    files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
    if not files:
        raise ValueError("Records Web source is empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for path in files:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError("Records Web artifact accepts regular files only")
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Records Web artifact path is unsafe")
            info = zipfile.ZipInfo(relative.as_posix(), _FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_artifact(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
