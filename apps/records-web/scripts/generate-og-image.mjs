import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const outputDirectory = new URL("../public/assets/", import.meta.url);
const indexPath = new URL("../index.html", import.meta.url);
const ogImagePathPattern = /\/assets\/shittim-chest-archive-og-(?:v\d+|[a-f0-9]{12})\.png/g;

const [delogy, lineSeed, aronaAvatar, planaAvatar, abeAvatar] = await Promise.all([
  readFile(new URL("../src/assets/fonts/Delogy-Regular.ttf", import.meta.url), "base64"),
  readFile(new URL("../src/assets/fonts/LINESeedJP-ExtraBold.woff2", import.meta.url), "base64"),
  readFile(new URL("./og-image-assets/participant-a.webp", import.meta.url), "base64"),
  readFile(new URL("./og-image-assets/participant-b.webp", import.meta.url), "base64"),
  readFile(new URL("./og-image-assets/participant-c.webp", import.meta.url), "base64"),
]);

const html = String.raw`<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <style>
      @font-face {
        font-family: "Delogy";
        src: url("data:font/ttf;base64,${delogy}") format("truetype");
        font-display: block;
      }

      @font-face {
        font-family: "LINE Seed JP";
        src: url("data:font/woff2;base64,${lineSeed}") format("woff2");
        font-display: block;
        font-weight: 800;
      }

      * {
        box-sizing: border-box;
      }

      html,
      body {
        width: 1200px;
        height: 630px;
        margin: 0;
        overflow: hidden;
      }

      body {
        position: relative;
        display: grid;
        place-items: center;
        background:
          radial-gradient(circle at 11% 8%, rgb(255 255 255 / 96%), transparent 29%),
          linear-gradient(145deg, #f5fbff 10%, #dff8fb 72%, #fde8f1 145%);
        color: #17324d;
      }

      body::before {
        position: absolute;
        inset: -210px auto auto -90px;
        width: 690px;
        height: 690px;
        border: 1px solid rgb(88 217 232 / 30%);
        border-radius: 50%;
        content: "";
      }

      body::after {
        position: absolute;
        right: -120px;
        bottom: -320px;
        width: 720px;
        height: 720px;
        border: 1px solid rgb(253 171 208 / 25%);
        content: "";
        transform: rotate(45deg);
      }

      .grid {
        position: absolute;
        inset: 0;
        opacity: 0.22;
        background-image:
          linear-gradient(rgb(88 217 232 / 20%) 1px, transparent 1px),
          linear-gradient(90deg, rgb(88 217 232 / 20%) 1px, transparent 1px);
        background-size: 72px 72px;
        mask-image: linear-gradient(100deg, transparent 12%, black 58%, transparent 100%);
      }

      .geometry {
        position: absolute;
        left: 104px;
        top: 82px;
        display: grid;
        width: 420px;
        height: 420px;
        place-items: center;
      }

      .orbit,
      .diamond {
        position: absolute;
      }

      .orbit {
        width: 404px;
        height: 404px;
        border: 2px solid rgb(88 217 232 / 30%);
        border-radius: 50%;
      }

      .orbit::before,
      .orbit::after {
        position: absolute;
        width: 12px;
        height: 12px;
        border: 3px solid #58d9e8;
        background: #fff;
        content: "";
        transform: rotate(45deg);
      }

      .orbit::before {
        top: 28px;
        left: 94px;
      }

      .orbit::after {
        right: 14px;
        bottom: 98px;
      }

      .diamond {
        width: 270px;
        height: 270px;
        border: 2px solid rgb(88 217 232 / 34%);
        transform: rotate(45deg);
      }

      .mark {
        position: relative;
        display: grid;
        width: 112px;
        height: 112px;
        place-items: center;
        border: 2px solid rgb(255 255 255 / 94%);
        border-radius: 50%;
        background: rgb(255 255 255 / 48%);
        box-shadow:
          0 0 0 15px rgb(88 217 232 / 13%),
          0 18px 50px rgb(45 119 148 / 12%);
      }

      .mark::before {
        position: absolute;
        width: 76%;
        height: 76%;
        border: 1px solid rgb(88 217 232 / 62%);
        border-radius: 50%;
        content: "";
      }

      .mark span {
        width: 42px;
        height: 42px;
        border: 10px solid #58d9e8;
        background: rgb(255 255 255 / 52%);
        transform: rotate(45deg);
      }

      .panel {
        position: absolute;
        right: 84px;
        top: 96px;
        display: grid;
        width: 520px;
        min-height: 438px;
        align-content: center;
        padding: 44px 58px;
        border: 1px solid rgb(255 255 255 / 94%);
        border-radius: 34px;
        background: rgb(255 255 255 / 78%);
        box-shadow: 0 24px 72px rgb(45 119 148 / 15%);
        text-align: center;
        backdrop-filter: blur(24px);
      }

      h1 {
        margin: 0;
        font-family: "Delogy", sans-serif;
        font-size: 30px;
        font-weight: 400;
        letter-spacing: 0.025em;
        line-height: 1.18;
      }

      h1 span {
        display: block;
        white-space: nowrap;
      }

      .rule {
        width: 84px;
        height: 3px;
        margin: 25px auto 22px;
        border-radius: 999px;
        background: linear-gradient(90deg, #58d9e8, #77c6ee);
      }

      .description {
        margin: 0;
        font-family: "LINE Seed JP", sans-serif;
        font-size: 24px;
        font-weight: 800;
        letter-spacing: 0.025em;
        line-height: 1.55;
      }

      .participants {
        display: flex;
        align-items: flex-start;
        justify-content: center;
        gap: 18px;
        margin: 26px 0 0;
      }

      .participant {
        display: grid;
        width: 92px;
        justify-items: center;
        gap: 7px;
      }

      .participant img {
        width: 66px;
        height: 66px;
        border: 3px solid rgb(255 255 255 / 96%);
        border-radius: 50%;
        background: #f5fbff;
        box-shadow:
          0 0 0 2px rgb(88 217 232 / 58%),
          0 8px 22px rgb(45 119 148 / 14%);
        object-fit: cover;
      }

      .participant span {
        font-family: "LINE Seed JP", sans-serif;
        font-size: 13px;
        font-weight: 800;
        line-height: 1.2;
        white-space: nowrap;
      }
    </style>
  </head>
  <body>
    <div class="grid" aria-hidden="true"></div>
    <div class="geometry" aria-hidden="true">
      <div class="orbit"></div>
      <div class="diamond"></div>
      <div class="mark"><span></span></div>
    </div>
    <main class="panel">
      <h1><span>THE SHITTIM</span><span>CHEST ARCHIVE</span></h1>
      <div class="rule"></div>
      <p class="description">シッテムの箱<br />議事録閲覧システム</p>
      <div class="participants" aria-label="議論参加者">
        <div class="participant">
          <img src="data:image/webp;base64,${aronaAvatar}" alt="" />
          <span>アロナ</span>
        </div>
        <div class="participant">
          <img src="data:image/webp;base64,${planaAvatar}" alt="" />
          <span>プラナ</span>
        </div>
        <div class="participant">
          <img src="data:image/webp;base64,${abeAvatar}" alt="" />
          <span>安倍晋三AI</span>
        </div>
      </div>
    </main>
  </body>
</html>`;

