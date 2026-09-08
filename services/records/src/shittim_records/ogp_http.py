"""Public HTML/PNG boundary and internal, record-ID-only preparation."""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Mapping
from html import escape
from html.parser import HTMLParser
from typing import Any, Protocol

from shittim_records.ogp import IMAGE_VERSION, RECORD_ID, RecordPreview


class PreviewStore(Protocol):
    def preview(self, record_id: str) -> RecordPreview | None: ...
    def index_html(self) -> str: ...
    def cached_image(self, key: str) -> bytes | None: ...
    def save_image(self, key: str, image: bytes) -> bytes: ...
    def avatar(self, key: str) -> bytes | None: ...


class Renderer(Protocol):
    def render(self, preview: RecordPreview, avatar: bytes | None = None) -> bytes: ...


class PreviewService:
    def __init__(self, store: PreviewStore, renderer: Renderer, origin: str) -> None:
        if re.fullmatch(r"https://[a-z0-9.-]+", origin) is None:
            raise ValueError("invalid preview origin")
        self.store = store
        self.renderer = renderer
        self.origin = origin

    def image(self, record_id: str, version: str | None = None) -> bytes | None:
        if version is not None:
            cached = self.store.cached_image(f"ogp/records/{record_id}/{version}.png")
            if cached is not None:
                return cached
        preview = self.store.preview(record_id)
        if preview is None or (version is not None and preview.version != version):
            return None
        if version is None:
            cached = self.store.cached_image(preview.cache_key)
            if cached is not None:
                return cached
        avatar = self.store.avatar(preview.avatar_key) if preview.avatar_key else None
        image = self.renderer.render(preview, avatar)
        return self.store.save_image(preview.cache_key, image)

    def handle(self, event: Mapping[str, Any]) -> dict[str, Any]:
        # API Gateway always supplies requestContext. No HTTP route can select this branch.
        if "requestContext" not in event:
            if set(event) != {"recordId"} or not _record_id(event.get("recordId")):
                return {"prepared": False}
            try:
                return {"prepared": self.image(event["recordId"]) is not None}
            except Exception:
                # Preparation is optional; never log exceptions containing source content.
                return {"prepared": False}
        context = event.get("requestContext", {})
        method = context.get("http", {}).get("method") if isinstance(context, dict) else None
        path = event.get("rawPath", "")
        if method not in {"GET", "HEAD"} or not isinstance(path, str):
            return _response(404, "", "text/plain", "no-store")
        html_match = re.fullmatch(rf"/records/({RECORD_ID})", path)
        image_match = re.fullmatch(rf"/og/records/({RECORD_ID})/({IMAGE_VERSION})\.png", path)
        if html_match:
            try:
                index = self.store.index_html()
            except Exception:
                # The static entry point does not call this Lambda, so no redirect loop.
                return _response(302, "", "text/plain", "no-store", {"Location": "/index.html"})
            try:
                preview = self.store.preview(html_match[1])
                html = metadata_html(index, preview, self.origin) if preview else index
                status = 200 if preview else 404
                cache = "public, max-age=0, s-maxage=60" if preview else "no-store"
            except Exception:
                html, status, cache = index, 200, "no-store"
            return _response(
                status, "" if method == "HEAD" else html, "text/html; charset=utf-8", cache
            )
        if image_match:
            try:
                image = self.image(image_match[1], image_match[2])
                if image is not None:
                    response = _response(
                        200,
                        "" if method == "HEAD" else base64.b64encode(image).decode(),
                        "image/png",
                        "public, max-age=31536000, immutable",
                        {"Content-Length": str(len(image))},
                    )
                    response["isBase64Encoded"] = True
                    return response
            except Exception:
                logging.getLogger(__name__).warning("preview_image_unavailable")
            try:
                parser = _HeadParser(self.store.index_html())
                fallback = parser.image_url
                if not fallback.startswith(self.origin + "/assets/"):
                    raise ValueError("invalid fallback image")
                return _response(302, "", "text/plain", "no-store", {"Location": fallback})
            except Exception:
                return _response(503, "", "text/plain", "no-store")
        return _response(404, "", "text/plain", "no-store")


def _record_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(RECORD_ID, value) is not None


def _response(
    status: int, body: str, content_type: str, cache: str, extra: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
            **(extra or {}),
        },
        "body": body,
    }


class _HeadParser(HTMLParser):
    """Locate existing head metadata without rewriting deployed script/style elements."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.offsets = [0]
        for match in re.finditer("\n", source):
            self.offsets.append(match.end())
        self.removals: list[tuple[int, int]] = []
        self.in_head = False
        self.title_start: int | None = None
        self.head_end: int | None = None
        self.image_url = ""
        self.feed(source)

    def position(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self.in_head = True
        if not self.in_head:
            return
        values = dict(attrs)
        if tag == "title":
            self.title_start = self.position()
        property_name = values.get("property") or values.get("name") or ""
        if property_name == "og:image":
            self.image_url = values.get("content") or ""
        if (
            tag == "meta"
            and (
                property_name.startswith(("og:", "twitter:"))
                or property_name in {"description", "robots"}
            )
        ) or (tag == "link" and values.get("rel") == "canonical"):
            self.removals.append(
                (self.position(), self.position() + len(self.get_starttag_text() or ""))
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.title_start is not None:
            self.removals.append((self.title_start, self.source.index(">", self.position()) + 1))
            self.title_start = None
        if tag == "head":
            self.head_end = self.position()
            self.in_head = False


def metadata_html(index: str, preview: RecordPreview, origin: str) -> str:
    parser = _HeadParser(index)
    if parser.head_end is None:
        raise ValueError("invalid SPA template")
    title = f"{preview.question} | THE SHITTIM CHEST"
    description = f"{preview.requester_name}の議論の記録 · {preview.date_label}"
    image = origin + preview.image_path
    canonical = origin + "/records/" + preview.record_id
    tags = [
        f"<title>{escape(title)}</title>",
        f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
    ]
    values = {
        "description": description,
        "robots": "noindex, nofollow",
        "og:type": "article",
        "og:site_name": "THE SHITTIM CHEST",
        "og:locale": "ja_JP",
        "og:title": title,
        "og:description": description,
        "og:url": canonical,
        "og:image": image,
        "og:image:width": "1200",
        "og:image:height": "630",
        "og:image:type": "image/png",
        "og:image:alt": preview.question,
        "twitter:card": "summary_large_image",
        "twitter:title": title,
        "twitter:description": description,
        "twitter:image": image,
        "twitter:image:alt": preview.question,
    }
    for key, value in values.items():
        attr = "property" if key.startswith("og:") else "name"
        tags.append(f'<meta {attr}="{key}" content="{escape(value, quote=True)}">')
    result = index[: parser.head_end] + "\n".join(tags) + "\n" + index[parser.head_end :]
    for start, end in sorted(parser.removals, reverse=True):
        result = result[:start] + result[end:]
    return result
