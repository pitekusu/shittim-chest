import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const endpoint = "/api/v1/gallery";
const media = "https://gallery-media.example.test";
const thumbnailPath = fileURLToPath(
  new URL("../scripts/og-image-assets/participant-a.webp", import.meta.url),
);
const originalPath = fileURLToPath(
  new URL("../public/assets/shittim-chest-archive-og-5e706f31f069.png", import.meta.url),
);
const schoolLogo = `data:image/webp;base64,${readFileSync(new URL("../scripts/og-image-assets/participant-c.webp", import.meta.url)).toString("base64")}`;
const privateCache = { "cache-control": "private, no-store" };
const csp = [
  "default-src 'self'",
  "base-uri 'self'",
  "connect-src 'self'",
  "font-src 'self'",
  `img-src 'self' data: ${media}`,
  "object-src 'none'",
  "frame-ancestors 'none'",
  "script-src 'self'",
  "style-src 'self'",
].join("; ");
const collectors = {
  first: {
    id: "a".repeat(43),
    displayName: "春の先生",
    avatar: {
      kind: "image",
      url: `${media}/avatar.webp`,
      alt: "春の先生",
      fallbackVariant: "cyan",
    },
  },
  second: { id: "b".repeat(43), displayName: "秋の先生" },
};

function cardFixture(index: number, count: number) {
  const selected = index === count - 1;
  return {
    cardId: index.toString(16).padStart(80, "g"),
    postedAt: new Date(Date.UTC(2026, 10, 14) - index * 60_000).toISOString(),
    performer: "participant-a",
    studentName: `生徒${index + 1}`,
    schoolName: selected ? "ゲヘナ学園" : "アビドス高等学校",
    rarity: selected ? 4 : index % 5 === 0 ? null : index % 4,
    collector: selected ? collectors.second : collectors.first,
    isMine: selected,
    thumbnailReady: true,
  };
}

async function mockGallery(
  page: Page,
  count: number,
  options: {
    readonly waitForSecondPage?: Promise<void>;
    readonly expireFirstOriginal?: boolean;
  } = {},
) {
  const cards = Array.from({ length: count }, (_, index) => cardFixture(index, count));
  const cardsById = new Map(cards.map((card) => [card.cardId, card]));
  const requests = {
    pages: [] as number[],
    details: [] as string[],
    originals: [] as string[],
    png: [] as string[],
    thumbnails: [] as string[],
  };
  const tickets = new Map<string, number>();
  const expiresAt = () => new Date(Date.now() + 300_000).toISOString();
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: true,
        isAdmin: false,
        user: {
          displayName: "閲覧者",
          avatar: { kind: "placeholder", url: null, alt: "閲覧者", fallbackVariant: "cyan" },
        },
        csrfToken: "gallery-test-session",
      },
    }),
  );
  await page.route(
    (url) => url.pathname === endpoint || url.pathname.startsWith(`${endpoint}/`),
    async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === `${endpoint}/school-logos`) {
        await route.fulfill({
          headers: privateCache,
          json: {
            schemaVersion: 1,
            logos: { アビドス高等学校: schoolLogo, ゲヘナ学園: schoolLogo },
          },
        });
      } else if (url.pathname === endpoint) {
        const start = Number(url.searchParams.get("cursor") ?? "0");
        requests.pages.push(start);
        if (start === 100) await options.waitForSecondPage;
        await route.fulfill({
          headers: privateCache,
          json: {
            schemaVersion: 1,
            items: cards.slice(start, start + 100),
            nextCursor: start + 100 < count ? String(start + 100) : null,
          },
        });
      } else if (url.pathname === `${endpoint}/thumbnails`) {
        await route.fulfill({
          headers: privateCache,
          json: {
            schemaVersion: 1,
            items: (url.searchParams.get("ids") ?? "").split(",").map((cardId) => ({
              cardId,
              url: `${media}/thumbnails/${cardId}.webp`,
              expiresAt: expiresAt(),
            })),
          },
        });
      } else {
        const [id, variant] = url.pathname.slice(endpoint.length + 1).split("/");
        const card = id ? cardsById.get(id) : undefined;
        if (!id || !card) {
          await route.fulfill({
            status: 404,
            json: { error: { code: "CARD_NOT_FOUND", message: "Not found", requestId: "fixture" } },
          });
        } else if (variant === "original") {
          requests.originals.push(id);
          const ticket = (tickets.get(id) ?? 0) + 1;
          tickets.set(id, ticket);
          await route.fulfill({
            headers: privateCache,
            json: {
              schemaVersion: 1,
              url: `${media}/originals/${id}.png?ticket=${ticket}`,
              expiresAt: expiresAt(),
            },
          });
        } else {
          requests.details.push(id);
          await route.fulfill({
            headers: privateCache,
            json: { schemaVersion: 1, card, width: 1792, height: 828 },
          });
        }
      }
    },
  );
  await page.route(`${media}/thumbnails/**`, (route) => {
    requests.thumbnails.push(new URL(route.request().url()).pathname);
    return route.fulfill({ path: thumbnailPath, contentType: "image/webp", headers: privateCache });
  });
  await page.route(`${media}/avatar.webp`, (route) =>
    route.fulfill({ path: thumbnailPath, contentType: "image/webp", headers: privateCache }),
  );
  await page.route(`${media}/originals/**`, (route) => {
    const url = new URL(route.request().url());
    requests.png.push(`${url.pathname}${url.search}`);
    if (options.expireFirstOriginal && url.searchParams.get("ticket") === "1") {
      return route.fulfill({ status: 403, headers: privateCache });
    }
    return route.fulfill({ path: originalPath, contentType: "image/png", headers: privateCache });
  });
  await page.route(
    (url) => /^\/gallery(?:\/[^/]+)?$/u.test(url.pathname),
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({
        response,
        headers: { ...response.headers(), "content-security-policy": csp },
      });
    },
  );
  return { cards, requests };
}

