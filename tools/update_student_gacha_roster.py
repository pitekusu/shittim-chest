# SPDX-License-Identifier: MIT
"""Refresh the student gacha roster from the two Blue Archive Wiki lists."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from typing import Literal, TypedDict, override

ROSTER_PATH = (
    Path(__file__).resolve().parents[1]
    / "data/student-gacha/blue_archive_characters_with_npcs.json"
)
WIKI_URL = "https://bluearchive.wikiru.jp/"
NAME_PUNCTUATION = " （）()・＊.＆&'’‐-"  # noqa: RUF001 - preserve Wiki spelling.
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_ROSTER_BYTES = 256 * 1024
MAX_ROSTER_ROWS = 2_000
RARITIES = frozenset(range(5))
NPC_EXCLUSIONS = frozenset({("アロナ", "シッテムの箱"), ("プラナ", "シッテムの箱")})
PLAYABLE_RARITY = {"★1": 1, "★2": 2, "★3": 3}
SCHOOL_NAMES = {
    "SRT": "SRT特殊学園",
    "その他": "その他生徒",
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
}


class StudentRow(TypedDict):
    name: str
    school: str
    rarity: int


def _check_text(text: str, maximum: int) -> None:
    if (
        not 1 <= len(text) <= maximum
        or text != text.strip()
        or not unicodedata.is_normalized("NFC", text)
        or not any(unicodedata.category(char).startswith("L") for char in text)
        or any(
            not (unicodedata.category(char)[0] in "LMN" or char in NAME_PUNCTUATION)
            for char in text
        )
    ):
        raise ValueError("名簿に使用できない名前または所属があります")


def _validate_roster(value: object) -> list[StudentRow]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_ROSTER_ROWS:
        raise ValueError("名簿の件数が範囲外です")
    rows: list[StudentRow] = []
    names: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or row.keys() != {"name", "school", "rarity"}:
            raise ValueError("名簿の行は name、school、rarity の3項目が必要です")
        name, school, rarity = row["name"], row["school"], row["rarity"]
        if not isinstance(name, str) or not isinstance(school, str):
            raise ValueError("名簿の名前と所属は文字列が必要です")
        _check_text(name, maximum=60)
        _check_text(school, maximum=40)
        if type(rarity) is not int or rarity not in RARITIES:
            raise ValueError("名簿のレアリティは0〜4の整数が必要です")
        if name in names:
            raise ValueError("名簿に重複した名前があります")
        if rarity == 0 and (name, school) in NPC_EXCLUSIONS:
            raise ValueError("シッテムの箱のアロナとプラナはレアリティ0の対象外です")
        names.add(name)
        rows.append({"name": name, "school": school, "rarity": rarity})
    if {row["rarity"] for row in rows} != RARITIES:
        raise ValueError("名簿はすべてのレアリティに候補が必要です")
    return rows


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("名簿の行に重複したキーがあります")
    return result


def _read_limited(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError(f"サイズの上限を超えています ({path.name})")
    return content


@dataclass
class _Cell:
    parts: list[str] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)

    def text(self) -> str:
        return "".join(self.parts).replace("\n", "").strip()


class _WikiTables(HTMLParser):
    """Read only the student table or the one-cell NPC cards in the page body."""

    def __init__(self, page: Literal["players", "npc"]) -> None:
        super().__init__()
        self.page = page
        self.player_rows: list[list[_Cell]] = []
        self.npc_cards: list[tuple[str, str, _Cell]] = []
        self.sections: list[str] = []
        self.body_depth = 0
        self.body_closed = False
        self.table: list[list[_Cell]] | None = None
        self.row: list[_Cell] | None = None
        self.cell: _Cell | None = None
        self.heading: list[str] | None = None
        self.heading_ended = False
        self.section = ""
        self.affiliation = ""
        self.player_table_found = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            if self.body_depth:
                self.body_depth += 1
            elif attributes.get("id") == "body":
                if self.body_closed:
                    raise ValueError("Wiki本文が重複しています")
                self.body_depth = 1
        if not self.body_depth:
            return

        if self.cell is not None and tag not in {"td", "th"}:
            self.cell.tags.add(tag)
            if tag == "br":
                self.cell.parts.append("\n")
        if self.page == "npc":
            if tag in {"h2", "h3"}:
                self.heading = []
                self.heading_ended = False
            if tag == "a" and attributes.get("class") == "anchor_super":
                self.heading_ended = True

        if tag == "table":
            if self.table is not None:
                raise ValueError("Wikiの名簿表が入れ子になっています")
            if self.page == "players" and attributes.get("id") == "sortabletable1":
                if self.player_table_found:
                    raise ValueError("Wikiのキャラクター一覧表が重複しています")
                self.player_table_found = True
                self.table = []
            elif (
                self.page == "npc"
                and self.section in {"NPC一覧", "衣装替え一覧"}
                and attributes.get("class") == "style_table"
            ):
                self.table = []
        elif self.table is not None:
            if tag == "tr":
                if self.row is not None:
                    raise ValueError("Wikiの名簿表に閉じられていない行があります")
                self.row = []
            if tag in {"td", "th"}:
                if self.row is None or self.cell is not None:
                    raise ValueError("Wikiの名簿表に無効なセルがあります")
                self.cell = _Cell()

    @override
    def handle_endtag(self, tag: str) -> None:
        if not self.body_depth:
            return
        if tag == "div":
            self.body_depth -= 1
            if self.body_depth == 0:
                self.body_closed = True
        if tag in {"h2", "h3"} and self.heading is not None:
            title = "".join(self.heading).strip()
            if tag == "h2":
                self.section = title
                self.affiliation = ""
                self.sections.append(title)
            else:
                self.affiliation = title
            self.heading = None
        if self.table is None:
            return
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(self.cell)
            self.cell = None
        if tag == "tr" and self.row is not None and self.cell is None:
            self.table.append(self.row)
            self.row = None
        if tag == "table":
            self._close_table()

    def _close_table(self) -> None:
        if self.table is None or self.row is not None or self.cell is not None:
            raise ValueError("Wikiの名簿表が閉じられていません")
        if self.page == "players":
            self.player_rows = self.table
        else:
            if len(self.table) != 1 or len(self.table[0]) != 1 or not self.affiliation:
                raise ValueError("WikiのNPCカードの構造が変わりました")
            self.npc_cards.append((self.section, self.affiliation, self.table[0][0]))
        self.table = None

    @override
    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.parts.append(data)
        if self.heading is not None and not self.heading_ended:
            self.heading.append(data)


def _parse_wiki(html: str, page: Literal["players", "npc"]) -> _WikiTables:
    parser = _WikiTables(page)
    parser.feed(html)
    parser.close()
    if not parser.body_closed or parser.table is not None:
        raise ValueError("Wiki本文を最後まで取得できませんでした")
    return parser


def _parse_players(html: str) -> dict[str, StudentRow]:
    rows = _parse_wiki(html, "players").player_rows
    if not rows:
        raise ValueError("Wikiにキャラクター一覧表がありません")
    header = [cell.text() for cell in rows[0]]
    columns = ("レア", "名前", "学校", "入手")
    if any(header.count(column) != 1 for column in columns):
        raise ValueError("Wikiのキャラクター一覧表の見出しが変わりました")
    rarity_index, name_index, school_index, acquire_index = map(header.index, columns)

    players: dict[str, StudentRow] = {}
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError("Wikiのキャラクター一覧表の列数が一致しません")
        if row[-1].text() == "編集" and not any(cell.text() for cell in row[:-1]):
            continue  # Empty template row supplied by the Wiki's table editor.
        rarity_cell, name_cell, school_cell, acquire_cell = (
            row[index] for index in (rarity_index, name_index, school_index, acquire_index)
        )
        if any(
            cell.tags - {"a", "br"} for cell in (rarity_cell, name_cell, school_cell, acquire_cell)
        ):
            raise ValueError("Wikiのキャラクター一覧に未対応の書式があります")
        name, school = name_cell.text(), school_cell.text()
        _check_text(name, maximum=60)
        _check_text(school, maximum=40)
        rarity = PLAYABLE_RARITY.get(rarity_cell.text())
        if rarity is None:
            raise ValueError("Wikiのキャラクター一覧に無効なレアリティがあります")
        if rarity == 1 and "◇" in acquire_cell.text():
            rarity = 3
        player: StudentRow = {"name": name, "school": school, "rarity": rarity}
        if players.get(name, player) != player:
            raise ValueError("Wikiのキャラクター一覧で同じ名前の行が一致しません")
        players[name] = player
    if {row["rarity"] for row in players.values()} != set(PLAYABLE_RARITY.values()):
        raise ValueError("Wikiのキャラクター一覧にレアリティ1〜3がそろっていません")
    return players


def _parse_npcs(html: str) -> tuple[dict[str, StudentRow], set[str]]:
    parsed = _parse_wiki(html, "npc")
    if parsed.sections.count("NPC一覧") != 1 or parsed.sections.count("衣装替え一覧") != 1:
        raise ValueError("WikiのNPC一覧・衣装替え一覧の見出しが変わりました")
    npcs: dict[str, StudentRow] = {}
    costumes: set[str] = set()
    for section, affiliation, cell in parsed.npc_cards:
        lines = [line.strip() for line in "".join(cell.parts).split("\n") if line.strip()]
        if not 2 <= len(lines) <= 3 or lines[0] != "NPC" or cell.tags - {"a", "br", "img", "span"}:
            raise ValueError("WikiのNPCカードに未対応の書式があります")
        name = "".join(lines[1:])
        _check_text(name, maximum=60)
        if section == "衣装替え一覧":
            costumes.add(name)
        elif (name, affiliation) not in NPC_EXCLUSIONS:
            _check_text(affiliation, maximum=40)
            npc: StudentRow = {"name": name, "school": affiliation, "rarity": 0}
            if npcs.get(name, npc) != npc:
                raise ValueError("WikiのNPC一覧で同じ名前に複数の所属があります")
            npcs[name] = npc
    if not npcs or not costumes:
        raise ValueError("WikiのNPC一覧または衣装替え一覧が空です")
    return npcs, costumes


def _costume_school(name: str, affiliations: dict[str, set[str]]) -> str:
    base_name = name.partition("（")[0].partition("＊")[0]  # noqa: RUF001
    schools = affiliations.get(base_name, set())
    if base_name == name or len(schools) != 1:
        raise ValueError(f"Wikiの衣装替え「{name}」の所属を特定できません")
    return next(iter(schools))


def update_roster(previous: list[StudentRow], players_html: str, npc_html: str) -> list[StudentRow]:
    players = _parse_players(players_html)
    npcs, costumes = _parse_npcs(npc_html)
    affiliations: dict[str, set[str]] = {}
    for row in (*players.values(), *npcs.values()):
        school = SCHOOL_NAMES.get(row["school"], row["school"])
        affiliations.setdefault(row["name"], set()).add(school)

    for name in sorted(costumes - players.keys() - npcs.keys()):
        school = _costume_school(name, affiliations)
        if (name, school) not in NPC_EXCLUSIONS:
            npcs[name] = {"name": name, "school": school, "rarity": 0}

    custom = {row["name"]: row for row in previous if row["rarity"] == 4}
    current = npcs | players | custom
    previous_names = {row["name"] for row in previous}
    missing_names = sorted(previous_names - current.keys())
    if missing_names:
        raise ValueError(
            f"Wikiの一覧から既存の{len(missing_names)}件を確認できません: "
            + "、".join(missing_names[:5])
        )
    if any(row["rarity"] > 0 and current[row["name"]]["rarity"] == 0 for row in previous):
        raise ValueError("Wikiで既存のプレイアブルキャラクターがNPCとしてのみ見つかりました")
    updated = [current[row["name"]] for row in previous]
    updated.extend(current[name] for name in sorted(current.keys() - previous_names))
    return _validate_roster(updated)


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise ValueError("Wikiが一覧を別のURLへ転送しました")


def _fetch(page: Literal["キャラクター一覧", "NPC一覧"]) -> str:
    query = urllib.parse.urlencode({"cmd": "read", "page": page})
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS host and encoded page.
        f"{WIKI_URL}?{query}",
        headers={"User-Agent": "shittim-chest student roster maintenance"},
    )
    opener = urllib.request.build_opener(_RejectRedirect())
    with opener.open(request, timeout=25) as response:
        if response.headers.get_content_type() != "text/html":
            raise ValueError("Wikiの応答がHTMLではありません")
        content = response.read(MAX_HTML_BYTES + 1)
    if len(content) > MAX_HTML_BYTES:
        raise ValueError("WikiのHTMLがサイズの上限を超えています")
    return content.decode("utf-8")


def _serialize(rows: list[StudentRow]) -> bytes:
    content = (json.dumps(rows, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(content) > MAX_ROSTER_BYTES:
        raise ValueError("名簿がサイズの上限を超えています")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", type=Path, default=ROSTER_PATH, help="JSON roster path")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--check", action="store_true", help="validate the roster offline")
    source.add_argument("--players-html", type=Path, help="saved character list HTML")
    parser.add_argument("--npc-html", type=Path, help="saved NPC list HTML")
    args = parser.parse_args()
    if bool(args.players_html) != bool(args.npc_html):
        parser.error("--players-html and --npc-html must be used together")

    try:
        original = _read_limited(args.roster, MAX_ROSTER_BYTES)
        previous = _validate_roster(json.loads(original, object_pairs_hook=_unique_object))
        if args.check:
            if original != _serialize(previous):
                raise ValueError("名簿のJSON形式が正規化されていません")
            print(f"名簿は有効です ({len(previous)}件)")
            return 0

        if args.players_html is None:
            players_html = _fetch("キャラクター一覧")
            npc_html = _fetch("NPC一覧")
        else:
            players_html = _read_limited(args.players_html, MAX_HTML_BYTES).decode("utf-8")
            npc_html = _read_limited(args.npc_html, MAX_HTML_BYTES).decode("utf-8")
        current = update_roster(previous, players_html, npc_html)
        updated = _serialize(current)
        if original == updated:
            print(f"名簿に差分はありません ({len(current)}件)")
            return 0
        args.roster.write_bytes(updated)
        changed = sum(old != new for old, new in zip(previous, current, strict=False))
        print(f"名簿を更新しました (追加{len(current) - len(previous)}件、変更{changed}件)")
    except (OSError, ValueError, RecursionError, HTTPException) as error:
        print(f"名簿の確認に失敗しました: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
