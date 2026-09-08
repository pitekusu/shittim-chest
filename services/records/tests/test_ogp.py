"""Preview boundaries use fictional data and never invoke Discord or live AWS."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
import pytest
import regex
from botocore.stub import Stubber
from PIL import Image

from shittim_records.ogp import RecordPreview, preview_text
from shittim_records.ogp_adapters import META_ATTRIBUTES, PROFILE_ATTRIBUTES, AwsPreviewStore
from shittim_records.ogp_http import PreviewService
from shittim_records.ogp_render import PreviewRenderer

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://shittim.pitekusu.dev"
PREVIEW = RecordPreview(
    "a" * 43,
    "架空の相談 <script>alert(1)</script>",
    "架空の依頼者",
    datetime.fromisoformat("2026-09-08T16:00:00+00:00"),
)


class MemoryStore:
    def __init__(self) -> None:
        self.images: dict[str, bytes] = {}
        self.fail = False
        self.value: RecordPreview | None = PREVIEW

    def preview(self, record_id: str) -> RecordPreview | None:
        if self.fail:
            raise RuntimeError("must not be returned")
        return self.value if record_id == PREVIEW.record_id else None

    def index_html(self) -> str:
        return (ROOT / "apps/records-web/index.html").read_text()

    def cached_image(self, key: str) -> bytes | None:
        return self.images.get(key)

    def save_image(self, key: str, image: bytes) -> bytes:
        return self.images.setdefault(key, image)

    def avatar(self, key: str) -> bytes | None:
        return None


class CountingRenderer:
    def __init__(self) -> None:
        self.calls = 0

    def render(self, preview: RecordPreview, avatar: bytes | None = None) -> bytes:
        self.calls += 1
        return b"test-png"


def request(path: str, method: str = "GET") -> dict[str, Any]:
    return {"rawPath": path, "requestContext": {"http": {"method": method}}}


def test_public_html_escapes_preview_preserves_spa_assets_and_png_is_cached() -> None:
    store, renderer = MemoryStore(), CountingRenderer()
    service = PreviewService(store, renderer, ORIGIN)
    response = service.handle(request(f"/records/{PREVIEW.record_id}"))
    html = response["body"]
    assert response["statusCode"] == 200
    assert response["headers"]["X-Robots-Tag"] == "noindex, nofollow"
    assert html.count('property="og:image"') == 1
    assert html.count('rel="canonical"') == 1
    assert html.count("<title>") == 1
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert "/src/main.tsx" in html and 'id="root"' in html
    assert ORIGIN + PREVIEW.image_path in html
    assert "2026年9月9日" in html
    assert "requester_key" not in html and "initialOpinions" not in html
    assert renderer.calls == 0  # Old records are lazy; HTML does not render images.
    image = service.handle(request(PREVIEW.image_path))
    assert base64.b64decode(image["body"]) == b"test-png"
    assert image["headers"]["Content-Type"] == "image/png"
    assert "immutable" in image["headers"]["Cache-Control"]
    assert service.handle(request(PREVIEW.image_path, "HEAD"))["body"] == ""
    assert renderer.calls == 1
    assert service.handle({"recordId": PREVIEW.record_id}) == {"prepared": True}
    assert renderer.calls == 1
    assert service.handle(request(f"/records/{PREVIEW.record_id}", "HEAD"))["body"] == ""


def test_failure_falls_back_without_long_caching_or_internal_http_route() -> None:
    store = MemoryStore()
    service = PreviewService(store, CountingRenderer(), ORIGIN)
    store.fail = True
    response = service.handle(request(f"/records/{PREVIEW.record_id}"))
    assert "must not be returned" not in response["body"]
    assert "archive-og" in response["body"]
    assert response["headers"]["Cache-Control"] == "no-store"
    response = service.handle(request(PREVIEW.image_path))
    assert response["statusCode"] == 302
    assert response["headers"]["Location"].startswith(ORIGIN + "/assets/")
    assert response["headers"]["Cache-Control"] == "no-store"
    assert service.handle({"recordId": PREVIEW.record_id}) == {"prepared": False}
    assert service.handle({"recordId": PREVIEW.record_id, "question": "injected"}) == {
        "prepared": False
    }
    assert service.handle(request("/prepare", "POST"))["statusCode"] == 404


def test_versions_bind_display_information_and_reject_unknown_version_generation() -> None:
    store, renderer = MemoryStore(), CountingRenderer()
    service = PreviewService(store, renderer, ORIGIN)
    assert service.image(PREVIEW.record_id, "0" * 32) is None
    assert renderer.calls == 0
    for changes in (
        {"question": "別の架空の相談"},
        {"requester_name": "別名"},
        {"avatar_key": "requesters/new/avatar.webp"},
    ):
        assert replace(PREVIEW, **changes).version != PREVIEW.version
    assert preview_text("e\u0301" * 200, 160).endswith("e\u0301…")


def test_renderer_handles_long_japanese_combined_emoji_and_bad_avatar() -> None:
    renderer = PreviewRenderer(
        assets=ROOT / "apps/records-web/src/assets/fonts",
        emoji_path=ROOT / "services/records/third_party/noto-emoji/NotoColorEmoji.ttf",
    )
    value = replace(
        PREVIEW,
        question="架空の長い議題です。" * 25,
        requester_name="架空 \U0001f468\u200d\U0001f469\u200d\U0001f467 \U0001f1ef\U0001f1f5",
    )
    image = renderer.render(value, b"not an image")
    with Image.open(BytesIO(image)) as result:
        assert result.size == (1200, 630)
    assert len(image) < 4 * 1024 * 1024
    assert renderer.width(renderer.ellipsize(value.requester_name * 10, 26, 595), 26) <= 595
    assert len(renderer.wrap(value.question, 34, 1000, 4)) <= 4


def test_small_kana_punctuation_and_descenders_keep_their_vertical_positions() -> None:
    renderer = PreviewRenderer(assets=ROOT / "apps/records-web/src/assets/fonts")
    canvas = Image.new("RGB", (490, 100))
    bounds = {}
    for index, character in enumerate("あっー、。Ag"):
        renderer.line(canvas, character, index * 70, 0, 48, "white")
        box = canvas.crop((index * 70, 0, (index + 1) * 70, 100)).getbbox()
        assert box is not None
        bounds[character] = box
    assert bounds["っ"][1] > bounds["あ"][1] + 8
    assert bounds["ー"][1] > bounds["あ"][1] + 8
    assert bounds["、"][1] > bounds["っ"][1] + 8
    assert bounds["。"][1] > bounds["っ"][1] + 8
    assert bounds["g"][1] > bounds["A"][1]
    assert bounds["g"][3] > bounds["A"][3]


def test_japanese_wrap_keeps_phrases_and_ellipsis_inside_the_panel() -> None:
    renderer = PreviewRenderer(assets=ROOT / "apps/records-web/src/assets/fonts")
    text = "ちょっと休憩。コーヒーとクッキーで、ゆっくり過ごそう!"
    lines = renderer.wrap(text, 48, 1000, 4)
    assert "".join(lines) == text
    assert any("ゆっくり" in line for line in lines)
    assert all(renderer.width(line, 48) <= 1000 for line in lines)
    long = renderer.wrap("カタカナ" * 100, 34, 1000, 4)
    assert len(long) == 4 and long[-1].endswith("…")
    assert all(renderer.width(line, 34) <= 1000 for line in long)
    assert renderer.wrap("(" * 100, 34, 1000, 4) == ["…"]


@pytest.mark.parametrize(
    "text",
    [
        "あっちへ行こう、コーヒーを飲もう。",
        "今日は「ゆっくり休もう」。明日も楽しもう。",
        "星空の旅人👨‍👩‍👧‍👦と日本🇯🇵でカフェに行こう。",
    ],
)
def test_japanese_wrap_obeys_kinsoku_without_splitting_graphemes(text: str) -> None:
    renderer = PreviewRenderer(assets=ROOT / "apps/records-web/src/assets/fonts")
    lines = renderer.wrap(text, 32, 240, 20)
    assert "".join(lines) == text
    assert [part for line in lines for part in regex.findall(r"\X", line)] == regex.findall(
        r"\X", text
    )
    for line in lines:
        assert renderer.width(line, 32) <= 240
        assert line[0] not in "、。）」っー"  # noqa: RUF001 - Japanese kinsoku characters
        assert line[-1] not in "（「("  # noqa: RUF001 - Japanese kinsoku characters


def test_aws_reads_project_only_public_attributes_and_missing_cache_without_list_permission() -> (
    None
):
    dynamodb = boto3.resource(
        "dynamodb",
        region_name="ap-northeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - local Stubber, no network
    )
    s3 = boto3.client(
        "s3",
        region_name="ap-northeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 - local Stubber, no network
    )
    store = AwsPreviewStore(
        dynamodb.Table("archive"),
        dynamodb.Table("profiles"),
        s3,
        media_bucket="media",
        web_bucket="web",
    )
    with Stubber(dynamodb.meta.client) as stub:
        meta = {
            "PK": {"S": "RECORD#" + PREVIEW.record_id},
            "SK": {"S": "META"},
            "schema_version": {"N": "3"},
            "record_type": {"S": "archive_meta"},
            "record_id": {"S": PREVIEW.record_id},
            "question": {"S": "架空"},
            "requester_key": {"S": "b" * 43},
            "requester_display_name": {"S": "保存済みの名前"},
            "completed_at": {"S": "2026-09-08T12:00:00+00:00"},
        }
        for table, key, attributes, response in (
            (
                "archive",
                {"PK": "RECORD#" + PREVIEW.record_id, "SK": "META"},
                META_ATTRIBUTES,
                {"Item": meta},
            ),
            ("profiles", {"PK": "PROFILE#REQUESTER", "SK": "b" * 43}, PROFILE_ATTRIBUTES, {}),
        ):
            names = {f"#a{i}": attr for i, attr in enumerate(attributes)}
            stub.add_response(
                "get_item",
                response,
                {
                    "TableName": table,
                    "Key": key,
                    "ConsistentRead": True,
                    "ProjectionExpression": ", ".join(names),
                    "ExpressionAttributeNames": names,
                },
            )
        result = store.preview(PREVIEW.record_id)
        assert result is not None and result.requester_name == "保存済みの名前"
        assert result.avatar_key is None
        stub.assert_no_pending_responses()
    with Stubber(s3) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        assert store.cached_image(PREVIEW.cache_key) is None
    with Stubber(s3) as stub:
        stub.add_client_error(
            "put_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params={
                "Bucket": "media",
                "Key": PREVIEW.cache_key,
                "Body": b"new",
                "ContentType": "image/png",
                "CacheControl": "public, max-age=31536000, immutable",
                "ServerSideEncryption": "AES256",
                "IfNoneMatch": "*",
            },
        )
        stub.add_response(
            "get_object",
            {"Body": BytesIO(b"winner"), "ContentLength": 6},
            {"Bucket": "media", "Key": PREVIEW.cache_key},
        )
        assert store.save_image(PREVIEW.cache_key, b"new") == b"winner"
    with pytest.raises(ValueError, match="invalid preview record"):
        store.preview("../secrets")
