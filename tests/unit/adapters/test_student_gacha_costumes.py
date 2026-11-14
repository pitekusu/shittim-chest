from __future__ import annotations

import io
from collections.abc import Callable
from html import escape
from urllib.parse import quote

import httpx2
import pytest
from PIL import Image

from shittim_chest.adapters.assets import student_gacha_costumes
from shittim_chest.application.student_gacha_models import GachaGenerationFailure, Student

pytestmark = pytest.mark.asyncio

_HOST = "bluearchive.wikiru.jp"
_WIKI = f"https://{_HOST}/"
_STUDENT = Student("キキョウ\uff08水着\uff09", "百鬼夜行", rarity=3)
_ATTACHMENT = "attach2/AB12_1234.png"
_WRONG_ATTACHMENT = "attach2/AB12_9876.png"


def _png(size: tuple[int, int] = (256, 400)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, "indigo").save(buffer, "PNG")
    return buffer.getvalue()


def _profile(name: str, image: str) -> str:
    return f"""
      <h2>基本情報 <a class="anchor_super" id="Profile">†</a></h2>
      <table class="style_table">
        <tr><th>画像</th><th colspan="2">プロフィール</th></tr>
        <tr><td><img height="400" data-src="{escape(image, quote=True)}"></td>
          <th>名前</th><td>{name}</td></tr>
      </table>"""


def _html(inner: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        text=f'<div id="body">{inner}</div>',
        headers={"Content-Type": "text/html; charset=UTF-8"},
    )


def _mock_http(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx2.Request], httpx2.Response]
) -> list[httpx2.URL]:
    requests: list[httpx2.URL] = []
    client = httpx2.AsyncClient

    def intercept(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == _HOST
        assert request.url.scheme == "https"
        requests.append(request.url)
        return handler(request)

    def create_client(**options: object) -> httpx2.AsyncClient:
        assert options["base_url"] == _WIKI
        assert options["follow_redirects"] is False
        return client(
            base_url=_WIKI,
            follow_redirects=False,
            transport=httpx2.MockTransport(intercept),
        )

    monkeypatch.setattr(student_gacha_costumes.httpx2, "AsyncClient", create_client)
    return requests


async def test_exact_profile_downloads_outfit_as_webp(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/":
            assert request.url.params["page"] == _STUDENT.name
            return _html(
                _profile("キキョウ", _WRONG_ATTACHMENT) + _profile(_STUDENT.name, _ATTACHMENT)
            )
        assert request.url.path == f"/{_ATTACHMENT}"
        return httpx2.Response(200, content=_png(), headers={"Content-Type": "image/png"})

    requests = _mock_http(monkeypatch, respond)
    costume = await student_gacha_costumes.load_wiki_costume(_STUDENT)

    with Image.open(io.BytesIO(costume)) as image:
        assert image.format == "WEBP"
        assert image.size == (256, 400)
    assert len(requests) == 2


async def test_npc_uses_exact_costume_column_and_linked_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "セリナ\uff08ナース\uff09"
    source = "衣装替え"
    href = f"./?{quote(source)}#SportsEvent"

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("page") == "NPC一覧":
            return _html(f"""
            <h2>衣装替え一覧 <a class="anchor_super" id="Costume">†</a></h2>
            <h3>イベント</h3>
              <table class="style_table">
                <tr><td>NPC<br><a href="{href}">
<img height="100" data-src="{_WRONG_ATTACHMENT}"></a>
<a href="{href}">セリナ<br>\uff08ナース\uff09</a></td></tr>
              </table>""")
        if request.url.params.get("page") == source:
            return _html(f"""
            <h4>別のイベント <a class="anchor_super" id="WrongEvent">†</a></h4>
            <table class="style_table"><tr><th>{name}</th></tr>
              <tr><td><img height="400" data-src="{_WRONG_ATTACHMENT}"></td></tr></table>
            <h4>イベント <a class="anchor_super" id="SportsEvent">†</a></h4>
            <table class="style_table">
              <tr><th colspan="2"><a>セリナ</a>\uff08ナース\uff09</th><td rowspan="4"></td></tr>
              <tr><td colspan="2"><img height="400" data-src="{_ATTACHMENT}"></td></tr>
            </table>""")
        assert request.url.path == f"/{_ATTACHMENT}"
        return httpx2.Response(200, content=_png(), headers={"Content-Type": "image/png"})

    requests = _mock_http(monkeypatch, respond)

    assert await student_gacha_costumes.load_wiki_costume(Student(name, "トリニティ", rarity=0))
    assert len(requests) == 3


async def test_same_page_ref_attachment_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "エリ"
    filename = "CH0304_spr (1).png"
    source = f"./?plugin=ref&page={quote(name)}&src={quote(filename)}"

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("cmd") == "read":
            return _html(_profile(name, source))
        assert dict(request.url.params) == {"plugin": "ref", "page": name, "src": filename}
        return httpx2.Response(200, content=_png(), headers={"Content-Type": "image/png"})

    _mock_http(monkeypatch, respond)
    assert await student_gacha_costumes.load_wiki_costume(Student(name, "ワイルドハント", rarity=1))


async def test_decagrammaton_uses_manifested_form_art(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "デカグラマトン"
    href = "./?" + quote(name)

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("page") == "NPC一覧":
            return _html(f"""
                <h2>NPC一覧</h2><h3>{name}</h3>
                <table class="style_table"><tr><td>NPC<br><a href="{href}">
<img height="100" data-src="{_WRONG_ATTACHMENT}"></a>{name}</td></tr></table>""")
        if request.url.params.get("page") == name:
            return _html(
                _profile(name, _WRONG_ATTACHMENT)
                + f'<img height="505" data-src="{_ATTACHMENT}"'
                + f' alt="マルクト\uff08{name}\uff09.png">'
            )
        assert request.url.path == f"/{_ATTACHMENT}"
        return httpx2.Response(200, content=_png(), headers={"Content-Type": "image/png"})

    _mock_http(monkeypatch, respond)
    assert await student_gacha_costumes.load_wiki_costume(Student(name, name, rarity=0))


@pytest.mark.parametrize(
    "source",
    [
        "https://www.example.org/picture.png",
        "//www.example.org/picture.png",
        "attach2/../picture.png",
        "./?plugin=ref&page=wrong&src=costume.png",
    ],
)
async def test_rejects_image_sources_outside_the_expected_page(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    requests = _mock_http(monkeypatch, lambda _: _html(_profile(_STUDENT.name, source)))

    with pytest.raises(GachaGenerationFailure, match="gacha_costume_reference_unconfirmed"):
        await student_gacha_costumes.load_wiki_costume(_STUDENT)

    assert len(requests) == 1


@pytest.mark.parametrize("content", [b"not an image", _png((100, 100))])
async def test_rejects_invalid_or_thumbnail_bytes(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/":
            return _html(_profile(_STUDENT.name, _ATTACHMENT))
        return httpx2.Response(200, content=content, headers={"Content-Type": "image/png"})

    _mock_http(monkeypatch, respond)

    with pytest.raises(GachaGenerationFailure, match="gacha_costume_reference_invalid"):
        await student_gacha_costumes.load_wiki_costume(_STUDENT)
