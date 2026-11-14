# ruff: noqa: RUF001 -- Full-width parentheses are part of Wiki character names.
"""Regression checks for refreshing the Blue Archive character roster."""

from pathlib import Path

import pytest
from tools import update_student_gacha_roster as updater

_PLAYERS = """<div id="body"><table id="sortabletable1">
<tr><th>レ<br />ア</th><th>名前</th><th>学校</th><th>募<br />集</th><th>入<br />手</th></tr>
<tr><td>★1</td><td>アスナ</td><td>ミレニアム</td><td>○</td><td>○</td></tr>
<tr><td>★1</td><td>キリノ</td><td>ヴァルキューレ</td><td>◇</td><td>○</td></tr>
<tr><td>★1</td><td>イブキ</td><td>ゲヘナ</td><td>-</td><td>◇</td></tr>
<tr><td>★2</td><td>アイリ</td><td>トリニティ</td><td>-</td><td>◇</td></tr>
<tr><td>★3</td><td>キキョウ<br />（水着）</td><td>百鬼夜行</td><td>○</td><td>-</td></tr>
<tr><td>★3</td><td>キキョウ（水着）</td><td>百鬼夜行</td><td>○</td><td>-</td></tr>
<tr><td></td><td></td><td></td><td></td><td><a>編集</a></td></tr>
</table></div>
"""
_NPC = """<div id="body">
<h2>NPC一覧 <a class="anchor_super">†</a></h2>
<h3>シッテムの箱 <a class="anchor_super">†</a></h3>
<table class="style_table"><tr><td>NPC<br /><a><img></a><br /><a><span>アロナ<br />
&nbsp;</span></a></td></tr></table>
<table class="style_table"><tr><td>NPC<br /><br /><a>プラナ</a></td></tr></table>
<h3>アビドス高等学校 <a class="anchor_super">†</a></h3>
<h4>アビドス生徒会 <a class="anchor_super">†</a></h4>
<table class="style_table"><tr><td>NPC<br /><br /><a>ユメ</a></td></tr></table>
<table class="style_table"><tr><td>NPC<br /><br /><a>ユメ</a></td></tr></table>
<h2>衣装替え一覧 <a class="anchor_super">†</a></h2>
<h3>イベント <a class="anchor_super">†</a></h3>
<table class="style_table"><tr><td>NPC<br /><br /><a>アスナ<br />（制服）</a></td></tr></table>
<h2>コメントフォーム <a class="anchor_super">†</a></h2>
<table class="style_table"><tr><td>NPC<br /><br /><a>コメント</a></td></tr></table>
</div>
"""
_PREVIOUS: list[updater.StudentRow] = [
    {"name": "アスナ", "school": "ミレニアム", "rarity": 1},
    {"name": "ユメ", "school": "アビドス高等学校", "rarity": 0},
    {"name": "アイリ", "school": "トリニティ", "rarity": 2},
    {"name": "キキョウ（水着）", "school": "百鬼夜行", "rarity": 3},
    {"name": "アロナ", "school": "シッテムの箱", "rarity": 4},
]


def test_refresh_keeps_order_and_applies_rarity_rules() -> None:
    refreshed = updater.update_roster(_PREVIOUS, _PLAYERS, _NPC)
    assert refreshed == [
        *_PREVIOUS,
        {"name": "アスナ（制服）", "school": "ミレニアムサイエンススクール", "rarity": 0},
        {"name": "イブキ", "school": "ゲヘナ", "rarity": 3},
        {"name": "キリノ", "school": "ヴァルキューレ", "rarity": 1},
    ]
    assert updater.update_roster(refreshed, _PLAYERS, _NPC) == refreshed


@pytest.mark.parametrize(
    ("players", "npcs", "message"),
    [
        (_PLAYERS.replace("入<br />手", "入手方法"), _NPC, "見出しが変わりました"),
        (_PLAYERS.replace("アスナ</td>", "新キャラクター</td>"), _NPC, "所属を特定できません"),
        (_PLAYERS.replace("アスナ</td>", "&lt;script&gt;</td>"), _NPC, "使用できない"),
        (
            _PLAYERS.replace("ミレニアム</td>", "<script>隠し文字</script></td>"),
            _NPC,
            "未対応の書式",
        ),
        (
            _PLAYERS.replace("★3</td><td>キキョウ（水着）", "★2</td><td>キキョウ（水着）"),
            _NPC,
            "同じ名前の行が一致しません",
        ),
        (_PLAYERS, _NPC.replace("NPC一覧", "NPCリスト"), "見出しが変わりました"),
        (_PLAYERS, _NPC.replace("ユメ", "別のNPC"), "既存の1件を確認できません"),
        (_PLAYERS, _NPC.rsplit("</div>", maxsplit=1)[0], "最後まで取得できません"),
    ],
)
def test_refresh_rejects_incomplete_or_unexpected_sources(
    players: str, npcs: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        updater.update_roster(_PREVIOUS, players, npcs)


def test_offline_check_rejects_invalid_roster_without_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = tmp_path / "roster.json"
    roster.write_bytes(updater._serialize(_PREVIOUS))
    monkeypatch.setattr(updater.sys, "argv", ["updater", "--check", "--roster", str(roster)])

    def fail_on_fetch(page: str) -> str:
        pytest.fail(f"Offline check unexpectedly tried to fetch {page}")

    monkeypatch.setattr(updater, "_fetch", fail_on_fetch)
    assert updater.main() == 0

    roster.write_bytes(
        updater._serialize([*_PREVIOUS, {"name": "プラナ", "school": "シッテムの箱", "rarity": 0}])
    )
    invalid = roster.read_bytes()
    assert updater.main() == 1
    assert roster.read_bytes() == invalid
