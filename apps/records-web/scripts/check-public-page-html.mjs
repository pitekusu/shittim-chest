import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";

const origin = "https://shittim.pitekusu.dev";
const output = new URL("../dist/", import.meta.url);
const pages = [
  {
    path: "/privacy-policy",
    title: "プライバシーポリシー | シッテムの箱",
    description: "シッテムの箱の情報の取扱いと問い合わせ窓口。",
  },
  {
    path: "/data-deletion",
    title: "データ削除の申請 | シッテムの箱",
    description: "シッテムの箱のデータ削除を、ログイン不要でメール申請できます。",
  },
];

for (const page of pages) {
  const html = await readFile(new URL(`${page.path.slice(1)}/index.html`, output), "utf8");
  // Parse the actual build output without executing scripts, as a preview crawler does.
  const window = new JSDOM(html).window;
  const { document } = window;
  assert.equal(document.title, page.title, `${page.path} initial title`);
  assert.equal(
    document.querySelector('link[rel="canonical"]')?.getAttribute("href"),
    `${origin}${page.path}`,
  );
  for (const [selector, value] of [
    ['meta[name="description"]', page.description],
    ['meta[property="og:title"]', page.title],
    ['meta[property="og:description"]', page.description],
    ['meta[property="og:url"]', `${origin}${page.path}`],
    ['meta[name="twitter:title"]', page.title],
    ['meta[name="twitter:description"]', page.description],
  ]) {
    assert.equal(document.querySelectorAll(selector).length, 1, `${page.path} unique ${selector}`);
    assert.equal(
      document.querySelector(selector)?.getAttribute("content"),
      value,
      `${page.path} ${selector}`,
    );
  }
  assert.match(
    document.querySelector('meta[property="og:image"]')?.getAttribute("content") ?? "",
    /^https:\/\/shittim\.pitekusu\.dev\/assets\//,
  );
  assert.ok(document.querySelector('script[type="module"][src]'), `${page.path} application entry`);
  window.close();
}
const index = new JSDOM(await readFile(new URL("index.html", output), "utf8")).window;
assert.equal(index.document.title, "THE SHITTIM CHEST ARCHIVE | シッテムの箱 議事録閲覧システム");
assert.equal(
  index.document.querySelector('link[rel="canonical"]')?.getAttribute("href"),
  `${origin}/`,
);
index.close();
console.log("Verified initial metadata for both public HTML pages without JavaScript");
