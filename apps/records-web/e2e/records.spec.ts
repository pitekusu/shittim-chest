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
  question: "休日を映画とゲームのどちらで過ごすか",
  requester: { displayName: "パワー系ウナギ", avatar: placeholder("依頼者", "cyan") },
  participants,
  initialOpinions: participants.map(({ slot }, index) => ({
    participant: slot,
    summary: ["物語へ集中する", "一緒に遊べる", "翌日の疲労を抑える"][index],
    proposal: ["映画を一本じっくり観ます。", "協力ゲームを楽しみます。", "短い映画から選びます。"][
      index
    ],
  })),
  finalProposals: participants.map(({ slot }, index) => ({
    participant: slot,
    title: ["映画で整える夜", "ゲームで盛り上がる夜", "余白を残す映画案"][index],
    proposal: [
      "映画を観てから感想を話します。",
      "協力ゲームを二時間遊びます。",
      "短編映画で早めに休みます。",
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
    decision: "今夜は映画を一本観て、感想を話し合います。",
    actions: ["飲み物を用意する", "二時間以内の映画を選ぶ"],
    caveats: ["翌日に疲れを残さない"],
  },
};

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
  await expect(page.getByRole("article")).toContainText(detail.question);
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
  await expect(page.getByText(/所要時間|Evidence|外部根拠/)).toHaveCount(0);

  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
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
