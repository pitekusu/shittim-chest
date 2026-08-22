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

const rankings = {
  schemaVersion: 1,
  wins: [
    { rank: 1, displayName: "アロナ", avatar: participants[0]!.avatar, count: 20 },
    { rank: 2, displayName: "プラナ", avatar: participants[1]!.avatar, count: 18 },
    { rank: 3, displayName: "安倍晋三AI", avatar: participants[2]!.avatar, count: 16 },
  ],
  requests: [
    {
      rank: 1,
      displayName: "パワー系ウナギ",
      avatar: placeholder("パワー系ウナギ", "cyan"),
      count: 12,
    },
    {
      rank: 1,
      displayName: "吹雪型JC",
      avatar: placeholder("吹雪型JC", "pink"),
      count: 12,
    },
    { rank: 3, displayName: "先生", avatar: placeholder("先生", "lavender"), count: 8 },
  ],
  generatedAt: "2026-08-22T00:00:00Z",
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

async function mockAuthenticatedApi(page: Page, recordDetail = detail): Promise<void> {
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
            recordId: recordDetail.recordId,
            completedAt: recordDetail.completedAt,
            questionPreview: recordDetail.question,
            requester: recordDetail.requester,
            participants: recordDetail.participants,
            result: recordDetail.result,
          },
        ],
        nextCursor: null,
      },
    }),
  );
  await page.route(`**/api/v1/records/${RECORD_ID}`, (route) =>
    route.fulfill({ json: recordDetail }),
  );
  await page.route("**/api/v1/insights/rankings", (route) => route.fulfill({ json: rankings }));
}

test("authenticated member can browse the completed archive", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/");

  const recordsHeading = page.getByRole("heading", { name: "議論の記録" });
  await expect(recordsHeading).toBeVisible();
  expect(
    await recordsHeading.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    ),
  ).toBeLessThanOrEqual(45);
  const logoff = page.getByRole("button", { name: "LOGOFF" });
  await expect(logoff).toHaveCSS("font-family", /Delogy/);
  await expect(logoff).toHaveCSS("border-top-style", "solid");
  await expect(logoff).toHaveAttribute("lang", "en");
  const card = page.getByRole("article");
  await expect(card).toContainText(detail.question);
  await expect(card.getByText("2026年8月15日 15:00")).toHaveAttribute(
    "datetime",
    detail.completedAt,
  );
  await expect(page.getByRole("button", { name: "依頼者" })).toContainText("すべて");
  await page.getByRole("button", { name: "依頼者" }).click();
  const requesterOption = page.getByRole("option", { name: "パワー系ウナギ" });
  await expect(requesterOption.locator("[aria-hidden=true]").first()).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await requesterOption.click();
  await expect(card).toContainText(detail.question);
  await page.getByRole("button", { name: "勝者" }).click();
  await expect(
    page.getByRole("option", { name: "アロナ" }).locator("[aria-hidden=true]").first(),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page.locator("body").click({ position: { x: 4, y: 4 } });
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
    // Self-hosted font rasterization differs by a few edge pixels between the
    // pinned local and GitHub-hosted Chromium environments.
    maxDiffPixels: 30,
  });

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

