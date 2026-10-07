# Third-party notices

## LINE Seed JP

- Source: `line/seed` release `v20251119`
- Release asset: `seed-v20251119.zip`
- Release asset SHA-256: `97019208d3b6886d5fc584bf5285e09332271247786dd9f9b8d96964812a6290`
- License: SIL Open Font License 1.1 (`third_party/line-seed/OFL.txt`)

Vendored WOFF2 checksums:

| File                         | SHA-256                                                            |
| ---------------------------- | ------------------------------------------------------------------ |
| `LINESeedJP-Regular.woff2`   | `0724ad3f3d0d84b2783eabfbe552f326e56adcfd89c315854640fae480d90601` |
| `LINESeedJP-Bold.woff2`      | `bb4008ed0dfce2d74273f2d58d4b3f67c739c0ea23af49825544fea99aa27450` |
| `LINESeedJP-ExtraBold.woff2` | `d37cb0244179cee1bc9d1b46af27437523dedb042d2024d9707990672f5d057a` |

Web-only derivatives in `src/assets/fonts/web/` split all supported characters
into disjoint CSS `unicode-range` groups. The common group covers the interface,
kana, Latin text, and punctuation; every remaining character is retained in
another group. Combining marks are included as shaping support. The original
fonts above remain unchanged for server-side OG rendering.

CSS ranges include gaps absent from the original cmap to keep the stylesheet
small; those characters retain the original system-font fallback. Common
characters are excluded from the other ranges to avoid overlapping ownership.

These derivatives keep the copyright and OFL metadata and use the same OFL 1.1
license. `manifest.json` records the source and derivative checksums. FontTools
and Brotli are pinned in the separate build-only `scripts/fonts/uv.lock`.

- Regenerate: `uv run --project scripts/fonts --frozen python scripts/fonts/generate_fonts.py --write`
- Check all original Unicode coverage and advance metrics: `uv run --project scripts/fonts --frozen python scripts/fonts/generate_fonts.py --check`
- Check reproducibility: use `--check-determinism` instead of `--check`
- Compare original and sharded glyph pixels, combining text, and system fallback: `node scripts/fonts/check-rendering.mjs`
- Normal Web builds verify the checked-in derivative checksums without Python.

## Delogy

- Designer: Rasul Hasan
- Source and license: <https://befonts.com/delogy-typeface.html>
- License: Commercial Use Allowed
- Vendored without conversion or subsetting as `Delogy-Regular.ttf`
- SHA-256: `7e8c1c12ab4a537da2d0e558cf3cbb3f81f8b2d98420a7f6891a50cee4e01e63`
