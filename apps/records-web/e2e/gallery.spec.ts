import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { GalleryCollectionCard, GalleryRosterEntry } from "../src/api/gallery";

const endpoint = "/api/v1/gallery";
const media = "https://gallery-media.example.test";
const thumbnailPath = fileURLToPath(
  new URL("../scripts/og-image-assets/participant-a.webp", import.meta.url),
);
const originalPath = fileURLToPath(
  new URL("../public/assets/shittim-chest-archive-og-5e706f31f069.png", import.meta.url),
);
const animatedImages = {
  gif: Buffer.from(
    "R0lGODlhCAAEAIEAAPXLmQAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQAFAAAACwAAAAACAAEAAAIDAABCBxIsKDBgwQDAgAh+QQBFAABACwAAAAACAAEAIFvue8AAAAAAAAAAAAIDAABCBxIsKDBgwQDAgA7",
    "base64",
  ),
  apng: Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAECAIAAAA8r+mnAAAACGFjVEwAAAACAAAAAPONk3AAAAAaZmNUTAAAAAAAAAAIAAAABAAAAAAAAAAAABEAZAAAlRJiLgAAABRJREFUeJxj/Hp6JgM2wIRVlCwJAOhiAmFFaNVGAAAAGmZjVEwAAAABAAAACAAAAAQAAAAAAAAAAAAEABkAAPpIawsAAAAYZmRBVAAAAAJ4nGNszqtmwAaYsIqSJQEAjVQBdH0SMnoAAAAASUVORK5CYII=",
    "base64",
  ),
};
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

function studentId(index: number) {
  return index.toString(16).padStart(43, "a");
}

function cardFixture(index: number, count: number): GalleryCollectionCard {
  const selected = index === count - 1;
  return {
    cardId: index.toString(16).padStart(80, "g"),
    postedAt: new Date(Date.UTC(2026, 10, 14) - index * 60_000).toISOString(),
    performer: "participant-a",
    studentId: studentId(index),
    studentName: `生徒${index + 1}`,
    schoolName: selected ? "ゲヘナ学園" : "アビドス高等学校",
    rarity: selected ? 4 : index % 5 === 0 ? null : ([0, 1, 2, 3] as const)[index % 4]!,
    collector: selected ? collectors.second : collectors.first,
    isMine: selected,
    thumbnailReady: true,
  };
}

function rosterEntry(card: GalleryCollectionCard): GalleryRosterEntry {
  const rarity = card.rarity ?? 1;
  const secret = rarity === 0 || rarity === 4;
  return {
    studentId: card.studentId,
    studentName: secret ? null : card.studentName,
    schoolName: secret ? null : card.schoolName,
    rarity,
  };
}

