#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Read immutable upstream image references from the Dependabot-managed Dockerfile."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Final

IMAGE_FILE: Final = Path(__file__).with_name("containers") / "Dockerfile"
REPOSITORIES: Final = {"dynamodb-local": "amazon/dynamodb-local", "buildkit": "moby/buildkit"}


def load_image(name: str, path: Path = IMAGE_FILE) -> str:
    """Reject missing, duplicate, unpinned, or substituted service images."""

    if name not in REPOSITORIES:
        raise ValueError(f"unknown container image: {name}")
    references = re.findall(rf"(?m)^FROM (\S+) AS {re.escape(name)}$", path.read_text())
    if len(references) != 1:
        raise ValueError(f"expected one Dockerfile stage for {name}")
    image = references[0]
    repository = re.escape(REPOSITORIES[name])
    if re.fullmatch(rf"{repository}(?::[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{{64}}", image) is None:
        raise ValueError(f"{name} must use its upstream repository and an immutable digest")
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=REPOSITORIES)
    arguments = parser.parse_args()
    print(load_image(arguments.name))


if __name__ == "__main__":
    main()
