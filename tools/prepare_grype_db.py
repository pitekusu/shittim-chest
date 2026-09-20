#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Prepare a versioned database cache key or explicitly refresh Grype's database."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def cache_keys(version: str, runner_os: str, date: str) -> tuple[str, str]:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("invalid Grype version")
    if runner_os not in {"Linux", "Windows", "macOS"}:
        raise ValueError("invalid runner OS")
    prefix = f"grype-db-v2-{runner_os}-{version}-"
    return prefix + date, prefix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--github-output", type=Path)
    mode.add_argument("--update", action="store_true")
    args = parser.parse_args()
    if args.update:
        # A hit may predate today's DB publication. Never skip freshness validation.
        subprocess.run([str(args.binary), "db", "update"], check=True)  # noqa: S603
        return
    version = json.loads(
        subprocess.check_output(  # noqa: S603
            [str(args.binary), "version", "-o", "json"], text=True
        )
    )["version"]
    key, prefix = cache_keys(
        version, os.environ["RUNNER_OS"], datetime.now(UTC).strftime("%Y-%m-%d")
    )
    with args.github_output.open("a") as output:
        output.write(f"key={key}\nprefix={prefix}\n")


if __name__ == "__main__":
    main()
