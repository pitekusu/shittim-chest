import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const RECORD_ID = "r".repeat(43);

const placeholder = (displayName: string, fallbackVariant: string) => ({
  kind: "placeholder",
  url: null,
  alt: `${displayName}のアバター`,
  fallbackVariant,
});

const participants = [
  ["participant-a", "アロナ", "cyan"],
  ["participant-b", "プラナ", "pink"],
  ["participant-c", "安倍晋三AI", "lavender"],
].map(([slot, displayName, fallbackVariant]) => ({
  slot,
  displayName,
  avatar: placeholder(displayName, fallbackVariant),
}));

const detail = {
  schemaVersion: 1,
  recordId: RECORD_ID,
  completedAt: "2026-08-15T06:00:00Z",
  question: "休日に家で過ごすなら、映画を見るかゲームをするか。それぞれの価値観から話し合う",
  requester: { displayName: "パワー系ウナギ", avatar: placeholder("依頼者", "cyan") },
  participants,
  initialOpinions: participants.map(({ slot }, index) => ({
    participant: slot,
    summary: ["物語へ集中する", "一緒に遊べる", "翌日の疲労を抑える"][index],
    proposal: [
      "映画を一本じっくり観て、物語の余韻まで落ち着いて楽しみます。",
      "協力ゲームを二時間ほど遊び、全員で同じ達成感を共有します。",
      "短編映画を選び、翌日の予定に疲れを残さない範囲で楽しみます。",
    ][index],
  })),
  finalProposals: participants.map(({ slot }, index) => ({
    participant: slot,
    title: ["映画で整える夜", "ゲームで盛り上がる夜", "余白を残す映画案"][index],
    proposal: [
      "映画を観てから印象に残った場面を振り返り、感想をゆっくり話します。",
      "協力ゲームを二時間遊び、途中で休憩を入れながら全員で最後まで進めます。",
      "短編映画を一本選び、見終わった後に余裕を持って早めに休みます。",
    ][index],
  })),
  votes: [
    { voter: "participant-a", candidate: "participant-b", reason: "皆で参加できるため" },
    { voter: "participant-b", candidate: "participant-a", reason: "落ち着いて楽しめるため" },
    { voter: "participant-c", candidate: "participant-a", reason: "明日の負担が少ないため" },
  ],
  result: {
    winner: "participant-a",
    voteCounts: [
      { participant: "participant-a", count: 2 },
      { participant: "participant-b", count: 1 },
      { participant: "participant-c", count: 0 },
    ],
    tieBreakApplied: false,
  },
  finalDecision: {
    winner: "participant-a",
    victoryMessage: "みんなの一票が本当にうれしいです！",
    decision:
      "今夜は二時間以内の映画を一本観て、見終わった後に印象に残った場面や感想をゆっくり話し合います。",
    actions: ["好みの飲み物を用意する", "全員が楽しめる二時間以内の映画を選ぶ"],
    caveats: ["翌日の予定に疲れを残さない時間までに終える"],
  },
};

const PRODUCTION_CSP = [
  "default-src 'self'",
  "base-uri 'self'",
  "connect-src 'self'",
  "font-src 'self'",
  "img-src 'self' data:",
  "object-src 'none'",
  "frame-ancestors 'none'",
  "script-src 'self'",
  "style-src 'self'",
].join("; ");

async function mockAuthenticatedApi(page: Page): Promise<void> {
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: true,
        user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
        csrfToken: "csrf-token",
      },
    }),
  );
  await page.route("**/api/v1/records?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        items: [
          {
            schemaVersion: 1,
            recordId: detail.recordId,
            completedAt: detail.completedAt,
            questionPreview: detail.question,
            requester: detail.requester,
            participants: detail.participants,
            result: detail.result,
          },
        ],
        nextCursor: null,
      },
    }),
  );
  await page.route(`**/api/v1/records/${RECORD_ID}`, (route) => route.fulfill({ json: detail }));
}

