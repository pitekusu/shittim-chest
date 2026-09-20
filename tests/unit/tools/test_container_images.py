"""The shared image source must preserve immutable upstream references."""

from pathlib import Path

import pytest
from tools.container_images import load_image


def test_reads_updated_image_without_requiring_a_python_pin_change(tmp_path: Path) -> None:
    path = tmp_path / "Dockerfile"
    image = f"moby/buildkit:v0.34.0@sha256:{'a' * 64}"
    path.write_text(f"FROM {image} AS buildkit\n")
    assert load_image("buildkit", path) == image


@pytest.mark.parametrize(
    "content",
    [
        "FROM moby/buildkit:latest AS buildkit\n",
        f"FROM other/buildkit:v0.34.0@sha256:{'a' * 64} AS buildkit\n",
        f"FROM moby/buildkit:v0.34.0@sha256:{'a' * 63} AS buildkit\n",
        f"FROM moby/buildkit:v0.34.0@sha256:{'a' * 64} AS buildkit\n" * 2,
        "",
    ],
)
def test_rejects_unpinned_substituted_or_ambiguous_images(tmp_path: Path, content: str) -> None:
    path = tmp_path / "Dockerfile"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_image("buildkit", path)