function deferred() {
  let release = () => {};
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

test("loads a 1,000-card collection progressively and sorts and filters all metadata", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const remainingPages = deferred();
  const { cards, requests } = await mockGallery(page, 1_000, {
    waitForSecondPage: remainingPages.promise,
  });
  try {
    await page.goto("/gallery");
    await expect(page.getByRole("link", { name: /アロナ（生徒1）、未記録/u })).toBeVisible();
    await expect(page.getByRole("status")).toContainText(/読み込み/u);
    expect(requests.originals).toEqual([]);
    expect(requests.png).toEqual([]);
  } finally {
    remainingPages.release();
  }

  await expect(page.getByRole("status")).toHaveText("1,000枚");
  expect(requests.pages).toEqual([0, 100, 200, 300, 400, 500, 600, 700, 800, 900]);
  const gallery = page.getByRole("list", { name: "獲得した生徒カード" });
  await expect(gallery.getByRole("listitem").first()).toHaveAttribute("aria-setsize", "1000");
  expect(await gallery.getByRole("listitem").count()).toBeLessThan(50);
  await expect.poll(() => requests.thumbnails.length).toBeGreaterThan(0);
  expect(requests.thumbnails.length).toBeLessThan(50);
  const boundary = gallery.getByRole("listitem").last();
  const position = Number(await boundary.getAttribute("aria-posinset"));
  await boundary.getByRole("link").focus();
  await expect(gallery.locator(`[aria-posinset="${position}"]`).getByRole("link")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    gallery.locator(`[aria-posinset="${position + 1}"]`).getByRole("link"),
  ).toBeFocused();
  await page.evaluate(() => window.scrollTo(0, 0));

  await page.getByRole("combobox", { name: "並べ替え" }).click();
  await page.getByRole("option", { name: "レア度が高い順" }).click();
  const lastPageCard = gallery.getByRole("link", { name: /アロナ（生徒1000）、星4つ/u });
  await expect(lastPageCard).toBeVisible();
  await expect(lastPageCard.getByText("★★★★", { exact: true })).toBeVisible();
  await expect(lastPageCard).toHaveAttribute("href", new RegExp(cards[999]!.cardId));

  const collectorFilter = page.getByRole("combobox", { name: "獲得者" });
  await expect(collectorFilter).toHaveJSProperty("tagName", "BUTTON");
  await collectorFilter.click();
  const collectorOptions = page.getByRole("listbox", { name: "獲得者" }).getByRole("option");
  await expect(collectorOptions.first()).toHaveText("秋の先生");
  await expect(collectorOptions.filter({ hasText: "春の先生" }).locator("img")).toHaveAttribute(
    "src",
    collectors.first.avatar.url,
  );
  await page.getByRole("option", { name: "秋の先生" }).click();
  await expect(page.getByRole("status").filter({ hasText: "1枚" })).toHaveText("1枚");
  await expect(lastPageCard).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`collector=${collectors.second.id}`));
  await collectorFilter.click();
  await page.getByRole("option", { name: "すべて" }).click();
  await expect(page.getByRole("status")).toHaveText("1,000枚");

  const schoolFilter = page.getByRole("combobox", { name: "学校" });
  await expect(schoolFilter).toHaveJSProperty("tagName", "BUTTON");
  await schoolFilter.click();
  const schoolOptions = page.getByRole("listbox", { name: "学校" }).getByRole("option");
  await expect(schoolOptions.first()).toContainText("アビドス高等学校");
  await expect(schoolOptions.first().locator("img")).toBeVisible();
  await page.getByRole("option", { name: "ゲヘナ学園" }).click();
  await expect(page.getByRole("status")).toHaveText("1枚");

  await page.getByRole("combobox", { name: "アロプラ安倍" }).click();
  for (const performer of ["アロナ", "プラナ", "安倍晋三"]) {
    await expect(page.getByRole("option", { name: performer }).locator("img")).toBeVisible();
  }
  await page.keyboard.press("Escape");
  await page.getByRole("combobox", { name: "レア度" }).click();
  await expect(
    page.getByRole("option", { name: "星3つ" }).getByText("★★★", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  expect(requests.originals).toEqual([]);
  expect(requests.png).toEqual([]);
});