test("authenticated member can browse the completed archive", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "議論の記録" })).toBeVisible();
  const card = page.getByRole("article");
  await expect(card).toContainText(detail.question);
  await expect(card.getByText("2026年8月15日 15:00")).toHaveAttribute(
    "datetime",
    detail.completedAt,
  );
  await expect(page.getByLabel("依頼者")).toHaveValue("");
  await expect(
    page.getByLabel("依頼者").getByRole("option", { name: "パワー系ウナギ" }),
  ).toHaveCount(1);
  await page.getByLabel("依頼者").selectOption("パワー系ウナギ");
  await expect(card).toContainText(detail.question);
  await expect(page.getByLabel("並び順")).toHaveValue("newest");
  await expect(page.getByLabel("開始日")).toHaveCount(0);
  await expect(page.getByLabel("終了日")).toHaveCount(0);
  const typographySupport = await page.evaluate(() => ({
    autoPhrase: CSS.supports("word-break", "auto-phrase"),
    autospace: CSS.supports("text-autospace", "normal"),
    language: document.documentElement.lang,
  }));
  expect(typographySupport).toEqual({ autoPhrase: true, autospace: true, language: "ja" });
  const questionTypography = await card.getByRole("heading").evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      lineBreak: style.getPropertyValue("line-break"),
      overflowWrap: style.overflowWrap,
      textAutospace: style.getPropertyValue("text-autospace"),
      wordBreak: style.wordBreak,
    };
  });
  expect(questionTypography).toEqual({
    lineBreak: "strict",
    overflowWrap: "anywhere",
    textAutospace: "normal",
    wordBreak: "auto-phrase",
  });
  await expect(page).toHaveScreenshot("records-home.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
  await page.getByRole("link", { name: "記録を読む" }).click();
  await expect(page.getByRole("heading", { name: detail.question })).toBeVisible();
  await expect(page.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "最終決定" })).toBeVisible();
  await expect(page.getByText(detail.finalDecision.victoryMessage)).toBeVisible();
  await expect(page.getByText("2026年8月15日 15:00")).toHaveAttribute(
    "datetime",
    detail.completedAt,
  );
  await expect(page.getByText(/所要時間|Evidence|外部根拠/)).toHaveCount(0);
  await expect(page.getByText(detail.finalDecision.decision)).toHaveCSS("text-wrap", "pretty");

  await expect(page).toHaveScreenshot("records-detail.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("product name keeps the approved two-line break at narrow widths", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: { schemaVersion: 1, authenticated: false, user: null, csrfToken: null },
    }),
  );
  await page.goto("/");

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 808, height: 730 },
  ]) {
    await page.setViewportSize(viewport);
    const heading = page.getByRole("heading", { name: "シッテムの箱 議事録" });
    await expect(heading).toBeVisible();
    const lines = await heading.locator("span").evaluateAll((elements) =>
      elements.map((element) => {
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return { display: style.display, top: bounds.top, whiteSpace: style.whiteSpace };
      }),
    );
    expect(lines).toHaveLength(2);
    expect(lines[0]?.display).toBe("block");
    expect(lines[0]?.whiteSpace).toBe("nowrap");
    expect(lines[1]!.top).toBeGreaterThan(lines[0]!.top);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
  }
});

test("reduced motion skips the long login transition", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockAuthenticatedApi(page);
  await page.addInitScript(() => sessionStorage.setItem("records-login-transition", "pending"));
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "議論の記録" })).toBeVisible({
    timeout: 1_000,
  });
});

test("anonymous login page boots under the production CSP without dynamic evaluation", async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/session") {
      await route.fulfill({
        json: { schemaVersion: 1, authenticated: false, user: null, csrfToken: null },
      });
      return;
    }
    const response = await route.fetch();
    await route.fulfill({
      response,
      headers: { ...response.headers(), "content-security-policy": PRODUCTION_CSP },
    });
  });

  await page.goto("/");

  await expect(page.getByRole("link", { name: "Discordでログイン" })).toBeVisible();
  await page.evaluate(() => document.fonts.ready);
  expect(pageErrors).toEqual([]);
});