test("authenticated member can review responsive rankings", async ({ page }) => {
  await mockAuthenticatedApi(page);
  await page.goto("/insights");

  const sidebarProductName = page.locator('[aria-label="The Shittim Chest Archive"]');
  await expect(sidebarProductName.locator(":scope > span")).toHaveText([
    "THE SHITTIM",
    "CHEST ARCHIVE",
  ]);
  await expect(sidebarProductName).toHaveCSS("font-family", /Delogy/);
  await expect(sidebarProductName).toHaveAttribute("lang", "en");
  const sidebarType = await sidebarProductName.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      fontSize: Number.parseFloat(style.fontSize),
      lineHeight: Number.parseFloat(style.lineHeight),
    };
  });
  expect(sidebarType.lineHeight / sidebarType.fontSize).toBeGreaterThanOrEqual(1.4);
  expect(
    await sidebarProductName.evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
  await expect(sidebarProductName.locator("..").locator('[aria-hidden="true"]').first()).toHaveCSS(
    "width",
    "44px",
  );
  const insightsHeading = page.getByRole("heading", { name: "いろいろな記録" });
  await expect(insightsHeading).toBeVisible();
  expect(
    await insightsHeading.evaluate((element) =>
      Number.parseFloat(getComputedStyle(element).fontSize),
    ),
  ).toBeLessThanOrEqual(45);
  const wins = page.getByRole("region", { name: "勝利回数ランキング" });
  const requests = page.getByRole("region", { name: "依頼回数ランキング" });
  await expect(wins.getByText("VICTORIES", { exact: true })).toBeVisible();
  await expect(requests.getByText("REQUESTS", { exact: true })).toBeVisible();
  await expect(wins.getByRole("listitem")).toHaveCount(3);
  await expect(wins.getByRole("meter")).toHaveCount(3);
  await expect(
    wins.getByRole("meter", { name: "アロナ: 20回（最多20回との比較）" }),
  ).toHaveAttribute("value", "20");
  await expect(wins.getByRole("status", { name: "勝利回数ランキングの合計" })).toContainText(
    "54回",
  );
  await expect(requests.getByText("1位", { exact: true })).toHaveCount(2);
  await expect(
    requests.getByRole("status", { name: "依頼回数ランキングの上位合計" }),
  ).toContainText("32回");
  await expect(requests.getByRole("meter")).toHaveCount(3);
  await expect(page.getByText("2026年8月22日 09:00")).toBeVisible();
  await expect(page.getByText(/費用|Fargate|OpenAI/)).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await expect(page).toHaveScreenshot("records-insights.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("English login product name keeps the approved two-line break at narrow widths", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: { schemaVersion: 1, authenticated: false, user: null, csrfToken: null },
    }),
  );
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);

  for (const viewport of [
    { width: 320, height: 800 },
    { width: 808, height: 730 },
  ]) {
    await page.setViewportSize(viewport);
    const heading = page.getByRole("heading", { name: "The Shittim Chest Archive" });
    await expect(heading).toBeVisible();
    const lines = await heading.locator("span").evaluateAll((elements) =>
      elements.map((element) => {
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          clientWidth: element.clientWidth,
          display: style.display,
          scrollWidth: element.scrollWidth,
          top: bounds.top,
          whiteSpace: style.whiteSpace,
        };
      }),
    );
    expect(lines).toHaveLength(2);
    expect(lines[0]?.display).toBe("block");
    expect(lines[0]?.whiteSpace).toBe("nowrap");
    expect(lines[1]!.top).toBeGreaterThan(lines[0]!.top);
    expect(
      lines.every((line) => line.scrollWidth <= line.clientWidth),
      JSON.stringify({ viewport, lines }),
    ).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
    await expect(heading).toHaveCSS("text-align", "center");
    const headingType = await heading.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontSize: Number.parseFloat(style.fontSize),
        lineHeight: Number.parseFloat(style.lineHeight),
      };
    });
    expect(headingType.lineHeight / headingType.fontSize).toBeGreaterThanOrEqual(1.2);
    const authenticate = page.getByRole("link", { name: "AUTHENTICATE" });
    const authenticateType = await authenticate.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontSize: Number.parseFloat(style.fontSize),
        paddingInline: Number.parseFloat(style.paddingInlineStart),
      };
    });
    expect(authenticateType.fontSize).toBeLessThanOrEqual(10.6);
    expect(authenticateType.paddingInline).toBeGreaterThanOrEqual(16);
    await expect(page).toHaveScreenshot(`display-font-login-${viewport.width}.png`, {
      animations: "disabled",
      fullPage: true,
      maxDiffPixels: 20,
    });
  }
});