async function mockGallery(
  page: Page,
  count: number,
  options: {
    readonly waitForSecondPage?: Promise<void>;
    readonly expireFirstOriginal?: boolean;
    readonly animatedCards?: readonly string[];
    readonly animationFormat?: keyof typeof animatedImages;
    readonly waitForAnimation?: Promise<void>;
    readonly waitForAnimationTicket?: Promise<void>;
    readonly cards?: () => readonly GalleryCollectionCard[];
    readonly roster?: readonly GalleryRosterEntry[];
  } = {},
) {
  const cards = Array.from({ length: count }, (_, index) => cardFixture(index, count));
  const currentCards = () => options.cards?.() ?? cards;
  const animationFormat = options.animationFormat ?? "gif";
  const requests = {
    pages: [] as number[],
    details: [] as string[],
    originals: [] as string[],
    png: [] as string[],
    animations: [] as string[],
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
      } else if (url.pathname === `${endpoint}/roster`) {
        await route.fulfill({
          headers: privateCache,
          json: { schemaVersion: 1, items: options.roster ?? cards.map(rosterEntry) },
        });
      } else if (url.pathname === endpoint) {
        expect(url.searchParams.get("contract")).toBe("gallery-collection-v1");
        const start = Number(url.searchParams.get("cursor") ?? "0");
        requests.pages.push(start);
        if (start === 100) await options.waitForSecondPage;
        const visibleCards = currentCards();
        await route.fulfill({
          headers: privateCache,
          json: {
            schemaVersion: 1,
            contract: "gallery-collection-v1",
            items: visibleCards.slice(start, start + 100),
            nextCursor: start + 100 < visibleCards.length ? String(start + 100) : null,
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
        const collectionCard = currentCards().find((item) => item.cardId === id);
        if (!id || !collectionCard) {
          await route.fulfill({
            status: 404,
            json: { error: { code: "CARD_NOT_FOUND", message: "Not found", requestId: "fixture" } },
          });
        } else if (variant === "original") {
          requests.originals.push(id);
          const ticket = (tickets.get(id) ?? 0) + 1;
          tickets.set(id, ticket);
          const format = options.animatedCards?.includes(id) ? animationFormat : "png";
          if (format !== "png") await options.waitForAnimationTicket;
          await route.fulfill({
            headers: privateCache,
            json: {
              schemaVersion: 1,
              url: `${media}/originals/${id}.${format}?ticket=${ticket}`,
              expiresAt: expiresAt(),
            },
          });
        } else {
          requests.details.push(id);
          const { studentId: _studentId, ...card } = collectionCard;
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
  await page.route(`${media}/originals/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith(`.${animationFormat}`)) {
      requests.animations.push(`${url.pathname}${url.search}`);
      await options.waitForAnimation;
      return route.fulfill({
        body: animatedImages[animationFormat],
        contentType: `image/${animationFormat}`,
        headers: privateCache,
      });
    }
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

test("loads every card page before displaying a virtualized collection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const remainingPages = deferred();
  const { cards, requests } = await mockGallery(page, 1_000, {
    waitForSecondPage: remainingPages.promise,
  });
  try {
    await page.goto("/gallery");
    await expect(page.getByRole("status")).toHaveText("図鑑を読み込み中…");
    await expect(page.getByRole("list", { name: "生徒図鑑" })).toHaveCount(0);
    expect(requests.originals).toEqual([]);
    expect(requests.png).toEqual([]);
  } finally {
    remainingPages.release();
  }

  await expect(page.getByRole("status")).toHaveText("獲得済み 1,000 / 1,000体");
  expect(requests.pages).toEqual([0, 100, 200, 300, 400, 500, 600, 700, 800, 900]);
  const gallery = page.getByRole("list", { name: "生徒図鑑" });
  await expect(gallery.getByRole("listitem").first()).toHaveAttribute("aria-setsize", "1000");
  expect(await gallery.getByRole("listitem").count()).toBeLessThan(50);
  await expect.poll(() => requests.thumbnails.length).toBeGreaterThan(0);
  expect(requests.thumbnails.length).toBeLessThan(50);
  const positions = await gallery
    .getByRole("listitem")
    .evaluateAll((items) => items.map((item) => Number(item.getAttribute("aria-posinset"))));
  const position = positions.find((current, index) => positions[index + 1] !== current + 1)!;
  const boundary = gallery.locator(`[aria-posinset="${position}"]`);
  await boundary.getByRole("link").focus();
  await expect(gallery.locator(`[aria-posinset="${position}"]`).getByRole("link")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(
    gallery.locator(`[aria-posinset="${position + 1}"]`).getByRole("link"),
  ).toBeFocused();
  await page.evaluate(() => window.scrollTo(0, 0));

  await page.getByRole("combobox", { name: "並べ替え" }).click();
  await page.getByRole("option", { name: "レア度が高い順" }).click();
  const lastPageCard = gallery.getByRole("link", { name: /生徒1000、星4つ/u });
  await expect(lastPageCard).toBeVisible();
  await expect(lastPageCard.getByText("★★★★", { exact: true })).toBeVisible();
  await expect(lastPageCard).toHaveAttribute("href", new RegExp(cards[999]!.cardId));

  expect(requests.originals).toEqual([]);
  expect(requests.png).toEqual([]);
});

test("filters the full roster and sorts by rarity and first acquisition", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const first = {
    ...cardFixture(4, 10),
    studentName: "獲得済み二つ星",
    rarity: 2,
  } as const;
  const duplicate = {
    ...cardFixture(0, 10),
    studentId: first.studentId,
    studentName: first.studentName,
    rarity: first.rarity,
  } as const;
  const recent = {
    ...cardFixture(1, 10),
    studentName: "獲得済み三つ星",
    rarity: 3,
  } as const;
  const locked = {
    ...cardFixture(2, 10),
    studentName: "未入手一つ星",
    schoolName: "トリニティ総合学園",
    rarity: 1,
  } as const;
  const secret = {
    ...cardFixture(3, 10),
    studentName: "ひみつ四つ星",
    schoolName: "秘匿学園",
    rarity: 4,
  } as const;
  const starless = { ...cardFixture(5, 10), rarity: 0 } as const;
  await mockGallery(page, 0, {
    cards: () => [duplicate, recent, first],
    roster: [starless, recent, first, secret, locked].map(rosterEntry),
  });
  await page.goto("/gallery");

  const progress = page.getByRole("status").filter({ hasText: "獲得済み" });
  await expect(progress).toHaveText("獲得済み 2 / 5体");
  await expect(page.getByRole("link", { name: "獲得カード", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "図鑑を更新" })).toHaveCount(0);
  const list = page.getByRole("list", { name: "生徒図鑑" });
  const slots = list.getByRole("listitem");
  await expect(slots).toHaveCount(5);
  await expect(slots.nth(0)).toContainText(locked.studentName);
  await expect(slots.nth(1)).toContainText(first.studentName);
  await expect(slots.nth(2)).toContainText(recent.studentName);
  await expect(slots.nth(3)).toHaveAccessibleName("？？？？？、星4つ、未入手");
  await expect(slots.nth(4)).toHaveAccessibleName("？？？？？、星0つ、未入手");

  const acquiredOnly = page.getByRole("checkbox", { name: "獲得済みのみ表示", exact: true });
  await expect(acquiredOnly).not.toBeChecked();
  await acquiredOnly.check();
  await expect(page).toHaveURL((url) => url.searchParams.get("acquired") === "1");
  await expect(slots).toHaveCount(2);
  await expect(slots.nth(0)).toContainText(first.studentName);
  await expect(slots.nth(1)).toContainText(recent.studentName);
  await acquiredOnly.uncheck();
  await expect(page).toHaveURL((url) => !url.searchParams.has("acquired"));
  await expect(slots).toHaveCount(5);

  const sort = page.getByRole("combobox", { name: "並べ替え" });
  await sort.click();
  await page.getByRole("option", { name: "初回獲得日が新しい順" }).click();
  await expect(slots.nth(0)).toContainText(recent.studentName);
  await expect(slots.nth(1)).toContainText(first.studentName);
  for (const index of [2, 3, 4]) {
    await expect(slots.nth(index)).toHaveAccessibleName(/未入手$/u);
  }
  await sort.click();
  await page.getByRole("option", { name: "初回獲得日が古い順" }).click();
  await expect(slots.nth(0)).toContainText(first.studentName);
  await expect(slots.nth(1)).toContainText(recent.studentName);
  for (const index of [2, 3, 4]) {
    await expect(slots.nth(index)).toHaveAccessibleName(/未入手$/u);
  }

  const studentSearch = page.getByRole("searchbox", { name: "生徒名" });
  await studentSearch.fill("一つ星");
  await expect(slots).toHaveCount(1);
  await expect(slots.first()).toContainText(locked.studentName);
  await studentSearch.fill(secret.studentName);
  await expect(slots).toHaveCount(0);
  await studentSearch.clear();

  const school = page.getByRole("combobox", { name: "学校" });
  await school.click();
  await expect(page.getByRole("option", { name: secret.schoolName })).toHaveCount(0);
  await page.getByRole("option", { name: locked.schoolName }).click();
  await expect(slots).toHaveCount(1);
  await expect(slots.first()).toContainText(locked.studentName);
  await acquiredOnly.check();
  await expect(page).toHaveURL(
    (url) =>
      url.searchParams.get("school") === locked.schoolName &&
      url.searchParams.get("acquired") === "1",
  );
  await expect(slots).toHaveCount(0);
  await page.getByRole("button", { name: "絞り込みを解除" }).click();
  await expect(acquiredOnly).not.toBeChecked();
  await expect(slots).toHaveCount(5);

  await page.getByRole("combobox", { name: "レア度" }).click();
  await page.getByRole("option", { name: "星4つ" }).click();
  await expect(slots).toHaveCount(1);
  await expect(slots.first()).toHaveAccessibleName("？？？？？、星4つ、未入手");
  await expect(progress).toHaveText("獲得済み 2 / 5体");
});

test("commits a Japanese IME search after composition ends", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await mockGallery(page, 0, {
    roster: [
      { studentId: studentId(0), studentName: "プラナ", schoolName: "シッテムの箱", rarity: 1 },
      { studentId: studentId(1), studentName: "アロナ", schoolName: "シッテムの箱", rarity: 1 },
    ],
  });
  await page.goto("/gallery");
  const search = page.getByRole("searchbox", { name: "生徒名" });
  const entries = page.getByRole("list", { name: "生徒図鑑" }).getByRole("listitem");
  await expect(entries).toHaveCount(2);
  await search.focus();

  const cdp = await page.context().newCDPSession(page);
  for (const text of ["ら", "らな", "ラナ"]) {
    await cdp.send("Input.imeSetComposition", {
      text,
      selectionStart: text.length,
      selectionEnd: text.length,
    });
    await expect(search).toHaveValue(text);
    await expect(page).toHaveURL(/\/gallery$/u);
    await expect(entries).toHaveCount(2);
  }

  await cdp.send("Input.insertText", { text: "ラナ" });
  await expect(search).toHaveValue("ラナ");
  await expect(search).toBeFocused();
  await expect(page).toHaveURL((url) => url.searchParams.get("q") === "ラナ");
  await expect(entries).toHaveCount(1);
  await expect(entries.first()).toContainText("プラナ");
});

test("leaves PNG media unloaded on hover and restores focus and scroll after viewing", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const { cards, requests } = await mockGallery(page, 48);
  await page.goto("/gallery?sort=newest");
  await expect(page.getByRole("status")).toHaveText("獲得済み 48 / 48体");
  const gallery = page.getByRole("list", { name: "生徒図鑑" });
  await gallery.scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, 1100));
  const card = cards[12]!;
  const path = `/gallery/${card.cardId}?sort=newest`;
  const trigger = gallery.locator(`a[href="${path}"]`);
  await trigger.hover();
  const selected = card.cardId;
  await expect.poll(() => requests.details).toContain(selected);
  await expect.poll(() => requests.originals).toEqual([selected]);
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
  await expect(page).toHaveURL(/\/gallery\?sort=newest$/u);
  await expect(gallery.locator(`a[href="${path}"]`)).toBeFocused();
  await expect
    .poll(async () => Math.abs((await page.evaluate(() => window.scrollY)) - beforeScroll))
    .toBeLessThan(3);
});

for (const animationFormat of ["gif", "apng"] as const) {
  test(`previews only the hovered ${animationFormat} and keeps its thumbnail until the animation loads`, async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium");
    const first = cardFixture(0, 3);
    const waitingAnimation = deferred();
    const { requests } = await mockGallery(page, 3, {
      animatedCards: [first.cardId, cardFixture(1, 3).cardId],
      animationFormat,
      waitForAnimation: waitingAnimation.promise,
    });
    await page.goto("/gallery");
    const card = page.getByRole("link", { name: "生徒1、星1つを拡大" });
    const thumbnail = card.locator('img[src*="/thumbnails/"]');
    const animation = card.locator(`img[src*=".${animationFormat}?"]`);
    const animationRequest = `/originals/${first.cardId}.${animationFormat}?ticket=1`;
    try {
      await card.focus();
      await expect.poll(() => requests.details).toContain(first.cardId);
      expect(requests.originals).toEqual([]);
      await card.hover();
      await expect.poll(() => requests.animations).toEqual([animationRequest]);
      await expect(thumbnail).toBeVisible();
      await expect(animation).toHaveCSS("opacity", "0");
    } finally {
      waitingAnimation.release();
    }
    await expect(animation).toHaveCSS("opacity", "1");
    await page.mouse.move(0, 0);
    await expect(animation).toHaveCount(0);
    await expect(thumbnail).toBeVisible();
    await card.hover();
    await expect(animation).toHaveCSS("opacity", "1");
    await card.click();
    const dialog = page.getByRole("dialog", { name: "アロナ（生徒1）" });
    await expect(dialog.getByRole("img", { name: "アロナ（生徒1）" })).toHaveAttribute(
      "src",
      `${media}${animationRequest}`,
    );
    await expect(animation).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: "アニメーションを一時停止" })).toBeVisible();
    expect(requests.originals).toEqual([first.cardId]);
  });
}

test("drops a late GIF ticket when the pointer leaves its collection cover", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const newer = cardFixture(0, 2);
  const cover = { ...newer, cardId: cardFixture(1, 2).cardId, postedAt: "2026-11-13T00:00:00Z" };
  const waitingTicket = deferred();
  const { requests } = await mockGallery(page, 2, {
    cards: () => [newer, cover],
    roster: [rosterEntry(cover)],
    animatedCards: [cover.cardId],
    waitForAnimationTicket: waitingTicket.promise,
  });
  await page.goto("/gallery");
  const group = page.getByRole("link", { name: "生徒1、星1つ、2枚のカードを見る" });
  try {
    await group.hover();
    await expect.poll(() => requests.originals).toEqual([cover.cardId]);
    await page.mouse.move(0, 0);
  } finally {
    waitingTicket.release();
  }
  await expect(group.locator('img[src*="/thumbnails/"]')).toBeVisible();
  await expect(group.locator('img[src*=".gif?"]')).toHaveCount(0);
  expect(requests.animations).toEqual([]);
});

test("keeps the thumbnail after GIF failure and skips previews with Save Data", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  const first = cardFixture(0, 3);
  const second = cardFixture(1, 3);
  const { requests } = await mockGallery(page, 3, { animatedCards: [first.cardId, second.cardId] });
  const failedImage = page.route(`${media}/originals/${first.cardId}.gif?*`, (route) =>
    route.fulfill({ status: 403, headers: privateCache }),
  );
  await failedImage;
  await page.goto("/gallery");
  const firstLink = page.getByRole("link", { name: "生徒1、星1つを拡大" });
  const failedResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/originals/${first.cardId}.gif`,
  );
  await firstLink.hover();
  expect((await failedResponse).status()).toBe(403);
  await expect(firstLink.locator('img[src*=".gif?"]')).toHaveCount(0);
  await expect(firstLink.locator('img[src*="/thumbnails/"]')).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(navigator, "connection", { value: { saveData: true } });
  });
  await page.getByRole("link", { name: "生徒2、星1つを拡大" }).hover();
  // Let the hover debounce elapse before asserting the absence of a second request.
  await page.waitForTimeout(170);
  expect(requests.originals).toEqual([first.cardId]);
});

for (const animationFormat of ["gif", "apng"] as const) {
  test(`starts a ${animationFormat} paused with reduced motion and plays only on request`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const selected = cardFixture(0, 2);
    const { requests } = await mockGallery(page, 2, {
      animatedCards: [selected.cardId],
      animationFormat,
    });
    await page.goto("/gallery");
    const card = page.getByRole("link", { name: "生徒1、星1つを拡大" });
    await card.hover();
    await card.focus();
    await expect.poll(() => requests.details).toContain(selected.cardId);
    expect(requests.originals).toEqual([]);
    await card.click();
    const dialog = page.getByRole("dialog", { name: "アロナ（生徒1）" });
    const image = dialog.getByRole("img", { name: "アロナ（生徒1）" });
    const thumbnailUrl = `${media}/thumbnails/${selected.cardId}.webp`;
    await expect(image).toHaveAttribute("src", thumbnailUrl);
    expect(requests.animations).toEqual([]);
    await dialog.getByRole("button", { name: "アニメーションを再生" }).click();
    const animationRequest = `/originals/${selected.cardId}.${animationFormat}?ticket=1`;
    await expect(image).toHaveAttribute("src", `${media}${animationRequest}`);
    await expect.poll(() => requests.animations).toEqual([animationRequest]);
    await dialog.getByRole("button", { name: "アニメーションを一時停止" }).click();
    await expect(image).toHaveAttribute("src", thumbnailUrl);
    await expect(dialog.getByRole("button", { name: "アニメーションを再生" })).toBeVisible();
    expect(requests.originals).toEqual([selected.cardId]);
  });
}

test("supports dark reduced-motion mobile viewing and refreshes an expired PNG URL", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  const { cards, requests } = await mockGallery(page, 3, { expireFirstOriginal: true });
  await page.goto("/gallery?sort=newest");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const schoolFilter = page.getByRole("combobox", { name: "学校", exact: true });
  await schoolFilter.tap();
  const schoolLogoChip = page
    .getByRole("option", { name: "ゲヘナ学園" })
    .locator("span:has(> img)");
  await expect(schoolLogoChip).toHaveCSS("background-color", "rgb(245, 251, 255)");
  await page.keyboard.press("Escape");
  const firstCard = page.getByRole("link", { name: "生徒1、星1つを拡大" });
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
  await page.goto("/gallery?sort=newest");
  await page.getByRole("link", { name: "生徒2、星1つを拡大" }).click();

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

test("reveals regular and secret roster slots after acquisition", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const regular = {
    ...cardFixture(1, 42),
    studentName: "通常生徒",
    rarity: 3,
  } as const;
  const starless = {
    ...cardFixture(2, 42),
    studentName: "星なしの生徒",
    rarity: 0,
  } as const;
  const fourStar = {
    ...cardFixture(3, 42),
    studentName: "星4の生徒",
    rarity: 4,
  } as const;
  let acquired: readonly GalleryCollectionCard[] = [];
  const roster = [regular, starless, fourStar].map(rosterEntry);
  const { requests } = await mockGallery(page, 0, { cards: () => acquired, roster });

  await page.goto("/gallery");
  await expect(page).toHaveTitle(/生徒図鑑/u);
  await expect(page.getByRole("heading", { name: "生徒図鑑" })).toBeVisible();
  await expect(page.getByRole("link", { name: "生徒図鑑", exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toHaveText("獲得済み 0 / 3体");
  const collection = page.getByRole("list", { name: "生徒図鑑" });
  const regularSlot = collection.getByRole("listitem", { name: "通常生徒、星3つ、未入手" });
  await expect(regularSlot.getByText("？", { exact: true })).toBeVisible();
  await expect(collection.getByText("？？？？？", { exact: true })).toHaveCount(2);
  await expect(collection.getByText(starless.studentName)).toHaveCount(0);
  await expect(collection.getByText(fourStar.studentName)).toHaveCount(0);
  expect(requests.thumbnails).toEqual([]);

  acquired = [regular, starless, fourStar];
  await page.reload();
  await expect(page.getByRole("status")).toHaveText("獲得済み 3 / 3体");
  await expect(collection.getByText("？", { exact: true })).toHaveCount(0);
  await expect(collection.getByText("？？？？？", { exact: true })).toHaveCount(0);
  const revealed = collection.getByRole("link", { name: "星なしの生徒、星0つを拡大" });
  await expect(revealed).toBeVisible();
  await expect(collection.getByRole("link", { name: "星4の生徒、星4つを拡大" })).toBeVisible();
  await revealed.click();
  await expect(page.getByRole("dialog", { name: "アロナ（星なしの生徒）" })).toBeVisible();
  await page.getByRole("button", { name: "拡大表示を閉じる" }).click();
  await expect(revealed).toBeFocused();
});

test("uses the first acquisition across pages and restores position after browsing duplicates", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const totalCards = 102;
  const cards = Array.from({ length: totalCards }, (_, index) => cardFixture(index, totalCards));
  const named = (index: number, performer: GalleryCollectionCard["performer"]) =>
    ({
      ...cardFixture(index, totalCards),
      studentId: studentId(500),
      studentName: "同じ生徒（衣装）",
      schoolName: "アビドス高等学校",
      rarity: 1,
      performer,
    }) as const;
  cards[0] = named(0, "participant-a");
  cards[100] = named(100, "participant-b");
  const first = named(101, "participant-c");
  cards[101] = first;
  const ordinary = cards[1]!;
  const locked: GalleryRosterEntry[] = Array.from({ length: 60 }, (_, index) => ({
    studentId: studentId(index + 200),
    studentName: `未入手の生徒${index + 1}`,
    schoolName: "アビドス高等学校",
    rarity: 1,
  }));
  const roster: GalleryRosterEntry[] = [
    ...locked.slice(0, 20),
    rosterEntry(ordinary),
    ...locked.slice(20),
    rosterEntry(first),
  ];
  const catalogParams = new URLSearchParams({ school: first.schoolName });
  const laterPage = deferred();
  const { requests } = await mockGallery(page, 0, {
    cards: () => cards,
    roster,
    waitForSecondPage: laterPage.promise,
  });
  try {
    await page.goto(`/gallery?${catalogParams}`);
    await expect(page.getByRole("status")).toHaveText("図鑑を読み込み中…");
    await expect(page.getByRole("list", { name: "生徒図鑑" })).toHaveCount(0);
  } finally {
    laterPage.release();
  }
  await expect(page.getByRole("status")).toHaveText("獲得済み 2 / 62体");
  const collection = page.getByRole("list", { name: "生徒図鑑" });
  const slot = collection.getByRole("link", { name: "同じ生徒（衣装）、星1つ、3枚のカードを見る" });
  await page.getByRole("button", { name: "絞り込みを解除" }).focus();
  await page.keyboard.press("Tab");
  await expect(collection.getByRole("link", { name: "生徒2、星1つを拡大" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(slot).toBeFocused();
  await expect(slot.getByText("3枚", { exact: true })).toBeVisible();
  await expect(slot.locator("img").first()).toHaveAttribute(
    "src",
    `${media}/thumbnails/${first.cardId}.webp`,
  );
  await expect.poll(() => requests.pages).toEqual([0, 100]);
  expect(requests.originals).toEqual([]);
  const beforeScroll = await page.evaluate(() => window.scrollY);
  expect(beforeScroll).toBeGreaterThan(0);
  await page.keyboard.press("Enter");

  const group = page.getByRole("list", { name: "同じ生徒（衣装）の獲得カード" });
  await expect(page).toHaveURL(
    (url) =>
      url.searchParams.get("student") === first.studentId &&
      url.searchParams.get("school") === first.schoolName,
  );
  await expect(page.getByRole("heading", { name: first.studentName })).toBeFocused();
  const groupLinks = group.getByRole("link");
  await expect(groupLinks).toHaveCount(3);
  await expect(groupLinks.first().getByText("初回獲得", { exact: true })).toBeVisible();
  await expect(groupLinks.first()).toHaveAccessibleName(/安倍晋三/u);
  await expect(groupLinks.last()).toHaveAccessibleName(/アロナ/u);
  await groupLinks.first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveAccessibleName("安倍晋三（同じ生徒）（衣装）");
  await dialog.getByRole("button", { name: "次のカード" }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).toHaveAccessibleName("プラナ（同じ生徒）（衣装）");
  await page.keyboard.press("Escape");
  await expect(groupLinks.first()).toBeFocused();
  await page.getByRole("button", { name: "図鑑に戻る" }).click();
  await expect(page).toHaveURL((url) => url.search === `?${catalogParams}`);
  await expect(slot).toBeFocused();
  await expect
    .poll(async () => Math.abs((await page.evaluate(() => window.scrollY)) - beforeScroll))
    .toBeLessThan(3);

  const acquiredOnly = page.getByRole("checkbox", { name: "獲得済みのみ表示" });
  await acquiredOnly.check();
  await slot.click();
  await expect(page).toHaveURL(
    (url) =>
      url.searchParams.get("student") === first.studentId &&
      url.searchParams.get("acquired") === "1",
  );
  await page.getByRole("button", { name: "図鑑に戻る" }).click();
  await expect(acquiredOnly).toBeChecked();
});

test("opens a legacy card deep link and returns to the filtered collection", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const { cards } = await mockGallery(page, 3);
  await page.goto(`/gallery/${cards[2]!.cardId}?rarity=4`);
  const dialog = page.getByRole("dialog", { name: "アロナ（生徒3）" });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page).toHaveURL(/\/gallery\?rarity=4$/u);
  await expect(page.getByRole("status")).toHaveText("獲得済み 3 / 3体");
  const cardsInCollection = page.getByRole("list", { name: "生徒図鑑" }).getByRole("link");
  await expect(cardsInCollection).toHaveCount(1);
  await expect(cardsInCollection.first()).toHaveAccessibleName("生徒3、星4つを拡大");
});

test("retries the roster after a temporary error", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await mockGallery(page, 1);
  await page.route(
    `**${endpoint}/roster`,
    (route) =>
      route.fulfill({
        status: 503,
        json: {
          error: {
            code: "GALLERY_UNAVAILABLE",
            message: "Unavailable",
            requestId: "roster-fixture",
          },
        },
      }),
    { times: 1 },
  );
  await page.goto("/gallery");
  await expect(page.getByRole("alert")).toContainText("生徒の名簿を読み込めませんでした。");
  await expect(page.getByRole("list", { name: "生徒図鑑" })).toHaveCount(0);
  await page.getByRole("button", { name: "もう一度試す" }).click();
  await expect(page.getByRole("status")).toHaveText("獲得済み 1 / 1体");
});
