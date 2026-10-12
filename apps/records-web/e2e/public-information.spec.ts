import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { DATA_DELETION_MAILTO, LEGAL_CONTACT_EMAIL } from "../src/legalContact";

for (const width of [320, 960]) {
  for (const colorScheme of ["light", "dark"] as const) {
    test(`public information works without authentication at ${width}px in ${colorScheme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme });
      const apiRequests: string[] = [];
      page.on("request", (request) => {
        if (new URL(request.url()).pathname.startsWith("/api/")) apiRequests.push(request.url());
      });
      await page.route("**/api/**", (route) => route.abort());

      await page.goto("/privacy-policy");
      await expect(
        page.getByRole("heading", { name: "プライバシーポリシー", exact: true }),
      ).toBeVisible();
      await expect(page.getByRole("heading", { name: "送信先と閲覧範囲" })).toBeVisible();
      await expect(page).toHaveTitle("プライバシーポリシー | シッテムの箱");
      expect(await new AxeBuilder({ page }).analyze()).toMatchObject({ violations: [] });

      await page
        .getByRole("navigation", { name: "利用者向けの案内" })
        .getByRole("link", { name: "データ削除の申請" })
        .click();
      await expect(
        page.getByRole("heading", { name: "データ削除の申請", exact: true }),
      ).toBeVisible();
      await expect(page.getByRole("link", { name: "データ削除をメールで申請" })).toHaveAttribute(
        "href",
        DATA_DELETION_MAILTO,
      );
      await expect(page.getByRole("link", { name: LEGAL_CONTACT_EMAIL })).toBeVisible();
      await expect(
        page.getByText("パスワード、認証コード、アクセストークンを送る必要はありません。", {
          exact: false,
        }),
      ).toBeVisible();
      expect(await new AxeBuilder({ page }).analyze()).toMatchObject({ violations: [] });
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        width,
      );
      await page.reload();
      await expect(
        page.getByRole("heading", { name: "データ削除の申請", exact: true }),
      ).toBeVisible();
      expect(apiRequests).toEqual([]);
    });
  }
}

test("the login screen offers the public pages without changing protected routes", async ({
  page,
}) => {
  await page.route("**/api/v1/session*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      }),
    }),
  );
  await page.goto("/records/" + "r".repeat(43));
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "プライバシーポリシー" }).click();
  await expect(
    page.getByRole("heading", { name: "プライバシーポリシー", exact: true }),
  ).toBeVisible();
});