test("prefetches only metadata and restores focus and scroll after viewing an original", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const { cards, requests } = await mockGallery(page, 48);
  await page.goto("/gallery");
  await expect(page.getByRole("status")).toHaveText("48枚");
  const gallery = page.getByRole("list", { name: "獲得した生徒カード" });
  await gallery.scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, 1100));
  const card = cards[12]!;
  const path = `/gallery/${card.cardId}`;
  const trigger = gallery.locator(`a[href="${path}"]`);
  await trigger.hover();
  const selected = card.cardId;
  await expect.poll(() => requests.details).toContain(selected);
  expect(requests.originals).toEqual([]);
  expect(requests.png).toEqual([]);
  await trigger.focus();
  const beforeScroll = await page.evaluate(() => window.scrollY);
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect.poll(() => requests.png).toEqual([`/originals/${selected}.png?ticket=1`]);
  expect(requests.originals).toEqual([selected]);
  await expect.poll(() => requests.details).toContain(cards[11]!.cardId);
  await expect.poll(() => requests.details).toContain(cards[13]!.cardId);
  await expect(dialog.getByRole("button", { name: "拡大表示を閉じる" })).toBeFocused();
  const next = dialog.getByRole("button", { name: "次のカード" });
  await next.focus();
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveAccessibleName("アロナ（生徒14）");
  await expect(next).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveAccessibleName("アロナ（生徒15）");
  await expect(next).toBeFocused();
  await expect
    .poll(() => requests.originals)
    .toEqual([selected, cards[13]!.cardId, cards[14]!.cardId]);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL(/\/gallery$/u);
  await expect(gallery.locator(`a[href="${path}"]`)).toBeFocused();
  await expect
    .poll(async () => Math.abs((await page.evaluate(() => window.scrollY)) - beforeScroll))
    .toBeLessThan(3);
});

test("supports dark reduced-motion mobile viewing and refreshes an expired PNG URL", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  const { cards, requests } = await mockGallery(page, 3, { expireFirstOriginal: true });
  await page.goto("/gallery");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const firstCard = page.getByRole("link", { name: /アロナ（生徒1）、未記録/u });
  await firstCard.tap();
  const dialog = page.getByRole("dialog", { name: "アロナ（生徒1）" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveCSS("transition-duration", "0s");
  const card = cards[0]!;
  await expect
    .poll(() => requests.png)
    .toEqual([`/originals/${card.cardId}.png?ticket=1`, `/originals/${card.cardId}.png?ticket=2`]);
  const image = dialog.getByRole("img", { name: "アロナ（生徒1）" });
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth))
    .toBeGreaterThan(0);
  const zoom = dialog.getByRole("button", { name: "拡大", exact: true });
  await expect(zoom).toBeEnabled();
  const next = dialog.getByRole("button", { name: "次のカード" });
  await expect(next).toHaveCSS("opacity", "1");
  await zoom.tap();
  await expect(dialog.getByRole("button", { name: "縮小", exact: true })).toBeEnabled();
  await next.tap();
  await expect(page.getByRole("dialog")).toHaveAccessibleName("アロナ（生徒2）");
  await expect(page.getByRole("dialog").getByRole("button", { name: "前のカード" })).toBeVisible();
  await page.getByRole("button", { name: "拡大表示を閉じる" }).tap();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(firstCard).toBeFocused();
});

test("reveals each viewer arrow only at its image edge on a desktop pointer", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await mockGallery(page, 3);
  await page.goto("/gallery");
  await page.getByRole("link", { name: /アロナ（生徒2）/u }).click();

  const dialog = page.getByRole("dialog");
  const image = dialog.getByRole("img", { name: "アロナ（生徒2）" });
  await expect(image).toBeVisible();
  await image.evaluate((element: HTMLImageElement) => element.decode());
  const previous = dialog.getByRole("button", { name: "前のカード" });
  const next = dialog.getByRole("button", { name: "次のカード" });

  await image.hover();
  await expect(previous).toHaveCSS("opacity", "0");
  await expect(next).toHaveCSS("opacity", "0");

  const { width, height } = await image.evaluate((element) => ({
    width: element.clientWidth,
    height: element.clientHeight,
  }));
  await image.hover({ position: { x: width * 0.12, y: height / 2 } });
  await expect(previous).toHaveCSS("opacity", "1");
  await expect(next).toHaveCSS("opacity", "0");

  await image.hover({ position: { x: width * 0.88, y: height / 2 } });
  await expect(previous).toHaveCSS("opacity", "0");
  await expect(next).toHaveCSS("opacity", "1");
  await next.click();
  await expect(dialog).toHaveAccessibleName("アロナ（生徒3）");
  await expect(next).toBeDisabled();
});