await mkdir(outputDirectory, { recursive: true });

const browser = await chromium.launch({ headless: true });
let image;
try {
  const page = await browser.newPage({
    colorScheme: "light",
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
    viewport: { width: 1200, height: 630 },
  });
  await page.setContent(html, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);

  const fontsReady = await page.evaluate(
    () =>
      document.fonts.check('30px "Delogy"', "THE SHITTIM CHEST ARCHIVE") &&
      document.fonts.check('24px "LINE Seed JP"', "シッテムの箱 議事録閲覧システム"),
  );
  if (!fontsReady) {
    throw new Error("og_image_fonts_unavailable");
  }

  image = await page.screenshot({
    animations: "disabled",
    type: "png",
  });
} finally {
  await browser.close();
}

const contentHash = createHash("sha256").update(image).digest("hex").slice(0, 12);
const outputFileName = `shittim-chest-archive-og-${contentHash}.png`;
const outputPath = fileURLToPath(new URL(outputFileName, outputDirectory));
const publicImagePath = `/assets/${outputFileName}`;
const index = await readFile(indexPath, "utf8");
const imagePathMatches = [...index.matchAll(ogImagePathPattern)];

if (imagePathMatches.length !== 3 || new Set(imagePathMatches.map(([match]) => match)).size !== 1) {
  throw new Error("og_image_metadata_contract_mismatch");
}

await writeFile(outputPath, image);
await writeFile(indexPath, index.replaceAll(ogImagePathPattern, publicImagePath));

process.stdout.write(`Generated public/assets/${outputFileName} (1200x630)\n`);
