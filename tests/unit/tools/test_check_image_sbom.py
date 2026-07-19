"""Tests for production image SPDX validation."""

from __future__ import annotations

from copy import deepcopy
from typing import cast

import pytest
from tools.check_image_sbom import ImageSbomError, validate_spdx_document


def _document() -> dict[str, object]:
    return {
        "spdxVersion": "SPDX-2.3",
        "creationInfo": {"creators": ["Organization: Anchore, Inc", "Tool: syft-1.48.0"]},
        "packages": [
            _package("SPDXRef-os", "pkg:deb/debian/base-files@14?arch=arm64"),
            _package("SPDXRef-app", _versioned("pkg:pypi/shittim-chest", "0.1.0")),
            _package("SPDXRef-sdk", _versioned("pkg:pypi/openai", "2.45.0")),
        ],
    }


def _package(identifier: str, purl: str) -> dict[str, object]:
    return {
        "SPDXID": identifier,
        "name": purl,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": purl,
            }
        ],
    }


def _versioned(name: str, version: str) -> str:
    return f"{name}@{version}"


def test_valid_image_inventory_requires_os_and_runtime_packages() -> None:
    assert validate_spdx_document(_document()) == (1, 2)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("version", "SPDX-2.3"),
        ("creator", "identify Syft"),
        ("os", "no Debian"),
        ("app", "missing shittim-chest"),
        ("development", "development packages"),
        ("duplicate", "duplicate SPDXID"),
    ],
)
def test_invalid_image_inventory_is_rejected(mutation: str, message: str) -> None:
    document = deepcopy(_document())
    packages = document["packages"]
    assert isinstance(packages, list)
    packages = cast(list[object], packages)
    if mutation == "version":
        document["spdxVersion"] = "SPDX-2.2"
    elif mutation == "creator":
        document["creationInfo"] = {"creators": ["Tool: unknown"]}
    elif mutation == "os":
        packages.pop(0)
    elif mutation == "app":
        packages.pop(1)
    elif mutation == "development":
        packages.append(_package("SPDXRef-pytest", _versioned("pkg:pypi/pytest", "9.1.1")))
    else:
        packages.append(_package("SPDXRef-os", _versioned("pkg:pypi/httpx", "0.28.1")))

    with pytest.raises(ImageSbomError, match=message):
        validate_spdx_document(document)
