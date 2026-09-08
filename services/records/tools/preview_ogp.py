"""Render fictional OGP examples for visual review; never reads production records."""

import argparse
from datetime import datetime
from pathlib import Path

from PIL import Image

from shittim_records.ogp import RecordPreview
from shittim_records.ogp_render import PreviewRenderer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[3]
    renderer = PreviewRenderer(
        assets=root / "apps/records-web/src/assets/fonts",
        emoji_path=root / "services/records/third_party/noto-emoji/NotoColorEmoji.ttf",
    )
    examples = [
        ("normal", "秋の休日、友人と過ごすならどんな一日にしよう?", "空色の旅人"),
        (
            "long",
            "架空の相談です。" + "秋の週末にみんなで楽しく過ごせる場所や遊びを考えてください。" * 8,
            "とても長い表示名の架空の依頼者です" * 5,
        ),
        (
            "emoji",
            "夜の星空を眺めながら楽しむ、小さな旅の計画を考えよう。",
            "星空の旅人 \U0001f31f\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466 "
            "\U0001f1ef\U0001f1f5",
        ),
    ]
    for label, question, name in examples:
        preview = RecordPreview(
            "a" * 43, question, name, datetime.fromisoformat("2026-09-08T12:00:00+00:00")
        )
        path = args.output / f"{label}.png"
        path.write_bytes(renderer.render(preview))
        with Image.open(path) as image:
            image.resize((600, 315)).save(args.output / f"{label}-small.png")
    print(f"Rendered {len(examples)} fictional previews")


if __name__ == "__main__":
    main()
