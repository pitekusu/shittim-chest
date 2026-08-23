import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { App } from "./App";
import { Avatar, cardDecorationForRecord, formatCompletedDateTime } from "./components";
import type { SessionResponse } from "./api";
import { isRecordsApiResponse } from "./contracts";
import { THEME_STORAGE_KEY } from "./theme";

const RECORD_ID = "r".repeat(43);

function placeholder(displayName: string, fallbackVariant: "cyan" | "pink" | "lavender") {
  return {
    kind: "placeholder",
    url: null,
    alt: `${displayName}のアバター`,
    fallbackVariant,
  };
}

function recordDetail() {
  const participants = [
    ["participant-a", "アロナ", "cyan"],
    ["participant-b", "プラナ", "pink"],
    ["participant-c", "安倍晋三AI", "lavender"],
  ].map(([slot, displayName, fallbackVariant]) => ({
    slot,
    displayName,
    avatar: placeholder(displayName!, fallbackVariant as "cyan" | "pink" | "lavender"),
  }));
  return {
    schemaVersion: 1,
    recordId: RECORD_ID,
    completedAt: "2026-08-15T06:00:00Z",
    question: "休日の過ごし方を決める",
    requester: { displayName: "依頼者", avatar: placeholder("依頼者", "cyan") },
    participants,
    initialOpinions: participants.map(({ slot }) => ({
      participant: slot,
      summary: "要約",
      proposal: "初回意見",
    })),
    finalProposals: participants.map(({ slot }) => ({
      participant: slot,
      title: "最終案",
      proposal: "完成した提案",
    })),
    votes: [
      { voter: "participant-a", candidate: "participant-b", reason: "理由A" },
      { voter: "participant-b", candidate: "participant-a", reason: "理由B" },
      { voter: "participant-c", candidate: "participant-a", reason: "理由C" },
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
      victoryMessage: "勝利しました",
      decision: "最終決定",
      actions: ["実行する"],
      caveats: ["注意する"],
    },
  };
}

function authenticatedSession() {
  return {
    schemaVersion: 1,
    authenticated: true,
    user: { displayName: "閲覧者", avatar: placeholder("閲覧者", "cyan") },
    csrfToken: "csrf-token",
  };
}

function listResponse() {
  const detail = recordDetail();
  return {
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
  };
}

function rankingsResponse() {
  return {
    schemaVersion: 1,
    wins: [
      { rank: 1, displayName: "アロナ", avatar: placeholder("アロナ", "cyan"), count: 20 },
      { rank: 2, displayName: "プラナ", avatar: placeholder("プラナ", "pink"), count: 18 },
      {
        rank: 3,
        displayName: "安倍晋三AI",
        avatar: placeholder("安倍晋三AI", "lavender"),
        count: 16,
      },
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
      {
        rank: 3,
        displayName: "先生",
        avatar: placeholder("先生", "lavender"),
        count: 8,
      },
    ],
    generatedAt: "2026-08-22T00:00:00Z",
  };
}

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installThemeColorMeta(): HTMLMetaElement {
  const themeColor = document.createElement("meta");
  themeColor.name = "theme-color";
  themeColor.content = "#f5fbff";
  document.head.append(themeColor);
  return themeColor;
}