test("loads the next archive page automatically near the end of the loaded cards", async ({
  page,
}) => {
  const firstPage = Array.from({ length: 12 }, (_, index) => ({
    schemaVersion: 1,
    recordId: String.fromCharCode(65 + index).repeat(43),
    completedAt: detail.completedAt,
    questionPreview: `読み込み済みの議論 ${index + 1}`,
    requester: detail.requester,
    participants: detail.participants,
    result: detail.result,
  }));
  const nextRecord = {
    ...firstPage[0]!,
    recordId: "Z".repeat(43),
    questionPreview: "自動で追加された議論",
  };
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
  await page.route("**/api/v1/records?*", (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    return route.fulfill({
      json: {
        schemaVersion: 1,
        items: cursor === "next-page" ? [nextRecord] : firstPage,
        nextCursor: cursor === "next-page" ? null : "next-page",
      },
    });
  });

  await page.goto("/");
  await expect(page.getByText("読み込み済みの議論 12")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));

  await expect(page.getByText("自動で追加された議論")).toBeVisible();
  await expect(page.getByRole("button", { name: "さらに読み込む" })).toHaveCount(0);
});

test("record detail stays inside the mobile viewport with long Japanese content", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  const longText =
    "最初に必要な道具と時間を整理してから小さく試し、途中で休憩を入れながら、参加する全員が無理なく楽しめる進め方を選びます。最後に感想を共有して次回の工夫へつなげます。";
  const longDetail = {
    ...detail,
    question:
      "新しい趣味を始めるなら、庭で植物を育てるか室内で工作を楽しむか、それぞれの価値観から話し合って決める",
    initialOpinions: detail.initialOpinions.map((opinion) => ({
      ...opinion,
      summary: "準備を整えてから全員で無理なく楽しめる方法を選ぶ",
      proposal: longText,
    })),
    finalProposals: detail.finalProposals.map((proposal) => ({
      ...proposal,
      title: "小さく試して長く続ける共同趣味の計画",
      proposal: longText,
    })),
  };
  await mockAuthenticatedApi(page, longDetail);
  await page.goto(`/records/${RECORD_ID}`);
  await expect(page.getByRole("heading", { name: longDetail.question })).toBeVisible();

  const viewport = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(viewport.scrollWidth).toBeLessThanOrEqual(viewport.clientWidth);
});

