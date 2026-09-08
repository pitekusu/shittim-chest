"""Deterministic 1200 x 630 preview artwork using bundled, licensed fonts."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Literal

import budoux
import regex
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from shittim_records.ogp import RecordPreview

ASSETS = Path(__file__).with_name("assets")
NAVY = "#17324d"
MUTED = "#607e92"
EMOJI = regex.compile(r"\p{Extended_Pictographic}|\p{Regional_Indicator}|\u20e3")
# Japanese strict kinsoku, including small kana and prolonged sound marks (CJ).
NO_LINE_START = regex.compile(r"[\p{lb=CL}\p{lb=CP}\p{lb=EX}\p{lb=IS}\p{lb=NS}\p{lb=CJ}\p{lb=IN}]")
NO_LINE_END = regex.compile(r"\p{lb=OP}")


class PreviewRenderer:
    def __init__(self, *, assets: Path = ASSETS, emoji_path: Path | None = None) -> None:
        self.assets = assets
        self.emoji_path = emoji_path or assets / "NotoColorEmoji.ttf"
        self.parser = budoux.load_default_japanese_parser()

    @lru_cache(maxsize=24)  # noqa: B019 - bounded cache, one renderer per Lambda environment
    def font(
        self, size: int, weight: Literal["Bold", "Regular"] = "Bold"
    ) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.assets / f"LINESeedJP-{weight}.woff2"), size)

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

    def line(
        self,
        canvas: Image.Image,
        text: str,
        x: float,
        y: int,
        size: int,
        color: str,
        *,
        weight: Literal["Bold", "Regular"] = "Bold",
    ) -> None:
        draw = ImageDraw.Draw(canvas)
        font = self.font(size, weight)
        # Share the font baseline, not each glyph's ink top (small kana/punctuation differ).
        baseline = y + font.getmetrics()[0]
        for part in regex.findall(r"\X", text):
            if EMOJI.search(part):
                tile = self.emoji(part, size)
                canvas.paste(tile, (round(x), baseline - size), tile)
                x += size
            else:
                draw.text((x, baseline), part, font=font, fill=color, anchor="ls")
                x += font.getlength(part)

    def wrap(self, text: str, size: int, width: int, max_lines: int) -> list[str]:
        lines = self._wrap(text, size, width)
        visible = [self.ellipsize(line, size, width) for line in lines[:max_lines]]
        if len(lines) > max_lines:
            visible[-1] = self.ellipsize(visible[-1], size, width, force=True)
        return visible

    def _wrap(self, text: str, size: int, width: int) -> list[str]:
        parts = regex.findall(r"\X", text)
        phrase_ends = set()
        offset = 0
        for phrase in self.parser.parse(text):
            offset += len(phrase)
            phrase_ends.add(offset)
        offsets, widths = [0], [0.0]
        for part in parts:
            offsets.append(offsets[-1] + len(part))
            widths.append(widths[-1] + self.width(part, size))
        lines, start = [], 0
        while start < len(parts):
            if widths[-1] - widths[start] <= width:
                lines.append("".join(parts[start:]).rstrip())
                break
            legal = [
                end
                for end in range(start + 1, len(parts))
                if widths[end] - widths[start] <= width
                and not NO_LINE_END.fullmatch(parts[end - 1][0])
                and not NO_LINE_START.fullmatch(parts[end][0])
                and not parts[end].isspace()
            ]
            if not legal:
                # An unbreakable run cannot hang outside the panel. The caller shrinks
                # the font first, then ellipsizes rather than making an illegal break.
                lines.append("".join(parts[start:]))
                break
            preferred = [end for end in legal if offsets[end] in phrase_ends]
            end = max(preferred or legal)
            lines.append("".join(parts[start:end]).rstrip())
            start = end
        return lines or [""]

    def ellipsize(self, text: str, size: int, width: int, *, force: bool = False) -> str:
        if not force and self.width(text, size) <= width:
            return text
        parts = regex.findall(r"\X", text)
        while parts and (
            self.width("".join(parts) + "…", size) > width
            or NO_LINE_END.fullmatch(parts[-1][0])
            or parts[-1].isspace()
        ):
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
        self.line(canvas, "議論の記録", 965, 95, 20, MUTED, weight="Regular")
        draw.line((80, 150, 1120, 150), fill="#c2e7ef", width=2)
        size = 48
        while size > 34:
            candidate = self._wrap(preview.question, size, 1000)
            if len(candidate) <= 4 and all(self.width(line, size) <= 1000 for line in candidate):
                break
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
        self.line(canvas, "依頼者", 162, 492, 17, MUTED, weight="Regular")
        self.line(canvas, self.ellipsize(preview.requester_name, 26, 595), 162, 522, 26, NAVY)
        self.line(canvas, "完了日", 866, 492, 17, MUTED, weight="Regular")
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
