import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { App } from "./App";
import { authenticatedSession, mockApi, response } from "./test/recordsTestUtils";
import { THEME_STORAGE_KEY } from "./theme";

function installThemeColorMeta(): HTMLMetaElement {
  const themeColor = document.createElement("meta");
  themeColor.name = "theme-color";
  themeColor.content = "#f5fbff";
  document.head.append(themeColor);
  return themeColor;
}

afterEach(() => {
  focusManager.setFocused(undefined);
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.colorScheme = "";
  document.querySelector('meta[name="theme-color"]')?.remove();
  window.history.replaceState(null, "", "/");
});

describe("App shell", () => {
  it("shows ADMIN to every member and renders a branded denial without Admin API calls", async () => {
    window.history.replaceState(null, "", "/admin");
    const requests = mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "ACCESS DENIED" })).toBeVisible();
    expect(screen.getByText("この画面を利用する権限がありません。")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "ADMIN" })).toHaveLength(2);
    expect(requests).toEqual(["/api/v1/session"]);
  });

  it("coordinates branded motion and heading focus across internal routes", async () => {
    mockApi();
    const { container } = render(<App />);

    const archiveHeading = await screen.findByRole("heading", { name: "議論の記録" });
    const initialScene = container.querySelector<HTMLElement>("[data-route-scene]");
    expect(initialScene).toHaveAttribute("data-route-motion", "idle");
    expect(initialScene?.parentElement).toHaveAttribute("data-route-kind", "archive");
    expect(
      await screen.findByRole("link", { name: "「休日の過ごし方を決める」の記録を読む" }),
    ).toHaveStyle("--route-motion-delay: 60ms");
    expect(archiveHeading).not.toHaveFocus();

    fireEvent.click(screen.getByRole("link", { name: "いろいろな記録" }));

    const insightsHeading = await screen.findByRole("heading", { name: "いろいろな記録" });
    const insightsScene = container.querySelector<HTMLElement>("[data-route-scene]");
    expect(insightsScene).not.toBe(initialScene);
    await waitFor(() => expect(insightsScene).toHaveAttribute("data-route-motion", "active"));
    expect(insightsScene?.parentElement).toHaveAttribute("data-route-kind", "insights");
    await waitFor(() => expect(insightsHeading).toHaveFocus());
    expect(screen.getByRole("region", { name: "勝利回数ランキング" })).toHaveStyle(
      "--route-motion-delay: 60ms",
    );
    expect(screen.getByRole("region", { name: "依頼回数ランキング" })).toHaveStyle(
      "--route-motion-delay: 100ms",
    );

    fireEvent.click(screen.getByRole("link", { name: "議論の記録" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "議論の記録" })).toHaveFocus());
    expect(container.querySelector("[data-route-scene]")?.parentElement).toHaveAttribute(
      "data-route-kind",
      "archive",
    );
  });

  it("keeps both theme switches synchronized without refetching session or records", async () => {
    installThemeColorMeta();
    const requests = mockApi();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "議論の記録" })).toBeVisible();
    const switches = screen.getAllByRole("switch", { name: "ダークモード" });
    expect(switches).toHaveLength(2);
    expect(switches[0]).toHaveAttribute("aria-checked", "false");
    const requestsBeforeToggle = [...requests];

    fireEvent.click(switches[0]!);

    expect(switches[0]).toHaveAttribute("aria-checked", "true");
    expect(switches[1]).toHaveAttribute("aria-checked", "true");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
    expect(document.querySelector('meta[name="theme-color"]')).toHaveAttribute(
      "content",
      "#071724",
    );
    await act(async () => Promise.resolve());
    expect(requests).toEqual(requestsBeforeToggle);
  });

  it("follows OS changes only until a manual theme is stored", async () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    let matches = false;
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          get matches() {
            return matches;
          },
          media: query,
          onchange: null,
          addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) =>
            listeners.add(listener),
          removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) =>
            listeners.delete(listener),
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => false,
        }) as MediaQueryList,
    );
    mockApi();
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });

    matches = true;
    act(() => {
      for (const listener of listeners) listener({ matches: true } as MediaQueryListEvent);
    });
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();

    fireEvent.click(screen.getAllByRole("switch", { name: "ダークモード" })[0]!);
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");

    matches = false;
    act(() => {
      for (const listener of listeners) listener({ matches: false } as MediaQueryListEvent);
    });
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("prefers a saved theme over the OS preference", async () => {
    localStorage.setItem(THEME_STORAGE_KEY, "light");
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          matches: true,
          media: query,
          onchange: null,
          addEventListener: () => undefined,
          removeEventListener: () => undefined,
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => false,
        }) as MediaQueryList,
    );
    mockApi();
    render(<App />);

    await screen.findByRole("heading", { name: "議論の記録" });
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getAllByRole("switch", { name: "ダークモード" })[0]).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("shows the approved login page for an anonymous Guild visitor", async () => {
    mockApi({
      schemaVersion: 1,
      authenticated: false,
      isAdmin: false,
      user: null,
      csrfToken: null,
    });

    render(<App />);

    const productName = await screen.findByRole("heading", {
      name: "The Shittim Chest Archive",
    });
    expect(productName).toBeVisible();
    expect(Array.from(productName.children, (child) => child.textContent)).toEqual([
      "THE SHITTIM",
      "CHEST ARCHIVE",
    ]);
    expect(screen.getByRole("link", { name: "AUTHENTICATE" })).toHaveAttribute(
      "href",
      "/api/v1/auth/discord/start?returnTo=%2F",
    );
    expect(screen.getByText("シッテムの箱 議事録閲覧システム")).toBeVisible();
    expect(screen.getByText("吹雪型JCのつどいサーバの先生であることを認証します。")).toBeVisible();
  });

  it("returns an anonymous visitor to the requested insights page after login", async () => {
    window.history.replaceState(null, "", "/insights");
    mockApi({
      schemaVersion: 1,
      authenticated: false,
      isAdmin: false,
      user: null,
      csrfToken: null,
    });

    render(<App />);

    expect(await screen.findByRole("link", { name: "AUTHENTICATE" })).toHaveAttribute(
      "href",
      "/api/v1/auth/discord/start?returnTo=%2Finsights",
    );
  });

  it("finishes goodbye cleanup even if the session refreshes during the transition", async () => {
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query) =>
        ({
          matches: false,
          media: query,
          onchange: null,
          addEventListener: () => undefined,
          removeEventListener: () => undefined,
          addListener: () => undefined,
          removeListener: () => undefined,
          dispatchEvent: () => false,
        }) as MediaQueryList,
    );
    const requests = mockApi();
    render(<App />);

    await screen.findByRole("heading", { name: "議論の記録" });
    const staleAt = Date.now() + 31_000;
    vi.spyOn(Date, "now").mockReturnValue(staleAt);
    fireEvent.click(screen.getAllByRole("button", { name: "LOGOFF" })[0]!);

    expect(await screen.findByText("GOODBYE, SENSEI.")).toBeVisible();
    expect(screen.getByLabelText("ログオフしました")).toBeVisible();
    expect(screen.queryByText("WELCOME, SENSEI.")).not.toBeInTheDocument();
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await waitFor(() => {
      expect(requests.filter((path) => path === "/api/v1/session")).toHaveLength(2);
    });
    expect(screen.getByText("GOODBYE, SENSEI.")).toBeVisible();
    await waitFor(
      () => {
        expect(screen.getByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
      },
      { timeout: 2_500 },
    );
    expect(screen.queryByText("GOODBYE, SENSEI.")).not.toBeInTheDocument();
    expect(requests).toContain("/api/v1/logout");
    expect(sessionStorage).toHaveLength(0);
  });

  it("returns to login when a protected request reports an expired session", async () => {
    let sessionRequests = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path === "/api/v1/session") {
          sessionRequests += 1;
          const session =
            sessionRequests === 1
              ? authenticatedSession()
              : {
                  schemaVersion: 1,
                  authenticated: false,
                  isAdmin: false,
                  user: null,
                  csrfToken: null,
                };
          return Promise.resolve(response(session));
        }
        if (path.startsWith("/api/v1/records?")) {
          return Promise.resolve(
            response(
              {
                error: {
                  code: "AUTHENTICATION_REQUIRED",
                  message: "ログインし直してください。",
                  requestId: "request-id",
                },
              },
              401,
            ),
          );
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<App />);

    expect(await screen.findByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
    expect(sessionRequests).toBe(2);
  });
});
