import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { focusManager, QueryClient } from "@tanstack/react-query";
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

function mockFixedMedia(matches: boolean): void {
  vi.spyOn(window, "matchMedia").mockImplementation(
    (query) =>
      ({
        matches,
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as MediaQueryList,
  );
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
  it("shows the loading status until session lookup fails, then offers a retry", async () => {
    let rejectSession!: (reason: Error) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise<Response>((_resolve, reject) => {
            rejectSession = reject;
          }),
      ),
    );

    render(<App />);

    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent("記録庫を開いています");
    await act(async () => rejectSession(new Error("offline")));

    expect(await screen.findByText("記録庫へ接続できません")).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "もう一度試す" })).toBeVisible();
  });

  it("shows Memorial and both SYSTEM ACCESS links and opens service status for a member", async () => {
    window.history.replaceState(null, "", "/admin");
    const requests = mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "サービス状態確認" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "ACCESS DENIED" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "サービス状態確認" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "プロンプト管理" })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "メモリアルロビー" })).toHaveLength(2);
    expect(screen.getByText("SYSTEM ACCESS")).toBeVisible();
    expect(requests).toEqual(["/api/v1/session?contract=admin-v1", "/api/v1/admin/status"]);
  });

  it("opens the owner-only Memorial route for an authenticated member", async () => {
    window.history.replaceState(null, "", "/memorial");
    const requests = mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "メモリアルロビー" })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "まだメモリアルロビーにはログインできません" }),
    ).toBeVisible();
    expect(requests).toEqual(["/api/v1/session?contract=admin-v1", "/api/v1/memorial"]);
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
    mockFixedMedia(true);
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
    expect(screen.getByRole("link", { name: "Discordでログイン" })).toHaveAttribute(
      "href",
      "/api/v1/auth/discord/start?returnTo=%2F",
    );
    expect(screen.getByText("シッテムの箱 議事録閲覧システム")).toBeVisible();
    expect(screen.getByText("吹雪型JCのつどいサーバの先生であることを認証します。")).toBeVisible();
  });

  it.each(["/insights", "/momotalk", "/memorial", "/admin", "/admin/prompts"])(
    "returns an anonymous visitor to %s after login",
    async (path) => {
      window.history.replaceState(null, "", path);
      mockApi({
        schemaVersion: 1,
        authenticated: false,
        isAdmin: false,
        user: null,
        csrfToken: null,
      });

      render(<App />);

      expect(await screen.findByRole("link", { name: "Discordでログイン" })).toHaveAttribute(
        "href",
        `/api/v1/auth/discord/start?returnTo=${encodeURIComponent(path)}`,
      );
    },
  );

  it("finishes goodbye cleanup even if the session refreshes during the transition", async () => {
    mockFixedMedia(false);
    const requests = mockApi();
    render(<App />);

    await screen.findByRole("heading", { name: "議論の記録" });
    const staleAt = Date.now() + 31_000;
    vi.spyOn(Date, "now").mockReturnValue(staleAt);
    fireEvent.click(screen.getAllByRole("button", { name: "ログアウト" })[0]!);

    expect(await screen.findByText("GOODBYE, SENSEI.")).toBeVisible();
    expect(screen.getByLabelText("ログオフしました")).toBeVisible();
    expect(screen.queryByText("WELCOME, SENSEI.")).not.toBeInTheDocument();
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await waitFor(() => {
      expect(requests.filter((path) => path === "/api/v1/session?contract=admin-v1")).toHaveLength(
        2,
      );
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
        if (path === "/api/v1/session?contract=admin-v1") {
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

  it("keeps the archive after a failed logout and retries without duplicate pending writes", async () => {
    mockFixedMedia(true);
    mockApi();
    const originalFetch = fetch;
    const logoutRequests: (RequestInit | undefined)[] = [];
    let completeFirstLogout!: () => void;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path !== "/api/v1/logout") return originalFetch(input, init);
        logoutRequests.push(init);
        if (logoutRequests.length > 1) return originalFetch(input, init);
        return new Promise<Response>((resolve) => {
          completeFirstLogout = () =>
            resolve(
              response(
                {
                  error: {
                    code: "INTERNAL_ERROR",
                    message: "ログアウトできませんでした。",
                    requestId: "request-id",
                  },
                },
                500,
              ),
            );
        });
      }),
    );
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });
    const search = screen.getByRole("searchbox");
    fireEvent.change(search, { target: { value: "休日" } });
    fireEvent.click(screen.getAllByRole("button", { name: "ログアウト" })[0]!);

    await waitFor(() => expect(logoutRequests).toHaveLength(1));
    const pending = screen.getAllByRole("button", { name: "ログアウト中…" });
    expect(pending.every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
    for (const button of pending) fireEvent.click(button);
    expect(logoutRequests).toHaveLength(1);
    expect(new Headers(logoutRequests[0]?.headers).get("X-CSRF-Token")).toBe("csrf-token");
    await act(async () => completeFirstLogout());

    expect(await screen.findByRole("alert")).toHaveTextContent("ログアウトできませんでした");
    expect(screen.getByRole("heading", { name: "議論の記録" })).toBeVisible();
    expect(search).toHaveValue("休日");
    expect(screen.queryByText("GOODBYE, SENSEI.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "もう一度ログアウト" }));

    expect(await screen.findByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
    expect(logoutRequests).toHaveLength(2);
    expect(sessionStorage).toHaveLength(0);
  });

  it("discards private caches when a tab return refresh reports an anonymous session directly", async () => {
    const requests = mockApi();
    const originalFetch = fetch;
    let sessionReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path !== "/api/v1/session?contract=admin-v1") return originalFetch(input, init);
        sessionReads += 1;
        if (sessionReads === 1) return originalFetch(input, init);
        return Promise.resolve(
          response({
            schemaVersion: 1,
            authenticated: false,
            isAdmin: false,
            user: null,
            csrfToken: null,
          }),
        );
      }),
    );
    // Capture the real app client through its public cache API, without replacing its behavior.
    const cacheLookup = vi.spyOn(QueryClient.prototype, "getQueryCache");
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });
    const client = cacheLookup.mock.contexts.find((value) => value instanceof QueryClient);
    if (!(client instanceof QueryClient)) throw new Error("App query client was not created");
    client.setQueryData(["costs", "week"], { fixture: "private costs" });
    client.setQueryData(["admin", "prompts"], { fixture: "private configuration" });
    expect(client.getQueryCache().findAll({ queryKey: ["records"] })).toHaveLength(1);
    const search = screen.getByRole("searchbox");
    fireEvent.change(search, { target: { value: "local-only query" } });
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 31_000);

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    expect(await screen.findByRole("heading", { name: "The Shittim Chest Archive" })).toBeVisible();
    expect(sessionReads).toBe(2);
    expect(client.getQueryData(["costs", "week"])).toBeUndefined();
    expect(client.getQueryData(["admin", "prompts"])).toBeUndefined();
    expect(client.getQueryCache().findAll({ queryKey: ["records"] })).toHaveLength(0);
    expect(client.getQueryData(["records-session"])).toMatchObject({ authenticated: false });
    expect(requests).not.toContain("/api/v1/logout");
  });
});
