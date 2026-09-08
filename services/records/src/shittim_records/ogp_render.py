"""Deterministic 1200 x 630 preview artwork using bundled, licensed fonts."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path

import regex
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from shittim_records.ogp import RecordPreview

ASSETS = Path(__file__).with_name("assets")
NAVY = "#17324d"
MUTED = "#607e92"
EMOJI = regex.compile(r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3")


class PreviewRenderer:
    def __init__(self, *, assets: Path = ASSETS, emoji_path: Path | None = None) -> None:
        self.assets = assets
        self.emoji_path = emoji_path or assets / "NotoColorEmoji.ttf"

    @lru_cache(maxsize=12)  # noqa: B019 - bounded cache, one renderer per Lambda environment
    def font(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.assets / "LINESeedJP-ExtraBold.woff2"), size)

    @lru_cache(maxsize=128)  # noqa: B019 - bounded to 128 small tiles
    def emoji(self, text: str, size: int) -> Image.Image:
        # Noto CBDT has a fixed 109px strike. Render once and resample to the line size.
        font = ImageFont.truetype(str(self.emoji_path), 109)
        bounds = font.getbbox(text)
        tile = Image.new("RGBA", (max(1, round(bounds[2])), max(1, round(bounds[3] - bounds[1]))))
        ImageDraw.Draw(tile).text((0, -bounds[1]), text, font=font, embedded_color=True)
        return ImageOps.contain(tile, (size, size), Image.Resampling.LANCZOS)

    def width(self, text: str, size: int) -> float:
        font = self.font(size)
        return sum(
            size if EMOJI.search(part) else font.getlength(part)
            for part in regex.findall(r"\X", text)
        )

    def line(self, canvas: Image.Image, text: str, x: float, y: int, size: int, color: str) -> None:
        draw = ImageDraw.Draw(canvas)
        for part in regex.findall(r"\X", text):
            if EMOJI.search(part):
                tile = self.emoji(part, size)
                canvas.paste(tile, (round(x), y), tile)
                x += size
            else:
                draw.text((x, y), part, font=self.font(size), fill=color, anchor="lt")
                x += self.font(size).getlength(part)

    def wrap(self, text: str, size: int, width: int, max_lines: int) -> list[str]:
        lines = [""]
        for part in regex.findall(r"\X", text):
            if self.width(lines[-1] + part, size) > width:
                if len(lines) == max_lines:
                    lines[-1] = self.ellipsize(lines[-1] + part, size, width, force=True)
                    break
                lines.append("")
            lines[-1] += part
        return lines

    def ellipsize(self, text: str, size: int, width: int, *, force: bool = False) -> str:
        if not force and self.width(text, size) <= width:
            return text
        parts = regex.findall(r"\X", text)
        while parts and self.width("".join(parts) + "…", size) > width:
            parts.pop()
        return "".join(parts) + "…"

    def render(self, preview: RecordPreview, avatar: bytes | None = None) -> bytes:
        canvas = Image.new("RGB", (1200, 630))
        draw = ImageDraw.Draw(canvas)
        for y in range(630):
            t = y / 629
            color = tuple(
                round(a + (b - a) * t)
                for a, b in zip((247, 253, 255), (220, 246, 251), strict=True)
            )
            draw.line((0, y, 1200, y), fill=color)
        for x in range(0, 1200, 48):
            draw.line((x, 0, x, 630), fill="#e4f3f7")
        for y in range(0, 630, 48):
            draw.line((0, y, 1200, y), fill="#e4f3f7")
        draw.ellipse((880, -200, 1380, 300), outline="#bbeaf2", width=2)
        draw.ellipse((910, -170, 1350, 270), outline="#d7cfee", width=2)
        draw.polygon(
            ((1070, 360), (1300, 590), (1070, 820), (840, 590)), outline="#bce9f0", width=2
        )
        draw.rounded_rectangle(
            (48, 48, 1152, 582), radius=32, fill="#f9fdff", outline="#bcdfeb", width=2
        )
        draw.rounded_rectangle((80, 82, 87, 126), radius=3, fill="#32bfd6")
        title_font = ImageFont.truetype(str(self.assets / "Delogy-Regular.ttf"), 30)
        draw.text((106, 88), "THE SHITTIM CHEST", font=title_font, fill=NAVY)
        self.line(canvas, "議論の記録", 965, 95, 20, MUTED)
        draw.line((80, 150, 1120, 150), fill="#c2e7ef", width=2)
        size = 48
        while size > 34 and self.width(preview.question, size) > 1000 * 3.65:
            size -= 2
        lines = self.wrap(preview.question, size, 1000, 4)
        line_height = size + 16
        top = 175 + (264 - len(lines) * line_height) // 2
        for index, line in enumerate(lines):
            self.line(canvas, line, 92, top + index * line_height, size, NAVY)
        draw.line((80, 462, 1120, 462), fill="#c2e7ef", width=2)
        draw.ellipse((80, 490, 144, 554), fill="#dcf5fa", outline="#7ad5e5", width=2)
        avatar_image = self.avatar(avatar)
        if avatar_image is not None:
            mask = Image.new("L", (56, 56))
            ImageDraw.Draw(mask).ellipse((0, 0, 55, 55), fill=255)
            canvas.paste(avatar_image, (84, 494), mask)
        else:
            draw.ellipse((102, 502, 122, 522), fill="#82cadb")
            draw.rounded_rectangle((94, 525, 130, 542), radius=8, fill="#82cadb")
        self.line(canvas, "依頼者", 162, 492, 17, MUTED)
        self.line(canvas, self.ellipsize(preview.requester_name, 26, 595), 162, 522, 26, NAVY)
        self.line(canvas, "完了日", 866, 492, 17, MUTED)
        self.line(canvas, preview.date_label, 866, 524, 23, NAVY)
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()

    @staticmethod
    def avatar(data: bytes | None) -> Image.Image | None:
        if not data or len(data) > 512 * 1024:
            return None
        try:
            with Image.open(BytesIO(data)) as image:
                if image.width * image.height > 4_000_000:
                    return None
                return ImageOps.fit(image.convert("RGB"), (56, 56), Image.Resampling.LANCZOS)
        except OSError, UnidentifiedImageError, Image.DecompressionBombError:
            return None
