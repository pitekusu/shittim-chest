"""Read the private school logo catalog without extracting archive members."""

from __future__ import annotations

import hashlib
import io
import json
import re
import warnings
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Final

from PIL import Image

from shittim_chest.application.student_gacha_models import GachaGenerationFailure, SchoolBranding

SCHOOL_CATALOG_MAX_BYTES: Final = 2 * 1024 * 1024
_MAX_INDEX_BYTES = 32 * 1024
_MAX_LOGO_BYTES = 512 * 1024
_MAX_LOGO_PIXELS = 4_000_000
_MAX_SCHOOLS = 64
_ROOT = "blue_archive_school_logos/"
_SCHOOL_ID = re.compile(r"[a-z0-9-]{1,48}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The roster includes both Wiki short labels and the full school names.
_SCHOOL_ALIASES: Final = {
    "SRT": "SRT特殊学園",
    "アビドス": "アビドス高等学校",
    "アリウス": "アリウス分校",
    "オデュッセイア": "オデュッセイア海洋高等学校",
    "ゲヘナ": "ゲヘナ学園",
    "トリニティ": "トリニティ総合学園",
    "ハイランダー": "ハイランダー鉄道学園",
    "ミレニアム": "ミレニアムサイエンススクール",
    "レッドウィンター": "レッドウィンター連邦学園",
    "ワイルドハント": "ワイルドハント芸術学院",
    "ヴァルキューレ": "ヴァルキューレ警察学校",
    "山海経": "山海経高級中学校",
    "百鬼夜行": "百鬼夜行連合学院",
    "所属不明": "所属不明・所属なし",
    "所属なし": "所属不明・所属なし",
}


@dataclass(frozen=True, slots=True)
class _Logo:
    path: str
    size: tuple[int, int]
    sha256: str = field(repr=False)


def school_branding(catalog: bytes, school: str) -> SchoolBranding:
    """Resolve an exact affiliation or roster alias to its display name and supplied logo."""
    try:
        if not 1 <= len(catalog) <= SCHOOL_CATALOG_MAX_BYTES:
            raise ValueError
        with zipfile.ZipFile(io.BytesIO(catalog)) as archive:
            members = archive.namelist()
            if len(members) > _MAX_SCHOOLS + 4 or len(members) != len(set(members)):
                raise ValueError
            index = _read_member(archive, _ROOT + "schools.json", _MAX_INDEX_BYTES)
            schools = _parse_index(index)
            canonical_name = _SCHOOL_ALIASES.get(school, school)
            if canonical_name not in schools:
                return SchoolBranding(school)
            logo = schools[canonical_name]
            return SchoolBranding(
                canonical_name, _read_logo(archive, logo) if logo is not None else None
            )
    except (
        EOFError,
        KeyError,
        OSError,
        RecursionError,
        RuntimeError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        zipfile.BadZipFile,
        zlib.error,
    ):
        raise GachaGenerationFailure("gacha_school_catalog_invalid") from None


def _parse_index(content: bytes) -> dict[str, _Logo | None]:
    rows: object = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(rows, list) or not 1 <= len(rows) <= _MAX_SCHOOLS:
        raise ValueError
    schools: dict[str, _Logo | None] = {}
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        identifier = row["id"]
        name = row["name"]
        if (
            not isinstance(identifier, str)
            or _SCHOOL_ID.fullmatch(identifier) is None
            or identifier in identifiers
            or not isinstance(name, str)
            or not 1 <= len(name) <= 40
            or name != name.strip()
            or not name.isprintable()
            or name in schools
            or row["kind"] not in ("school", "organization", "category")
        ):
            raise ValueError
        identifiers.add(identifier)
        schools[name] = _logo_entry(row, identifier)
    return schools


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError
    return row


def _logo_entry(row: dict[str, object], identifier: str) -> _Logo | None:
    path = row["logo_path"]
    if path is None:
        return None
    width, height, sha256 = row["width"], row["height"], row["sha256"]
    if (
        not isinstance(path, str)
        or path != f"logos/{identifier}.png"
        or row["format"] != "PNG"
        or not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or min(width, height) < 1
        or width * height > _MAX_LOGO_PIXELS
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise ValueError
    return _Logo(path, (width, height), sha256)


def _read_member(archive: zipfile.ZipFile, path: str, maximum: int) -> bytes:
    member = archive.getinfo(path)
    # Stored and Deflate members support bounded reads without an archive-supplied dictionary.
    if (
        member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        or not 1 <= member.file_size <= maximum
    ):
        raise ValueError
    with archive.open(member) as source:
        content = source.read(maximum + 1)
    if len(content) != member.file_size:
        raise ValueError
    return content


def _read_logo(archive: zipfile.ZipFile, logo: _Logo) -> bytes:
    content = _read_member(archive, _ROOT + logo.path, _MAX_LOGO_BYTES)
    if hashlib.sha256(content).hexdigest() != logo.sha256:
        raise ValueError
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content), formats=["PNG"]) as image:
            if image.size != logo.size or getattr(image, "n_frames", 1) != 1:
                raise ValueError
            image.load()
    return content
