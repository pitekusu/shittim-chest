import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { fileURLToPath } from "node:url";

const roomId = "a".repeat(43);
const week = {
  weekId: "2026-09-13",
  periodStart: "2026-09-06T09:00:00Z",
  periodEnd: "2026-09-13T09:00:00Z",
  publishAt: "2026-09-13T11:00:00Z",
};
const avatar = (name: string, variant = "cyan") => ({
  kind: "placeholder",
  url: null as string | null,
  alt: `${name}のアバター`,
  fallbackVariant: variant,
});
const room = {
  roomId,
  requester: { displayName: "散歩好きの先生", avatar: avatar("先生") },
  questionCount: 2,
  state: "ready",
};
const participants = [
  { slot: "participant-a", displayName: "アロナ", avatar: avatar("アロナ") },
  { slot: "participant-b", displayName: "プラナ", avatar: avatar("プラナ", "pink") },
  { slot: "participant-c", displayName: "安倍晋三AI", avatar: avatar("安倍晋三AI", "lavender") },
];
// Fictional visual-QA content, not provider output or real requester data.
const lines = [
  [0, "先生、今週も秋のお散歩の話をしてくれましたね！一緒に落ち葉を集めたいです。"],
  [1, "はい。ですが、帰り道のおやつを先に決めていたのはアロナ先輩です。"],
  [0, "えへへ……それもお散歩の楽しみですよ！"],
  [2, "まさに、食欲と芸術の連立であります。私は焼き芋を推したい。"],
  [1, "連立にするほど対立していません。"],
  [2, "そこはですね、あえて大きく構えることが大事なんです。"],
  [0, "ふふっ。先生が次はどんな話をしてくれるのか、楽しみです！"],
  [2, "質問が届くと、今週も元気そうだなと感じますね。"],
  [1, "……私も。次のお話、待っています。"],
] as const;
const messages = lines.map(([slot, text], index) => ({
  id: index + 1,
  participant: participants[slot].slot,
  text,
}));

async function setup(page: Page) {
  for (const participant of participants) {
    participant.avatar = {
      ...participant.avatar,
      kind: "image",
      url: `/test-${participant.slot}.webp`,
    };
    await page.route(`**/test-${participant.slot}.webp`, (route) =>
      route.fulfill({
        path: fileURLToPath(
          new URL(`../scripts/og-image-assets/${participant.slot}.webp`, import.meta.url),
        ),
        contentType: "image/webp",
      }),
    );
  }
  await page.route("**/api/v1/session?*", (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: true,
        isAdmin: false,
        user: { displayName: "閲覧者", avatar: avatar("閲覧者") },
        csrfToken: "test-csrf",
      },
    }),
  );
  await page.route("**/api/v1/momotalk/weeks?*", (route) =>
    route.fulfill({ json: { schemaVersion: 1, weeks: [week], nextCursor: null } }),
  );
  await page.route(`**/api/v1/momotalk/weeks/${week.weekId}/rooms?*`, (route) =>
    route.fulfill({ json: { schemaVersion: 1, week, rooms: [room], nextCursor: null } }),
  );
  await page.route(`**/api/v1/momotalk/weeks/${week.weekId}/rooms/${roomId}`, (route) =>
    route.fulfill({
      json: {
        schemaVersion: 1,
        week,
        room,
        participants,
        messages,
        images: [
          {
            participant: "participant-a",
            mood: "happy",
            state: "ready",
            url: "/test-selfie.svg",
            thumbnailUrl: "/test-selfie.svg",
            downloadUrl: "/test-selfie.svg?download=1",
          },
        ],
      },
    }),
  );
  await page.route("**/test-selfie.svg*", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="480"><rect width="320" height="480" fill="#dff8fc"/><circle cx="160" cy="175" r="70" fill="#8ad6e5"/><text x="160" y="330" text-anchor="middle" fill="#183751">TEST IMAGE</text></svg>',
    }),
  );
  await page.goto("/momotalk");
  await page.getByRole("button", { name: /散歩好きの先生/ }).click();
}

test("replays on opening, supports skip and image save, with accessible responsive layout", async ({
  page,
}, testInfo) => {
  await setup(page);
  const posts = page.getByRole("list", { name: "チャットの発言" }).getByRole("listitem");
  await expect(posts).toHaveCount(0);
  await page.getByRole("button", { name: "すべて表示" }).click();
  await expect(posts).toHaveCount(10); // Nine text posts followed by one image.
  await expect(page.getByRole("button", { name: "もう一度再生" })).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty(
    "scrollWidth",
    await page.locator("body").evaluate((el) => el.clientWidth),
  );
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await posts.first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("momotalk-light.png"), fullPage: true });
  await page.getByRole("button", { name: /画像を拡大/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("link", { name: "画像を保存" })).toHaveAttribute(
    "href",
    /download=1/,
  );
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "もう一度再生" }).click();
  await expect(posts).toHaveCount(0);
});

test("reduced motion reveals all messages immediately and dark mode remains readable", async ({
  page,
}, testInfo) => {
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });
  await setup(page);
  await expect(
    page.getByRole("list", { name: "チャットの発言" }).getByRole("listitem"),
  ).toHaveCount(10);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page
    .getByRole("list", { name: "チャットの発言" })
    .getByRole("listitem")
    .first()
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("momotalk-dark.png"), fullPage: true });
});
