#!/usr/bin/env python3
"""Install the APT-authenticated Debian fix while DHI's runtime lags its builder."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

VERSION = "2.9.0-1~deb13u1"
TOOLS = Path("/run/package-tools")
PACKAGE = Path("/run/libexpat1.deb")


def install() -> None:
    # APT authenticates the pinned package in the builder. Bind-mounted standard
    # dpkg tools perform the install; none become part of the runtime image.
    environment = dict(os.environ)
    environment["PATH"] = os.pathsep.join(str(TOOLS / name) for name in ("bin", "sbin"))
    environment["LD_LIBRARY_PATH"] = str(TOOLS / "lib" / sysconfig.get_config_var("MULTIARCH"))
    metadata = subprocess.check_output(  # noqa: S603 - fixed, build-only package tools.
        [str(TOOLS / "bin/dpkg-deb"), "--field", str(PACKAGE), "Package", "Version"],
        env=environment,
        text=True,
    )
    if metadata.splitlines() != ["Package: libexpat1", f"Version: {VERSION}"]:
        raise ValueError("unexpected runtime Expat package")
    subprocess.run(  # noqa: S603 - APT-verified, exact-version Debian package.
        [
            str(TOOLS / "bin/dpkg"),
            "--no-triggers",
            "--log=/dev/null",
            "--install",
            str(PACKAGE),
        ],
        env=environment,
        check=True,
    )
    status = subprocess.check_output(  # noqa: S603 - query the installed package, not model metadata.
        [str(TOOLS / "bin/dpkg-query"), "--status", "libexpat1"],
        env=environment,
        text=True,
    )
    if f"Version: {VERSION}\n" not in status or "Status: install ok installed\n" not in status:
        raise ValueError("runtime Expat package was not installed")
    # DHI also publishes per-package inventory. Keep it consistent with dpkg's
    # authoritative database so SBOM readers do not retain a stale second record.
    Path("/var/lib/dpkg/status.d/libexpat1").write_text(status, encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--version"]:
        print(VERSION)
    elif len(sys.argv) == 1:
        install()
    else:
        raise SystemExit("unexpected Expat installer arguments")
