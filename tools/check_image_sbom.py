#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate the production image SPDX inventory emitted by Syft."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final, cast

MAXIMUM_SBOM_BYTES: Final = 20 * 1024 * 1024
PYPI_PURL_PREFIX: Final = "pkg:pypi/"
DEBIAN_PURL_PREFIX: Final = "pkg:deb/debian/"
PROJECT_VERSION: Final = "0.1.0"
FORBIDDEN_DEVELOPMENT_PACKAGES: Final = frozenset(
    {"hypothesis", "import-linter", "mypy", "pip-audit", "pytest", "ruff", "ty"}
)


class ImageSbomError(RuntimeError):
    """Raised when the image SBOM is malformed or incomplete."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ImageSbomError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ImageSbomError(f"{label} must be an array")
    return cast(list[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImageSbomError(f"{label} must be non-empty text")
    return value


def _parse(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ImageSbomError("image SBOM must be a regular non-symlink file")
    if path.stat().st_size > MAXIMUM_SBOM_BYTES:
        raise ImageSbomError("image SBOM exceeds the size limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ImageSbomError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value: object = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImageSbomError("image SBOM is not valid UTF-8 JSON") from error
    return _object(value, "SPDX document")


def validate_spdx_document(document: dict[str, object]) -> tuple[int, int]:
    """Require SPDX 2.3 with both Debian OS and production Python packages."""

    if document.get("spdxVersion") != "SPDX-2.3":
        raise ImageSbomError("image SBOM must use SPDX-2.3")
    creation = _object(document.get("creationInfo"), "creationInfo")
    creators = {
        _text(value, "creationInfo.creators[]")
        for value in _array(creation.get("creators"), "creationInfo.creators")
    }
    if not any("syft" in creator.lower() for creator in creators):
        raise ImageSbomError("image SBOM creator must identify Syft")

    package_ids: set[str] = set()
    purls: set[str] = set()
    for index, raw_package in enumerate(_array(document.get("packages"), "packages")):
        package = _object(raw_package, f"packages[{index}]")
        package_id = _text(package.get("SPDXID"), f"packages[{index}].SPDXID")
        if package_id in package_ids:
            raise ImageSbomError(f"duplicate SPDXID: {package_id}")
        package_ids.add(package_id)
        for raw_reference in _array(package.get("externalRefs", []), "externalRefs"):
            reference = _object(raw_reference, "externalRefs[]")
            if reference.get("referenceType") == "purl":
                purls.add(_text(reference.get("referenceLocator"), "purl"))

    python_purls = {purl for purl in purls if purl.startswith(PYPI_PURL_PREFIX)}
    debian_purls = {purl for purl in purls if purl.startswith(DEBIAN_PURL_PREFIX)}
    if not debian_purls:
        raise ImageSbomError("image SBOM contains no Debian OS packages")
    expected_project_purl = f"pkg:pypi/shittim-chest@{PROJECT_VERSION}"
    if not any(purl.startswith(expected_project_purl) for purl in python_purls):
        raise ImageSbomError("image SBOM is missing shittim-chest 0.1.0")
    normalized_names = {
        re.sub(r"[-_.]+", "-", purl.removeprefix(PYPI_PURL_PREFIX).split("@", 1)[0]).lower()
        for purl in python_purls
    }
    forbidden = normalized_names & FORBIDDEN_DEVELOPMENT_PACKAGES
    if forbidden:
        raise ImageSbomError("image contains development packages: " + ", ".join(sorted(forbidden)))
    return len(debian_purls), len(python_purls)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spdx", type=Path)
    args = parser.parse_args()
    try:
        os_count, python_count = validate_spdx_document(_parse(args.spdx))
    except ImageSbomError as error:
        print(f"image SBOM check failed: {error}", file=sys.stderr)
        return 1
    print(f"image SPDX SBOM is valid: {os_count} Debian and {python_count} Python packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
