import { readFile } from "node:fs/promises";

import { chromium } from "@playwright/test";

const fonts = new URL("../../src/assets/fonts/", import.meta.url);
const generated = (
  await readFile(new URL("../../src/styles/fonts.generated.css", import.meta.url), "utf8")
)
  .replaceAll("LINE Seed JP", "Sharded")
  .replaceAll("../assets/fonts/", "/fonts/");
const original = [
  ["Regular", 400],
  ["Bold", 700],
  ["ExtraBold", 800],
]
  .map(
    ([name, weight]) =>
      `@font-face{font-family:Source;src:url('/fonts/LINESeedJP-${name}.woff2') format('woff2');font-weight:${weight};font-display:swap;}`,
  )
  .join("");
const html = `<!DOCTYPE html><html lang="ja"><style>${original}${generated}</style><canvas id="canvas" width="1000" height="100"></canvas></html>`;
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:4197") {
      await route.abort();
      return;
    }
    if (url.pathname.startsWith("/fonts/")) {
      const file = new URL(url.pathname.slice("/fonts/".length), fonts);
      if (!file.href.startsWith(fonts.href)) {
        await route.abort();
        return;
      }
      await route.fulfill({ contentType: "font/woff2", body: await readFile(file) });
      return;
    }
    await route.fulfill({ contentType: "text/html", body: html });
  });
  await page.goto("http://127.0.0.1:4197/");
  const results = await page.evaluate(async () => {
    // Include source-supported supplementary glyphs, combining sequences, and
    // unsupported characters which must keep the original system-font fallback.
    const samples = [
      "シッテムの箱 議論の記録 1234567",
      "か\u3099 ハ\u309a e\u0301",
      "髙 𡨚 𤏐 爨",
      "﨑 𠮟",
      "ffi AV",
      "🙂☕️",
    ];
    const canvas = document.getElementById("canvas");
    const context = canvas.getContext("2d");
    const comparisons = [];
    for (const weight of [400, 700, 800]) {
      for (const text of samples) {
        const draw = (family) => {
          context.clearRect(0, 0, canvas.width, canvas.height);
          context.font = `${weight} 42px "${family}"`;
          context.fillStyle = "#123";
          context.fillText(text, 10, 70);
          return {
            width: context.measureText(text).width,
            pixels: context.getImageData(0, 0, canvas.width, canvas.height).data,
          };
        };
        await document.fonts.load(`${weight} 42px "Source"`, text);
        await document.fonts.load(`${weight} 42px "Sharded"`, text);
        const source = draw("Source");
        const sharded = draw("Sharded");
        comparisons.push({
          weight,
          text,
          identical:
            source.width === sharded.width &&
            source.pixels.every((value, index) => value === sharded.pixels[index]),
        });
      }
    }
    return comparisons;
  });
  const failed = results.filter((result) => !result.identical);
  if (failed.length > 0) throw new Error(`Web font rendering differs: ${JSON.stringify(failed)}`);
  console.log(
    `Verified identical source and sharded font pixels/metrics in ${results.length} cases`,
  );
} finally {
  await browser.close();
}
