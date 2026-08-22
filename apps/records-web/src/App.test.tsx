import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";

import { App } from "./App";
import { Avatar, formatCompletedDateTime } from "./components";
import type { SessionResponse } from "./api";
import { isRecordsApiResponse } from "./contracts";

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

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockApi(session: SessionResponse = authenticatedSession() as SessionResponse) {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path =
        typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      requests.push(path);
      if (path === "/api/v1/session") return Promise.resolve(response(session));
      if (path.startsWith("/api/v1/records?")) return Promise.resolve(response(listResponse()));
      if (path === `/api/v1/records/${RECORD_ID}`) {
        return Promise.resolve(response(recordDetail()));
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  return requests;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

describe("formatCompletedDateTime", () => {
  it("formats completed timestamps in fixed JST to minute precision", () => {
    expect(formatCompletedDateTime("2026-08-15T06:00:00Z")).toBe("2026年8月15日 15:00");
    expect(formatCompletedDateTime("2026-12-31T15:00:00Z")).toBe("2027年1月1日 00:00");
    expect(formatCompletedDateTime("2026-08-15T14:59:59Z")).toBe("2026年8月15日 23:59");
  });
});

describe("App", () => {
  it("shows the approved login page for an anonymous Guild visitor", async () => {
    mockApi({ schemaVersion: 1, authenticated: false, user: null, csrfToken: null });

    render(<App />);

    const productName = await screen.findByRole("heading", { name: "シッテムの箱 議事録" });
    expect(productName).toBeVisible();
    expect(Array.from(productName.children, (child) => child.textContent)).toEqual([
      "シッテムの箱",
      "議事録",
    ]);
    expect(screen.getByRole("link", { name: "Discordでログイン" })).toHaveAttribute(
      "href",
      "/api/v1/auth/discord/start?returnTo=%2F",
    );
    expect(screen.getByText("シッテムの箱BOTの議事録を閲覧できるシステム")).toBeVisible();
    expect(
      screen.getByText("吹雪型JCのつどいサーバのメンバーであることを認証します。"),
    ).toBeVisible();
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
        { name: "ログアウト" },
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/所要時間|Evidence|外部根拠/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("開始日")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("終了日")).not.toBeInTheDocument();
    expect(screen.getByLabelText("並び順")).toHaveValue("newest");
    expect(screen.getByLabelText("フリーワード検索")).toHaveAttribute(
      "placeholder",
      "質問文などを入力",
    );
    expect(screen.getByText("議論記録を閲覧できます。")).toBeVisible();
    expect(requests).toContain("/api/v1/records?limit=12&sort=newest");
  });

  it("requests the complete archive in the selected order", async () => {
    const requests = mockApi();
    render(<App />);
    await screen.findByRole("heading", { name: "議論の記録" });

    fireEvent.change(screen.getByLabelText("並び順"), { target: { value: "oldest" } });

    await waitFor(() => {
      expect(requests).toContain("/api/v1/records?limit=12&sort=oldest");
    });
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

    fireEvent.change(screen.getByLabelText("並び順"), { target: { value: "oldest" } });

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

    expect(await screen.findByRole("heading", { name: "シッテムの箱 議事録" })).toBeVisible();
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
