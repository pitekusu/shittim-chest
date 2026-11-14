from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from PIL import Image

from shittim_chest.adapters.assets.student_gacha_schools import school_branding
from shittim_chest.application.student_gacha_models import GachaGenerationFailure, SchoolBranding

_ROOT = "blue_archive_school_logos/"
_SCHOOL = "トリニティ総合学園"
_LOGO_PATH = "logos/trinity.png"


def _logo_png() -> bytes:
    with io.BytesIO() as output:
        Image.new("RGBA", (3, 2), (0, 0, 0, 255)).save(output, format="PNG")
        return output.getvalue()


def _catalog(logo: bytes, **entry_overrides: object) -> bytes:
    rows = [
        {
            "id": "trinity",
            "name": _SCHOOL,
            "kind": "school",
            "logo_path": _LOGO_PATH,
            "width": 3,
            "height": 2,
            "format": "PNG",
            "sha256": hashlib.sha256(logo).hexdigest(),
            **entry_overrides,
        },
        {"id": "sakugawa", "name": "柵川中学", "kind": "school", "logo_path": None},
    ]
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_ROOT + "schools.json", json.dumps(rows))
            archive.writestr(_ROOT + _LOGO_PATH, logo)
        return output.getvalue()


@pytest.mark.parametrize("affiliation", [_SCHOOL, "トリニティ"])
def test_returns_original_logo_and_canonical_name(affiliation: str) -> None:
    logo = _logo_png()
    assert school_branding(_catalog(logo), affiliation) == SchoolBranding(_SCHOOL, logo)


@pytest.mark.parametrize("affiliation", ["柵川中学", "その他生徒", "シッテムの箱", "自民党"])
def test_uses_affiliation_text_for_school_without_logo(affiliation: str) -> None:
    assert school_branding(_catalog(_logo_png()), affiliation) == SchoolBranding(affiliation)


@pytest.mark.parametrize(
    "entry_overrides",
    [
        {"logo_path": "../../trinity.png"},
        {"logo_path": "logos/gehenna.png"},
        {"width": 10_000_000},
        {"width": 4},
        {"sha256": "0" * 64},
    ],
)
def test_rejects_invalid_logo_reference(entry_overrides: dict[str, object]) -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_school_catalog_invalid"):
        school_branding(_catalog(_logo_png(), **entry_overrides), _SCHOOL)


@pytest.mark.parametrize("catalog", [b"invalid zip", _catalog(b"invalid image")])
def test_rejects_invalid_zip_or_png(catalog: bytes) -> None:
    with pytest.raises(GachaGenerationFailure, match="gacha_school_catalog_invalid"):
        school_branding(catalog, _SCHOOL)