test("vote graph keeps distinct routes, interactions, and responsive layouts", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mockAuthenticatedApi(page);
  await page.goto(`/records/${RECORD_ID}`);
  await page.evaluate(() => document.fonts.ready);

  const graph = page.getByTestId("vote-graph");
  await graph.scrollIntoViewIfNeeded();
  await expect(graph).toHaveAttribute("data-layout", "wide");
  await expect(graph).toHaveAttribute("data-revealed", "true");
  await expect(graph.getByLabel("アロナがプラナに投票")).toBeVisible();
  await expect(graph.getByLabel("プラナがアロナに投票")).toBeVisible();
  await expect(graph.getByLabel("安倍晋三AIがアロナに投票")).toBeVisible();

  const beforeAnimation = await graph.boundingBox();
  await page.waitForTimeout(1_500);
  const afterAnimation = await graph.boundingBox();
  expect(beforeAnimation).not.toBeNull();
  expect(afterAnimation).not.toBeNull();
  expect(Math.abs(afterAnimation!.width - beforeAnimation!.width)).toBeLessThan(0.5);
  expect(Math.abs(afterAnimation!.height - beforeAnimation!.height)).toBeLessThan(0.5);
  await expect(graph.locator('[data-part="line"]').first()).toHaveCSS("stroke-dasharray", "none");

  await expect(graph).toHaveScreenshot("vote-graph-desktop.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  const aronaNode = graph.getByRole("button", { name: "アロナに関係する投票を強調" });
  await aronaNode.hover();
  await expect(graph.locator('[data-vote-key="participant-a-participant-b"]')).toHaveAttribute(
    "data-relation",
    "outgoing",
  );
  await expect(graph).toHaveScreenshot("vote-graph-desktop-hover.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  await page.setViewportSize({ width: 1024, height: 900 });
  await expect(graph).toHaveAttribute("data-layout", "wide");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1024);

  await page.setViewportSize({ width: 768, height: 900 });
  await expect(graph).toHaveAttribute("data-layout", "compact");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(graph).toHaveAttribute("data-layout", "compact");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await expect(graph).toHaveScreenshot("vote-graph-mobile-390.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });

  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.reload();
  await graph.scrollIntoViewIfNeeded();
  await expect(graph.locator('[data-part="line"]').first()).toHaveCSS("animation-name", "none");
  await expect(graph.locator('[data-part="flow"]').first()).toHaveCSS("display", "none");
  await expect(graph.locator('[data-part="arrow"]').first()).toHaveCSS("opacity", "1");
  await expect(graph).toHaveScreenshot("vote-graph-reduced-motion.png", {
    animations: "disabled",
    maxDiffPixels: 20,
  });
});

test("touch activation keeps a vote selection until the same target is tapped again", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile-chromium");
  await mockAuthenticatedApi(page);
  await page.goto(`/records/${RECORD_ID}`);

  const graph = page.getByTestId("vote-graph");
  await graph.scrollIntoViewIfNeeded();
  const node = graph.getByRole("button", { name: "アロナに関係する投票を強調" });
  const route = graph.getByLabel("アロナがプラナに投票");

  await node.dispatchEvent("touchstart");
  await node.dispatchEvent("touchmove");
  await node.dispatchEvent("touchend");
  await expect(node).toHaveAttribute("aria-pressed", "false");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");

  await node.tap();
  await expect(node).toHaveAttribute("aria-pressed", "true");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "outgoing");

  await node.tap();
  await expect(node).toHaveAttribute("aria-pressed", "false");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");

  await route.tap();
  await expect(route).toHaveAttribute("aria-pressed", "true");
  await expect(graph.getByRole("tooltip")).toHaveText("アロナがプラナに投票");
  await expect(route.locator("..")).toHaveAttribute("data-relation", "active");

  await route.tap();
  await expect(route).toHaveAttribute("aria-pressed", "false");
  await expect(graph.getByRole("tooltip")).toHaveCount(0);
  await expect(route.locator("..")).toHaveAttribute("data-relation", "default");
});

test("reduced motion skips the long login transition", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockAuthenticatedApi(page);
  await page.addInitScript(() =>
    sessionStorage.setItem("shittim-records-login-transition", "pending"),
  );
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "議論の記録" })).toBeVisible({
    timeout: 1_000,
  });
});

test("English display copy uses Delogy while Japanese copy keeps LINE Seed JP", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: { schemaVersion: 1, authenticated: false, user: null, csrfToken: null },
    }),
  );
  await page.goto("/");
  await page.evaluate(() => document.fonts.ready);

  const loginHeading = page.getByRole("heading", { name: "The Shittim Chest Archive" });
  await expect(loginHeading).toHaveCSS("font-family", /Delogy/);
  await expect(loginHeading).toHaveAttribute("lang", "en");
  await expect(page.getByText("シッテムの箱 議事録閲覧システム")).toHaveCSS(
    "font-family",
    /LINE Seed JP/,
  );
  const authenticate = page.getByRole("link", { name: "AUTHENTICATE" });
  await expect(authenticate).toHaveCSS("font-family", /Delogy/);
  await expect(authenticate).toHaveAttribute("lang", "en");
  expect(await page.evaluate(() => document.fonts.check('16px "Delogy"'))).toBe(true);
  await expect(page).toHaveScreenshot("display-font-login.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixels: 20,
  });

  await page.unroute("**/api/v1/session");
  await mockAuthenticatedApi(page);
  await page.addInitScript(() =>
    sessionStorage.setItem("shittim-records-login-transition", "pending"),
  );
  await page.goto("/");
  const welcome = page.getByText("WELCOME, SENSEI.");
  await expect(welcome).toBeVisible();
  await expect(welcome).toHaveCSS("font-family", /Delogy/);
  await page.addStyleTag({
    content: "*, *::before, *::after { animation: none !important; transition: none !important; }",
  });
  await expect(page).toHaveScreenshot("display-font-transition.png", {
    animations: "allow",
    fullPage: true,
    maxDiffPixels: 20,
  });
});

