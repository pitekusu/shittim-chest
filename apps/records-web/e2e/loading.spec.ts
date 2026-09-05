import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("session loading animates until ready and respects reduced motion", async ({
  page,
  isMobile,
}, testInfo) => {
  if (isMobile) await page.setViewportSize({ width: 320, height: 740 });
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "no-preference" });
  let releaseSession!: () => void;
  const sessionReady = new Promise<void>((resolve) => {
    releaseSession = resolve;
  });
  await page.route("**/api/v1/session?*", async (route) => {
    await sessionReady;
    await route.fulfill({
      json: {
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      },
    });
  });

  try {
    await page.goto("/");
    const loading = page.getByRole("main");
    const status = page.getByRole("status");
    await expect(loading).toHaveAttribute("aria-busy", "true");
    await expect(status).toContainText("記録庫を開いています");
    const animationTime = () =>
      loading.evaluate((element) =>
        Math.max(
          0,
          ...element
            .getAnimations({ subtree: true })
            .filter((animation) => animation.playState === "running")
            .map((animation) => Number(animation.currentTime)),
        ),
      );
    const initialTime = await animationTime();
    await expect.poll(animationTime).toBeGreaterThan(initialTime);

    for (const colorScheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme });
      await expect(page.locator("html")).toHaveAttribute("data-theme", colorScheme);
      await expect(status).toBeVisible();
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      ).toBe(true);
      expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
      await page.evaluate(() => document.fonts.ready);
      const screenshot = testInfo.outputPath(`loading-${colorScheme}.png`);
      await page.screenshot({ path: screenshot });
      await testInfo.attach(`loading-${colorScheme}`, {
        path: screenshot,
        contentType: "image/png",
      });
    }

    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect
      .poll(() => loading.evaluate((element) => element.getAnimations({ subtree: true }).length))
      .toBe(0);
    await expect(status).toBeVisible();
  } finally {
    releaseSession();
  }

  await expect(page.getByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
});
