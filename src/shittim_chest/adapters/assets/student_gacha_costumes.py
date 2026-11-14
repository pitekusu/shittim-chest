"""Resolve the selected roster entry to full-size Blue Archive Wiki artwork."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from itertools import pairwise
from typing import TYPE_CHECKING, Final, override
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit

import httpx2

from shittim_chest.adapters.assets.images import MAX_IMAGE_BYTES, DebateImageFailure, validate_image
from shittim_chest.adapters.tls import create_public_tls_context
from shittim_chest.application.student_gacha_models import GachaGenerationFailure

if TYPE_CHECKING:
    from shittim_chest.application.student_gacha_models import Student

_WIKI_URL: Final = "https://bluearchive.wikiru.jp/"
_NPC_PAGE: Final = "NPC一覧"
_HTML_MAX_BYTES: Final = 4 * 1024 * 1024
_TIMEOUT_SECONDS: Final = 20
_ATTACHMENT_PATH = re.compile(r"attach2/[0-9A-Fa-f]+_[0-9A-Fa-f]+\.(png|jpe?g|webp)")
_MIN_ART_HEIGHT: Final = 240
# The Decagrammaton profile uses an emblem, while its manifested form has a costume.
_MANIFESTED_FORM = {"デカグラマトン": "マルクト\uff08デカグラマトン\uff09.png"}
_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
_HEADING_LEVELS = {tag: level for level, tag in enumerate(("h2", "h3", "h4", "h5"), start=2)}
_GROUP_LABELS = {
    ("モブキャラクター\uff08市民\uff09", "Animal2", "動物市民\uff08子供\uff09"): "子供",
    ("モブキャラクター\uff08市民\uff09", "Animal2", "動物市民\uff08婦人\uff09"): "婦人",
    ("スズメ亭の女将", "", "スズメ亭の女将"): "女将",
    ("連邦生徒会長", "", "連邦生徒会長"): "",
    ("連邦生徒会長\uff08金髪\uff09", "", "連邦生徒会長\uff08金髪\uff09"): "",
}
# Two NPC listing links have stale fragment identifiers on the same source page.
_GROUP_ANCHORS = {
    ("モブキャラクター\uff08連邦生徒会\uff09", "Student", "行政官"): "ADCOM",
    ("モブキャラクター\uff08その他\uff09", "Athletics", "海賊"): "Pirate",
}


@dataclass(frozen=True, slots=True)
class _Picture:
    source: str
    link: str
    height: int


@dataclass(slots=True)
class _Cell:
    tag: str
    colspan: int
    rowspan: int
    text: list[str] = field(default_factory=list)
    pictures: list[_Picture] = field(default_factory=list)

    def label(self) -> str:
        return "".join(self.text).replace("\n", "").strip()


@dataclass(slots=True)
class _Table:
    anchors: frozenset[str]
    section: str
    school: str
    rows: list[list[_Cell]] = field(default_factory=list)


class _WikiTables(HTMLParser):
    """Collect picture cells and the enclosing page headings inside the Wiki body."""

    def __init__(self, *, manifested_form: str | None = None) -> None:
        super().__init__()
        self.tables: list[_Table] = []
        self.manifested_pictures: list[_Picture] = []
        self._manifested_form = manifested_form
        self._body_depth = 0
        self._table_depth = 0
        self._table: _Table | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._headings: dict[int, tuple[str, str]] = {}
        self._heading_level = 0
        self._heading_anchor = ""
        self._heading_text: list[str] = []
        self._link = ""
        self._ruby_reading = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div" and (self._body_depth or attributes.get("id") == "body"):
            self._body_depth += 1
        if not self._body_depth:
            return
        if (
            tag == "img"
            and self._manifested_form is not None
            and attributes.get("alt") == self._manifested_form
            and (picture := _picture(attributes, link="")) is not None
        ):
            self.manifested_pictures.append(picture)
        if tag in _HEADING_LEVELS:
            self._heading_level = _HEADING_LEVELS[tag]
            self._heading_anchor = ""
            self._heading_text = []
        elif tag == "a" and self._heading_level and attributes.get("class") == "anchor_super":
            self._heading_anchor = attributes.get("id") or self._heading_anchor
        elif tag == "table":
            if self._table_depth == 0 and attributes.get("class") == "style_table":
                self._table = _Table(
                    frozenset(anchor for anchor, _ in self._headings.values() if anchor),
                    self._headings.get(2, ("", ""))[1],
                    self._headings.get(3, ("", ""))[1],
                )
            self._table_depth += 1
        elif self._table is not None and self._table_depth == 1:
            self._start_cell_tag(tag, attributes)

    def _start_cell_tag(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = _Cell(
                tag,
                _table_span(attributes.get("colspan")),
                _table_span(attributes.get("rowspan")),
            )
        elif tag == "a":
            self._link = attributes.get("href") or ""
        elif tag in {"rt", "rp"}:
            self._ruby_reading = True
        elif (
            tag == "img"
            and self._cell is not None
            and (picture := _picture(attributes, link=self._link)) is not None
        ):
            self._cell.pictures.append(picture)

    @override
    def handle_endtag(self, tag: str) -> None:
        if not self._body_depth:
            return
        if tag == "div":
            self._body_depth -= 1
        if tag in _HEADING_LEVELS and self._heading_level:
            self._headings = {
                level: value
                for level, value in self._headings.items()
                if level < self._heading_level
            }
            self._headings[self._heading_level] = (
                self._heading_anchor,
                "".join(self._heading_text).strip(),
            )
            self._heading_level = 0
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
        elif self._table is not None and self._table_depth == 1:
            self._end_cell_tag(tag, self._table)

    def _end_cell_tag(self, tag: str, table: _Table) -> None:
        if tag == "a":
            self._link = ""
        elif tag in {"rt", "rp"}:
            self._ruby_reading = False
        elif tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
            self._link = ""
        elif tag == "tr" and self._row is not None:
            table.rows.append(self._row)
            self._row = None

    @override
    def handle_data(self, data: str) -> None:
        if self._heading_level and not self._heading_anchor:
            self._heading_text.append(data)
        if self._cell is not None and self._table_depth == 1 and not self._ruby_reading:
            self._cell.text.append(data)


async def load_wiki_costume(student: Student) -> bytes:
    """Download one exact outfit and normalize it to a static WebP for image editing."""
    try:
        async with (
            asyncio.timeout(_TIMEOUT_SECONDS),
            httpx2.AsyncClient(
                base_url=_WIKI_URL,
                follow_redirects=False,
                timeout=8,
                verify=create_public_tls_context(),
                headers={"User-Agent": "shittim-chest student gacha"},
            ) as client,
        ):
            if student.rarity == 0:
                listing = await _page(client, _NPC_PAGE)
                page, anchor = _npc_source(listing.tables, student)
            else:
                page, anchor = student.name, ""
            source = await _page(client, page)
            label = _GROUP_LABELS.get((page, anchor, student.name), student.name)
            anchor = _GROUP_ANCHORS.get((page, anchor, student.name), anchor)
            path, content_type = _outfit_path(
                source.tables, label, page, anchor, source.manifested_pictures
            )
            content = await _download(client, path, content_type, MAX_IMAGE_BYTES)
            decoded = await asyncio.to_thread(validate_image, content, content_type=content_type)
            if decoded.height < _MIN_ART_HEIGHT:
                raise GachaGenerationFailure("gacha_costume_reference_invalid")
            return decoded.vision
    except httpx2.HTTPError, TimeoutError:
        raise GachaGenerationFailure("gacha_costume_source_unavailable") from None
    except DebateImageFailure, UnicodeError:
        raise GachaGenerationFailure("gacha_costume_reference_invalid") from None


async def _download(
    client: httpx2.AsyncClient, path: str, content_type: str, maximum: int
) -> bytes:
    async with client.stream("GET", path) as response:
        if response.status_code != 200:
            raise GachaGenerationFailure("gacha_costume_source_unavailable")
        if response.headers.get("content-type", "").split(";", 1)[0].strip() != content_type:
            raise GachaGenerationFailure("gacha_costume_reference_invalid")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            if len(content) + len(chunk) > maximum:
                raise GachaGenerationFailure("gacha_costume_reference_invalid")
            content.extend(chunk)
        return bytes(content)


async def _page(client: httpx2.AsyncClient, name: str) -> _WikiTables:
    query = urlencode({"cmd": "read", "page": name})
    content = await _download(client, f"?{query}", "text/html", _HTML_MAX_BYTES)
    parser = _WikiTables(manifested_form=_MANIFESTED_FORM.get(name))
    parser.feed(content.decode("utf-8"))
    parser.close()
    return parser


def _npc_source(tables: list[_Table], student: Student) -> tuple[str, str]:
    sources: set[tuple[str, str]] = set()
    for table in tables:
        if table.section != "衣装替え一覧" and (
            table.section != "NPC一覧" or table.school != student.school
        ):
            continue
        if len(table.rows) != 1 or len(table.rows[0]) != 1:
            continue
        cell = table.rows[0][0]
        if cell.label() != f"NPC{student.name}":
            continue
        for picture in cell.pictures:
            if (
                _ATTACHMENT_PATH.fullmatch(picture.source)
                and (source := _npc_link(picture.link)) is not None
            ):
                sources.add(source)
    if len(sources) != 1:
        raise GachaGenerationFailure("gacha_costume_reference_unconfirmed")
    return next(iter(sources))


def _npc_link(href: str) -> tuple[str, str] | None:
    # Use the linked Wiki page name as a query value, never as a request URL.
    try:
        parts = urlsplit(href)
        name = unquote_plus(parts.query, errors="strict")
        anchor = unquote_plus(parts.fragment, errors="strict")
    except ValueError:
        return None
    if (
        parts.scheme
        or parts.netloc
        or parts.path not in {"./", ""}
        or not 1 <= len(name) <= 100
        or not name.isprintable()
        or any(char in name for char in "=&?#/\\")
        or len(anchor) > 100
    ):
        return None
    return name, anchor


def _outfit_path(
    tables: list[_Table], name: str, page: str, anchor: str, manifested_pictures: list[_Picture]
) -> tuple[str, str]:
    if page in _MANIFESTED_FORM:
        path = _full_size_path(manifested_pictures, page)
        if path is not None:
            return path
        raise GachaGenerationFailure("gacha_costume_reference_unconfirmed")
    for table in tables:
        if (anchor or "Profile") not in table.anchors:
            continue
        for row in table.rows:
            # The portrait and exact display name share the profile's 名前 row.
            if (
                len(row) == 3
                and row[1].label() == "名前"
                and row[2].label() == name
                and (path := _full_size_path(row[0].pictures, page)) is not None
            ):
                return path
        if anchor:
            grid = _table_columns(table.rows)
            for headings, artwork in pairwise(grid):
                for columns, label in headings.items():
                    # Multiple depictions with the same roster label use the Wiki's first.
                    if (
                        label.tag == "th"
                        and label.label() == name
                        and (image_cell := artwork.get(columns)) is not None
                        and (path := _full_size_path(image_cell.pictures, page)) is not None
                    ):
                        return path
    raise GachaGenerationFailure("gacha_costume_reference_unconfirmed")


def _table_span(value: str | None) -> int:
    return max(1, int(value)) if value is not None and re.fullmatch(r"[0-9]{1,2}", value) else 1


def _table_columns(rows: list[list[_Cell]]) -> list[dict[tuple[int, int], _Cell]]:
    """Align costume labels and art by their HTML grid columns, accounting for merged cells."""
    occupied_until: dict[int, int] = {}
    grid: list[dict[tuple[int, int], _Cell]] = []
    for row_index, row in enumerate(rows):
        columns: dict[tuple[int, int], _Cell] = {}
        start = 0
        for cell in row:
            while occupied_until.get(start, 0) > row_index:
                start += 1
            stop = start + cell.colspan
            for column in range(start, stop):
                occupied_until[column] = row_index + cell.rowspan
            columns[(start, stop)] = cell
            start = stop
        grid.append(columns)
    return grid


def _picture(attributes: dict[str, str | None], *, link: str) -> _Picture | None:
    source = attributes.get("data-src") or attributes.get("src") or ""
    height = attributes.get("height") or ""
    if source and re.fullmatch(r"[0-9]{1,5}", height):
        return _Picture(source, link, int(height))
    return None


def _full_size_path(pictures: list[_Picture], page: str) -> tuple[str, str] | None:
    paths = {
        path
        for picture in pictures
        if picture.height >= _MIN_ART_HEIGHT
        and (path := _image_path(picture.source, page)) is not None
    }
    if len(paths) > 1:
        raise GachaGenerationFailure("gacha_costume_reference_unconfirmed")
    return next(iter(paths), None)


def _image_path(source: str, page: str) -> tuple[str, str] | None:
    if attachment := _ATTACHMENT_PATH.fullmatch(source):
        return source, _CONTENT_TYPES[attachment[1]]
    # Some profile art uses a ref query instead of the static attachment path.
    # Rebuild only this fixed-origin, read-only form using the current page.
    if not source.startswith("./?plugin=ref&"):
        return None
    try:
        query = parse_qsl(source[3:], strict_parsing=True, errors="strict", max_num_fields=3)
    except ValueError:
        return None
    args = dict(query)
    filename = args.get("src", "")
    extension = filename.rpartition(".")[2]
    if (
        args.keys() != {"plugin", "page", "src"}
        or args["plugin"] != "ref"
        or args["page"] != page
        or not 1 <= len(filename) <= 240
        or not filename.isprintable()
        or any(char in filename for char in "/\\?#")
        or extension not in _CONTENT_TYPES
    ):
        return None
    return "?" + urlencode(query), _CONTENT_TYPES[extension]