test("publishes complete Open Graph metadata and a 1200 by 630 preview image", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium");
  await page.route("**/api/v1/session", (route) =>
    route.fulfill({
      json: { schemaVersion: 1, authenticated: false, user: null, csrfToken: null },
    }),
  );
  await page.goto("/");

  const propertyContent = (property: string) =>
    page.locator(`meta[property="${property}"]`).getAttribute("content");
  const namedContent = (name: string) =>
    page.locator(`meta[name="${name}"]`).getAttribute("content");
  const description =
    "シッテムの箱 議事録閲覧システム。吹雪型JCのつどいサーバの先生であることを認証して、議論記録を閲覧できます。";
  const imageUrl = "https://shittim.pitekusu.dev/assets/shittim-chest-archive-og-v2.png";

  await expect(page).toHaveTitle("THE SHITTIM CHEST ARCHIVE | シッテムの箱 議事録閲覧システム");
  expect(await namedContent("description")).toBe(description);
  expect(await page.locator('link[rel="canonical"]').getAttribute("href")).toBe(
    "https://shittim.pitekusu.dev/",
  );
  expect(await propertyContent("og:title")).toBe("THE SHITTIM CHEST ARCHIVE");
  expect(await propertyContent("og:description")).toBe(description);
  expect(await propertyContent("og:type")).toBe("website");
  expect(await propertyContent("og:url")).toBe("https://shittim.pitekusu.dev/");
  expect(await propertyContent("og:site_name")).toBe("シッテムの箱 議事録");
  expect(await propertyContent("og:locale")).toBe("ja_JP");
  expect(await propertyContent("og:image")).toBe(imageUrl);
  expect(await propertyContent("og:image:secure_url")).toBe(imageUrl);
  expect(await propertyContent("og:image:type")).toBe("image/png");
  expect(await propertyContent("og:image:width")).toBe("1200");
  expect(await propertyContent("og:image:height")).toBe("630");
  expect(await propertyContent("og:image:alt")).toContain("THE SHITTIM CHEST ARCHIVE");
  expect(await propertyContent("og:image:alt")).toContain("アロナ・プラナ・安倍晋三AI");
  expect(await namedContent("twitter:card")).toBe("summary_large_image");
  expect(await namedContent("twitter:title")).toBe("THE SHITTIM CHEST ARCHIVE");
  expect(await namedContent("twitter:description")).toBe(description);
  expect(await namedContent("twitter:image")).toBe(imageUrl);
  expect(await namedContent("twitter:image:alt")).toContain("THE SHITTIM CHEST ARCHIVE");

  const image = await page.evaluate(async () => {
    const element = new Image();
    element.src = "/assets/shittim-chest-archive-og-v2.png";
    await element.decode();
    return { height: element.naturalHeight, width: element.naturalWidth };
  });
  expect(image).toEqual({ height: 630, width: 1200 });
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

  const loginButton = page.getByRole("link", { name: "AUTHENTICATE" });
  await expect(loginButton).toBeVisible();
  await loginButton.hover();
  await page.waitForTimeout(150);
  const hoverTransform = await loginButton.evaluate(
    (element) => getComputedStyle(element).transform,
  );
  await page.mouse.down();
  await expect
    .poll(() => loginButton.evaluate((element) => getComputedStyle(element).transform))
    .not.toBe(hoverTransform);
  await page.mouse.move(0, 0);
  await page.mouse.up();
  await page.evaluate(() => document.fonts.ready);
  expect(pageErrors).toEqual([]);
});