function mockApi(
  session: SessionResponse = authenticatedSession() as SessionResponse,
  rankings: unknown = rankingsResponse(),
) {
  const requests: string[] = [];
  let currentSession = session;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(path);
      if (path === "/api/v1/session") return Promise.resolve(response(currentSession));
      if (path === "/api/v1/logout") {
        currentSession = {
          schemaVersion: 1,
          authenticated: false,
          user: null,
          csrfToken: null,
        };
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (path.startsWith("/api/v1/records?")) return Promise.resolve(response(listResponse()));
      if (path === `/api/v1/records/${RECORD_ID}`) {
        return Promise.resolve(response(recordDetail()));
      }
      if (path === "/api/v1/insights/rankings") {
        return Promise.resolve(response(rankings));
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  return requests;
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

describe("formatCompletedDateTime", () => {
  it("formats completed timestamps in fixed JST to minute precision", () => {
    expect(formatCompletedDateTime("2026-08-15T06:00:00Z")).toBe("2026年8月15日 15:00");
    expect(formatCompletedDateTime("2026-12-31T15:00:00Z")).toBe("2027年1月1日 00:00");
    expect(formatCompletedDateTime("2026-08-15T14:59:59Z")).toBe("2026年8月15日 23:59");
  });
});

describe("cardDecorationForRecord", () => {
  it("selects a stable but varied card ornament from the record identity", () => {
    const identities = ["r", "s", "t", "u"].map((value) => value.repeat(43));
    const decorations = identities.map(cardDecorationForRecord);

    expect(cardDecorationForRecord(identities[0]!)).toEqual(decorations[0]);
    expect(new Set(decorations.map(({ variant }) => variant)).size).toBeGreaterThan(1);
    for (const decoration of decorations) {
      expect(decoration.rotation).toBeGreaterThanOrEqual(-6);
      expect(decoration.rotation).toBeLessThanOrEqual(6);
      expect(decoration.shiftX).toBeGreaterThanOrEqual(-8);
      expect(decoration.shiftX).toBeLessThanOrEqual(8);
      expect(decoration.shiftY).toBeGreaterThanOrEqual(-5);
      expect(decoration.shiftY).toBeLessThanOrEqual(5);
    }
  });
});

describe("App", () => {
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
    expect(insightsScene).toHaveAttribute("data-route-motion", "active");
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
    mockApi({ schemaVersion: 1, authenticated: false, user: null, csrfToken: null });

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
    mockApi({ schemaVersion: 1, authenticated: false, user: null, csrfToken: null });

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
  });

  it("renders completed records without duration or Evidence", async () => {
    const requests = mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "議論の記録" })).toBeVisible();
    const card = await screen.findByRole("article");
    expect(within(card).getByText("休日の過ごし方を決める")).toBeVisible();
    expect(within(card).getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
    expect(within(card).getAllByText("依頼者")).toHaveLength(2);
    expect(within(card).getByText("アロナ")).toBeVisible();
    expect(
      within(screen.getByRole("navigation", { name: "モバイルナビゲーション" })).getByRole(
        "button",
        { name: "LOGOFF" },
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/所要時間|Evidence|外部根拠/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("開始日")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("終了日")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "新しい順" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "古い順" })).not.toBeChecked();
    const cardLink = screen.getByRole("link", {
      name: "「休日の過ごし方を決める」の記録を読む",
    });
    expect(cardLink).toContainElement(card);
    expect(within(card).queryByRole("link")).not.toBeInTheDocument();
    expect(card.querySelector("[data-card-decoration]")).toHaveAttribute("aria-hidden", "true");
    expect(within(card).getByText("記録を読む")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByLabelText("フリーワード検索")).toHaveAttribute(
      "placeholder",
      "質問文などを入力",
    );
    expect(screen.queryByText("議論記録を閲覧できます。")).not.toBeInTheDocument();
    expect(screen.queryByText("検索対象は現在読み込み済みのカードです。")).not.toBeInTheDocument();
    expect(requests).toContain("/api/v1/records?limit=12&sort=newest");
  });

  it("requests the complete archive in the selected order", async () => {
    const requests = mockApi();
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });

    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));

    await waitFor(() => {
      expect(requests).toContain("/api/v1/records?limit=12&sort=oldest");
    });
    expect(screen.getByRole("radio", { name: "古い順" })).toBeChecked();
  });

  it("keeps the latest choice after rapid sort changes", async () => {
    const requests = mockApi();
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });

    const newest = screen.getByRole("radio", { name: "新しい順" });
    const oldest = screen.getByRole("radio", { name: "古い順" });
    fireEvent.click(oldest);
    fireEvent.click(newest);
    fireEvent.click(oldest);

    await waitFor(() => expect(oldest).toBeChecked());
    expect(newest).not.toBeChecked();
    expect(requests).toContain("/api/v1/records?limit=12&sort=oldest");
  });

  it("renders the two ranking panels with competition ranks and no cost placeholder", async () => {
    window.history.replaceState(null, "", "/insights");
    const requests = mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "いろいろな記録" })).toBeVisible();
    expect(
      screen.queryByText("これまでの議論を、ランキングで振り返れます。"),
    ).not.toBeInTheDocument();
    const wins = screen.getByRole("region", { name: "勝利回数ランキング" });
    const requestsPanel = screen.getByRole("region", { name: "依頼回数ランキング" });
    expect(await within(wins).findAllByRole("listitem")).toHaveLength(3);
    expect(within(wins).getAllByText("安倍晋三AI")).toHaveLength(2);
    expect(
      within(wins).getByRole("status", { name: "勝利回数ランキングの合計" }),
    ).toHaveTextContent("合計54回");
    expect(
      within(requestsPanel).getByRole("status", { name: "依頼回数ランキングの上位合計" }),
    ).toHaveTextContent("上位合計32回");
    const aronaBar = within(wins).getByRole("meter", {
      name: "アロナ: 20回（最多20回との比較）",
    });
    const planaBar = within(wins).getByRole("meter", {
      name: "プラナ: 18回（最多20回との比較）",
    });
    expect(aronaBar).toHaveAttribute("max", "20");
    expect(aronaBar).toHaveAttribute("value", "20");
    expect(planaBar).toHaveAttribute("value", "18");
    expect(within(requestsPanel).getAllByText("1位")).toHaveLength(2);
    expect(within(requestsPanel).getByText("3位")).toBeInTheDocument();
    expect(within(requestsPanel).getAllByRole("meter")).toHaveLength(3);
    expect(
      within(requestsPanel).getByRole("meter", {
        name: "パワー系ウナギ: 12回（上位合計の38%、最多12回との比較）",
      }),
    ).toHaveAttribute("value", "12");
    expect(screen.getByText("最終集計:")).toHaveTextContent("2026年8月22日 09:00");
    expect(screen.queryByText(/費用|Fargate|OpenAI/)).not.toBeInTheDocument();
    expect(requests).toContain("/api/v1/insights/rankings");
  });

  it("keeps all winners at the same podium height when every participant is tied", async () => {
    window.history.replaceState(null, "", "/insights");
    const tiedRankings = rankingsResponse();
    tiedRankings.wins = tiedRankings.wins.map((entry) => ({ ...entry, rank: 1, count: 20 }));
    mockApi(authenticatedSession() as SessionResponse, tiedRankings);

    const { container } = render(<App />);

    await screen.findByRole("heading", { name: "いろいろな記録" });
    await within(screen.getByRole("region", { name: "勝利回数ランキング" })).findAllByRole(
      "listitem",
    );
    expect(container.querySelector('[data-podium-layout="shared"]')).toBeInTheDocument();
    expect(container.querySelector('[data-podium-layout="ranked"]')).not.toBeInTheDocument();
  });

  it("shows snapshot preparation independently in both ranking panels", async () => {
    window.history.replaceState(null, "", "/insights");
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path === "/api/v1/session") {
          return Promise.resolve(response(authenticatedSession()));
        }
        if (path === "/api/v1/insights/rankings") {
          return Promise.resolve(
            response(
              {
                error: {
                  code: "INSIGHTS_UNAVAILABLE",
                  message: "集計を準備しています。",
                  requestId: "request-id",
                },
              },
              503,
            ),
          );
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<App />);

    expect(await screen.findAllByText("集計を準備しています")).toHaveLength(2);
    expect(screen.getAllByRole("status")).toHaveLength(2);
  });

  it("filters loaded records by the selected requester", async () => {
    const first = listResponse().items[0]!;
    const second = {
      ...structuredClone(first),
      recordId: "s".repeat(43),
      questionPreview: "別の依頼",
      requester: {
        displayName: "パワー系ウナギ",
        avatar: placeholder("パワー系ウナギ", "pink"),
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        if (path === "/api/v1/session") {
          return Promise.resolve(response(authenticatedSession()));
        }
        if (path.startsWith("/api/v1/records?")) {
          const items = path.includes("sort=oldest") ? [first] : [first, second];
          return Promise.resolve(response({ schemaVersion: 1, items, nextCursor: null }));
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<App />);

    expect(await screen.findByText("別の依頼")).toBeVisible();
    const requesterFilter = screen.getByRole("button", { name: "依頼者" });
    fireEvent.click(requesterFilter);
    const requesterOption = within(screen.getByRole("listbox", { name: "依頼者" })).getByRole(
      "option",
      { name: "パワー系ウナギ" },
    );
    expect(requesterOption.firstElementChild).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(requesterOption);

    expect(screen.getByText("別の依頼")).toBeVisible();
    expect(screen.queryByText("休日の過ごし方を決める")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "勝者" }));
    const winnerOption = within(screen.getByRole("listbox", { name: "勝者" })).getByRole("option", {
      name: "アロナ",
    });
    expect(winnerOption.firstElementChild).toHaveAttribute("aria-hidden", "true");
    fireEvent.keyDown(winnerOption, { key: "Escape" });

    fireEvent.click(screen.getByRole("radio", { name: "古い順" }));

    await waitFor(() => expect(requesterFilter).toHaveTextContent("すべて"));
    expect(await screen.findByText("休日の過ごし方を決める")).toBeVisible();
  });

  it("automatically loads the next page when the end sentinel enters the viewport", async () => {
    let notify: IntersectionObserverCallback | undefined;
    const observe = vi.fn<(target: Element) => void>();
    const unobserve = vi.fn<(target: Element) => void>();
    class MockIntersectionObserver implements IntersectionObserver {
      public readonly root = null;
      public readonly rootMargin = "320px 0px";
      public readonly scrollMargin = "";
      public readonly thresholds = [0];

      public constructor(callback: IntersectionObserverCallback) {
        notify = callback;
      }

      public observe = observe;
      public disconnect = vi.fn<() => void>();
      public unobserve = unobserve;
      public takeRecords = () => [];
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    const first = listResponse().items[0]!;
    const second = {
      ...structuredClone(first),
      recordId: "s".repeat(43),
      questionPreview: "自動で読み込まれた議論",
    };
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        requests.push(path);
        if (path === "/api/v1/session") {
          return Promise.resolve(response(authenticatedSession()));
        }
        if (path.includes("cursor=next-page")) {
          return Promise.resolve(response({ schemaVersion: 1, items: [second], nextCursor: null }));
        }
        if (path.startsWith("/api/v1/records?")) {
          return Promise.resolve(
            response({ schemaVersion: 1, items: [first], nextCursor: "next-page" }),
          );
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<App />);

    expect(await screen.findByText(first.questionPreview)).toBeVisible();
    await waitFor(() => expect(observe).toHaveBeenCalledOnce());
    notify?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);

    expect(await screen.findByText(second.questionPreview)).toBeVisible();
    expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(1);
    expect(unobserve).toHaveBeenCalledOnce();
  });

  it("does not drain remaining pages automatically while a local filter is active", async () => {
    let notify: IntersectionObserverCallback | undefined;
    class MockIntersectionObserver implements IntersectionObserver {
      public readonly root = null;
      public readonly rootMargin = "320px 0px";
      public readonly scrollMargin = "";
      public readonly thresholds = [0];

      public constructor(callback: IntersectionObserverCallback) {
        notify = callback;
      }

      public observe = vi.fn<(target: Element) => void>();
      public disconnect = vi.fn<() => void>();
      public unobserve = vi.fn<(target: Element) => void>();
      public takeRecords = () => [];
    }
    vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
    const first = listResponse().items[0]!;
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path =
          typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        requests.push(path);
        if (path === "/api/v1/session") {
          return Promise.resolve(response(authenticatedSession()));
        }
        if (path.includes("cursor=next-page")) {
          return Promise.resolve(response({ schemaVersion: 1, items: [], nextCursor: null }));
        }
        if (path.startsWith("/api/v1/records?")) {
          return Promise.resolve(
            response({ schemaVersion: 1, items: [first], nextCursor: "next-page" }),
          );
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );

    render(<App />);

    expect(await screen.findByText(first.questionPreview)).toBeVisible();
    fireEvent.change(screen.getByLabelText("フリーワード検索"), {
      target: { value: "一致しない検索" },
    });
    notify?.([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver);

    await waitFor(() => {
      expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(0);
    });
    fireEvent.click(screen.getByRole("button", { name: "検索対象をさらに読み込む" }));
    await waitFor(() => {
      expect(requests.filter((path) => path.includes("cursor=next-page"))).toHaveLength(1);
    });
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
              : { schemaVersion: 1, authenticated: false, user: null, csrfToken: null };
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

  it("renders named votes and only the saved final result on detail", async () => {
    window.history.replaceState(null, "", `/records/${RECORD_ID}`);
    mockApi();

    render(<App />);

    expect(await screen.findByRole("heading", { name: "休日の過ごし方を決める" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "3人の意見" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "投票" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "アロナ → プラナ" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "最終決定" })).toBeVisible();
    expect(screen.getByText("勝利しました")).toBeVisible();
    expect(screen.getByText("実行する")).toBeVisible();
    expect(screen.getByText("2026年8月15日 15:00")).toHaveAttribute(
      "datetime",
      "2026-08-15T06:00:00Z",
    );
  });

  it("validates API payloads against the generated Python contract", () => {
    expect(isRecordsApiResponse(recordDetail())).toBe(true);
    expect(isRecordsApiResponse({ authenticated: false })).toBe(false);
    expect(isRecordsApiResponse({ schemaVersion: 1, privateId: "forbidden" })).toBe(false);

    const conflictingWinner = structuredClone(recordDetail());
    conflictingWinner.finalDecision.winner = "participant-b";
    expect(isRecordsApiResponse(conflictingWinner)).toBe(false);
  });

  it("uses the geometric placeholder when an avatar image cannot load", () => {
    render(
      <Avatar
        avatar={{
          kind: "image",
          url: "https://media.example.invalid/avatar.webp",
          alt: "依頼者のアバター",
          fallbackVariant: "cyan",
        }}
      />,
    );

    fireEvent.error(screen.getByRole("img", { name: "依頼者のアバター" }));

    expect(screen.queryByRole("img", { name: "依頼者のアバター" })).not.toBeInTheDocument();
    expect(screen.getByText("依頼者のアバター")).toBeInTheDocument();
  });
});
